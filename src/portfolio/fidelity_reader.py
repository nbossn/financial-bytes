"""Fidelity portfolio reader — parses Fidelity's positions CSV export."""
from __future__ import annotations

import csv
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from loguru import logger

from src.portfolio.models import Holding


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
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Fidelity positions file not found: {path}")

    holdings: list[Holding] = []
    skipped: list[str] = []

    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        for row in reader:
            # Skip footer/disclaimer rows (Fidelity appends these after data)
            symbol_raw = (row.get("Symbol") or "").strip()
            if not symbol_raw:
                continue

            # Skip money market and footnoted symbols
            if _is_skip_symbol(symbol_raw):
                skipped.append(symbol_raw)
                continue

            # Account filter
            if account_filter:
                account_name = row.get("Account Name", "")
                if account_filter.lower() not in account_name.lower():
                    continue

            ticker = _SKIP_PATTERN.sub("", symbol_raw).upper()

            quantity = _clean_decimal(row.get("Quantity") or "")
            avg_cost = _clean_decimal(row.get("Average Cost Basis") or "")
            cost_basis_total = _clean_decimal(row.get("Cost Basis Total") or "")

            # Money market funds (e.g. SPAXX) are priced at $1.00/share;
            # Fidelity omits Quantity and Cost Basis — derive from Current Value.
            if (quantity is None or quantity <= 0) and ticker in _MONEY_MARKET_SYMBOLS:
                current_value = _clean_decimal(row.get("Current Value") or "")
                if current_value and current_value > 0:
                    quantity = current_value
                    avg_cost = Decimal("1.00")
                    logger.debug(f"Money market {ticker}: derived {quantity} shares @ $1.00 from Current Value")
                else:
                    logger.warning(f"Skipping {ticker}: no value data for money market fund")
                    skipped.append(ticker)
                    continue

            if quantity is None or quantity <= 0:
                logger.debug(f"Skipping {ticker}: invalid quantity '{row.get('Quantity')}'")
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

            holdings.append(
                Holding(
                    ticker=ticker,
                    shares=quantity,
                    cost_basis=avg_cost,
                    purchase_date=date.today(),  # Fidelity positions don't include lot dates
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
