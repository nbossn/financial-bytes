"""Fidelity portfolio reader — parses Fidelity's positions CSV export."""
from __future__ import annotations

import csv
import glob as _glob
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from loguru import logger

from src.portfolio.models import Holding

# Warn when the newest available export is older than this. The Jun-24 export
# was silently used for 55 days while the newsletter reported live prices.
STALE_EXPORT_DAYS = 7

# e.g. "Portfolio_Positions_Aug-18-2026.csv", "Portfolio_Positions_Aug-18-2026 (3).csv"
_EXPORT_DATE_RE = re.compile(r"Portfolio_Positions_([A-Z][a-z]{2}-\d{2}-\d{4})")


def _export_date(path: Path) -> date | None:
    """Parse the export date out of a Fidelity positions filename."""
    m = _EXPORT_DATE_RE.search(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%b-%d-%Y").date()
    except ValueError:
        return None


def resolve_positions_path(csv_path: str | Path) -> Path:
    """Resolve a positions-CSV path that may be a glob, to the newest export.

    Ranks matches by the date embedded in the filename (Fidelity's own naming),
    falling back to mtime for undated names and for same-day repeat downloads
    ("... (1).csv", "... (2).csv"). Comparing parsed dates rather than filename
    strings matters: lexically "Aug" sorts before "Jun", so a plain sort picks
    the wrong file.

    A concrete (non-glob) path is returned unchanged if it exists.
    """
    raw = str(csv_path)

    if not _glob.has_magic(raw):
        path = Path(raw)
        if not path.exists():
            raise FileNotFoundError(f"Fidelity positions file not found: {path}")
        return path

    matches = [Path(p) for p in _glob.glob(raw)]
    matches = [p for p in matches if p.is_file()]
    if not matches:
        raise FileNotFoundError(f"No Fidelity positions export matched: {raw}")

    # Sort key: (has-date, date, mtime). Undated files rank below dated ones,
    # ordered among themselves by mtime.
    def _key(p: Path):
        d = _export_date(p)
        return (d is not None, d or date.min, p.stat().st_mtime)

    newest = max(matches, key=_key)

    exported = _export_date(newest)
    if exported is not None:
        age = (date.today() - exported).days
        if age > STALE_EXPORT_DAYS:
            logger.warning(
                f"Fidelity positions export is stale: {newest.name} is {age} days old "
                f"(> {STALE_EXPORT_DAYS}). Run `financial-bytes fidelity-sync` to refresh."
            )

    if len(matches) > 1:
        logger.info(f"Resolved {raw} → {newest.name} (newest of {len(matches)} exports)")

    return newest


# Symbols to always skip (money market funds, ETFs used as cash equivalents)
# Note: SPAXX is intentionally NOT in this set — it is included as a cash-equivalent holding
_SKIP_SYMBOLS = {"FDRXX", "FCNTX", "FZFXX", "FDLXX"}
_SKIP_PATTERN = re.compile(r"\*+$")  # e.g. "SPAXX**"

# Money market funds priced at $1.00/share — derive quantity from Current Value when Quantity is blank
_MONEY_MARKET_SYMBOLS = {"SPAXX", "FZDXX", "FZAXX"}


def _clean_decimal(value: str) -> Decimal | None:
    """Strip $, +, -, %, commas from Fidelity-formatted numbers. Returns None if empty."""
    if not value or not value.strip():
        return None
    cleaned = value.strip().lstrip("+").replace("$", "").replace(",", "").replace("%", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _is_skip_symbol(symbol: str) -> bool:
    """Return True if this symbol should be excluded (money market, footnoted, etc)."""
    s = symbol.strip()
    if not s:
        return True
    bare = _SKIP_PATTERN.sub("", s).upper()
    return bare in _SKIP_SYMBOLS


def _normalize_row(row: dict) -> dict:
    """Lower-case and collapse whitespace in a CSV row's keys.

    Fidelity changed its export header casing between 2026-06-24 and
    2026-08-18 ("Account Number" -> "Account number", "Average Cost Basis"
    -> "Average cost basis"). Exact-key lookups silently returned None for
    every renamed column, so all rows were dropped as "no cost basis data".
    Normalizing keys makes the reader tolerant of either casing.
    """
    normalized = {}
    for key, value in row.items():
        if key is None:
            continue
        normalized[" ".join(key.split()).lower()] = value
    return normalized


def read_fidelity_positions(
    csv_path: str | Path,
    account_filter: str | None = None,
) -> list[Holding]:
    """Parse a Fidelity Portfolio_Positions_*.csv export into a list of Holdings.

    Fidelity positions CSV columns (as of 2026):
      Account Number, Account Name, Symbol, Description, Quantity,
      Last Price, Last Price Change, Current Value,
      Today's Gain/Loss Dollar, Today's Gain/Loss Percent,
      Total Gain/Loss Dollar, Total Gain/Loss Percent,
      Percent Of Account, Cost Basis Total, Average Cost Basis, Type

    Args:
        csv_path: Path to the Fidelity positions CSV.
        account_filter: If set, only include holdings from accounts whose
            Account Name contains this string (case-insensitive).
            E.g. "Trust" to include only "Trust: Under Agreement".

    Returns:
        List of Holding objects (one per ticker, fractional shares supported).
    """
    path = resolve_positions_path(csv_path)

    holdings: list[Holding] = []
    skipped: list[str] = []

    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        for raw_row in reader:
            # Fidelity varies header casing between exports — match case-insensitively
            row = _normalize_row(raw_row)

            # Skip footer/disclaimer rows (Fidelity appends these after data)
            symbol_raw = (row.get("symbol") or "").strip()
            if not symbol_raw:
                continue

            # Skip money market and footnoted symbols
            if _is_skip_symbol(symbol_raw):
                skipped.append(symbol_raw)
                continue

            # Account filter — supports str (single) or list[str] (OR logic)
            # Matches against both Account Name and Account Number (case-insensitive)
            if account_filter:
                account_name = row.get("account name") or ""
                account_number = row.get("account number") or ""
                filters = account_filter if isinstance(account_filter, list) else [account_filter]
                if not any(
                    f.lower() in account_name.lower() or f.lower() in account_number.lower()
                    for f in filters
                ):
                    continue

            ticker = _SKIP_PATTERN.sub("", symbol_raw).upper()

            quantity = _clean_decimal(row.get("quantity") or "")
            avg_cost = _clean_decimal(row.get("average cost basis") or "")
            cost_basis_total = _clean_decimal(row.get("cost basis total") or "")

            # Money market funds (e.g. SPAXX) are priced at $1.00/share;
            # Fidelity omits Quantity and Cost Basis — derive from Current Value.
            if (quantity is None or quantity <= 0) and ticker in _MONEY_MARKET_SYMBOLS:
                current_value = _clean_decimal(row.get("current value") or "")
                if current_value and current_value > 0:
                    quantity = current_value
                    avg_cost = Decimal("1.00")
                    logger.debug(f"Money market {ticker}: derived {quantity} shares @ $1.00 from Current Value")
                else:
                    logger.warning(f"Skipping {ticker}: no value data for money market fund")
                    skipped.append(ticker)
                    continue

            if quantity is None or quantity <= 0:
                logger.debug(f"Skipping {ticker}: invalid quantity '{row.get('quantity')}'")
                skipped.append(ticker)
                continue

            # Prefer Average Cost Basis; fall back to Cost Basis Total / Quantity
            if avg_cost is None or avg_cost <= 0:
                if cost_basis_total and cost_basis_total > 0:
                    avg_cost = (cost_basis_total / quantity).quantize(Decimal("0.0001"))
                else:
                    logger.warning(f"Skipping {ticker}: no cost basis data")
                    skipped.append(ticker)
                    continue

            acct_num = (row.get("account number") or "").strip() or None
            holdings.append(
                Holding(
                    ticker=ticker,
                    shares=quantity,
                    cost_basis=avg_cost,
                    # Fidelity's positions export carries no lot dates. Leave this
                    # unknown rather than stamping today: a today stamp told the
                    # analyst agent the position was bought this morning, which
                    # produced fabricated claims like "the CEO sold on your
                    # day-of-entry". Real lot dates come from purchase_history.
                    purchase_date=None,
                    account_number=acct_num,
                )
            )
            logger.debug(f"Loaded Fidelity holding: {ticker} {quantity}@{avg_cost}")

    if skipped:
        logger.debug(f"Skipped {len(skipped)} non-equity rows: {skipped[:10]}")

    if not holdings:
        raise ValueError(f"No equity holdings found in {path}")

    logger.info(f"Loaded {len(holdings)} Fidelity holdings: {[h.ticker for h in holdings]}")
    return holdings


def export_fidelity_to_portfolio_csv(
    positions_csv: str | Path,
    output_csv: str | Path,
    account_filter: str | None = None,
) -> list[Holding]:
    """Read Fidelity positions and write a standard portfolio.csv.

    This lets the existing pipeline ingest Fidelity data via `--portfolio`.
    """
    holdings = read_fidelity_positions(positions_csv, account_filter=account_filter)

    out = Path(output_csv)
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "shares", "cost_basis", "purchase_date"])
        for h in holdings:
            writer.writerow([h.ticker, h.shares, h.cost_basis, h.purchase_date])

    logger.info(f"Fidelity portfolio CSV written → {out} ({len(holdings)} holdings)")
    return holdings
