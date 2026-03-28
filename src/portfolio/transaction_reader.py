"""Parse Robinhood transaction CSV into portfolio holdings.

Robinhood export format (Activity → Export to CSV):
  Activity Date, Process Date, Settle Date, Instrument, Description,
  Trans Code, Quantity, Price, Amount

Trans Code values handled: Buy, Sell (others are cash events and are skipped).
Cost basis is computed as the weighted average of all buys across the history.
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from loguru import logger

from src.portfolio.models import Holding
from src.portfolio.reader import PortfolioReadError

# Trans codes that represent equity buys / sells
_BUY_CODES = {"buy", "bto"}   # buy-to-open
_SELL_CODES = {"sell", "stc"}  # sell-to-close


def _clean_decimal(raw: str) -> Decimal:
    """Strip currency symbols, commas, parentheses and convert to Decimal."""
    cleaned = re.sub(r"[$,\s]", "", raw.strip())
    # Parentheses mean negative in accounting notation
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    return Decimal(cleaned)


def _parse_date(raw: str) -> date:
    """Handle M/D/YYYY (Robinhood) or YYYY-MM-DD formats."""
    raw = raw.strip()
    if "/" in raw:
        parts = raw.split("/")
        if len(parts) == 3:
            month, day, year = parts
            return date(int(year), int(month), int(day))
    return date.fromisoformat(raw)


def read_transactions(csv_path: str | Path) -> list[Holding]:
    """Parse a Robinhood transaction CSV and return net holdings.

    Only tickers with positive net shares after all buys and sells are returned.
    Cost basis is the weighted average price of all buy transactions.
    Purchase date is the date of the first buy for each ticker.

    Returns:
        List of Holding objects sorted by ticker.

    Raises:
        PortfolioReadError: If file is missing or has unrecognisable format.
    """
    path = Path(csv_path)
    if not path.exists():
        raise PortfolioReadError(f"Transaction file not found: {path}")

    # Per-ticker accumulators
    total_shares: dict[str, Decimal] = defaultdict(Decimal)   # net (buy - sell)
    total_buy_cost: dict[str, Decimal] = defaultdict(Decimal)  # cumulative buy value
    total_buy_shares: dict[str, Decimal] = defaultdict(Decimal)  # cumulative buy shares
    first_buy_date: dict[str, date] = {}

    rows_processed = 0

    with path.open(newline="", encoding="utf-8-sig") as f:  # utf-8-sig strips BOM
        # Skip non-CSV preamble rows (Robinhood sometimes prepends account summary lines)
        raw_lines = f.read().splitlines()

    # Find the header row
    header_idx = None
    for i, line in enumerate(raw_lines):
        if "Trans Code" in line or "Activity Date" in line:
            header_idx = i
            break

    if header_idx is None:
        raise PortfolioReadError(
            "Could not locate header row in transaction CSV. "
            "Expected columns: Activity Date, Instrument, Trans Code, Quantity, Price"
        )

    csv_lines = "\n".join(raw_lines[header_idx:])
    reader = csv.DictReader(csv_lines.splitlines())

    # Normalise column names (strip whitespace)
    if reader.fieldnames is None:
        raise PortfolioReadError("Transaction CSV appears empty after header")

    required = {"Instrument", "Trans Code", "Quantity", "Price", "Activity Date"}
    present = {f.strip() for f in reader.fieldnames}
    missing = required - present
    if missing:
        raise PortfolioReadError(
            f"Transaction CSV missing expected columns: {missing}. "
            f"Found: {list(reader.fieldnames)}"
        )

    for row_num, row in enumerate(reader, start=header_idx + 2):
        # Normalise keys
        row = {k.strip(): v.strip() for k, v in row.items() if k}

        ticker = row.get("Instrument", "").strip().upper()
        trans_code = row.get("Trans Code", "").strip().lower()

        # Skip cash-only events (no instrument) or unknown codes
        if not ticker or ticker in ("", "-"):
            continue
        if trans_code not in (_BUY_CODES | _SELL_CODES):
            continue

        try:
            qty_raw = row.get("Quantity", "").strip()
            price_raw = row.get("Price", "").strip()
            if not qty_raw or not price_raw:
                continue

            qty = _clean_decimal(qty_raw)
            price = _clean_decimal(price_raw)
            if qty <= 0 or price <= 0:
                logger.debug(f"Row {row_num}: skipping zero/negative qty/price for {ticker}")
                continue

            txn_date = _parse_date(row.get("Activity Date", ""))

            if trans_code in _BUY_CODES:
                total_shares[ticker] += qty
                total_buy_shares[ticker] += qty
                total_buy_cost[ticker] += qty * price
                if ticker not in first_buy_date or txn_date < first_buy_date[ticker]:
                    first_buy_date[ticker] = txn_date
            else:  # sell
                total_shares[ticker] -= qty

            rows_processed += 1

        except (InvalidOperation, ValueError) as e:
            logger.warning(f"Row {row_num}: could not parse transaction for {ticker} — {e}")
            continue

    if rows_processed == 0:
        raise PortfolioReadError(
            "No Buy/Sell transactions found in the CSV. "
            "Ensure the file is a Robinhood activity export with Trans Code column."
        )

    holdings = []
    for ticker in sorted(total_shares):
        net_shares = total_shares[ticker]
        if net_shares <= Decimal("0.0001"):
            logger.debug(f"Skipping {ticker}: net shares {net_shares} (fully sold)")
            continue

        buy_shares = total_buy_shares[ticker]
        if buy_shares == 0:
            continue

        avg_cost = total_buy_cost[ticker] / buy_shares
        purchase_date = first_buy_date.get(ticker)

        holdings.append(Holding(
            ticker=ticker,
            shares=net_shares.quantize(Decimal("0.0001")),
            cost_basis=avg_cost.quantize(Decimal("0.0001")),
            purchase_date=purchase_date,
        ))
        logger.debug(f"  {ticker}: {net_shares} shares @ avg ${avg_cost:.4f} (first buy {purchase_date})")

    if not holdings:
        raise PortfolioReadError("Transaction CSV produced no open holdings (all positions fully sold?)")

    logger.info(
        f"Transaction import: {rows_processed} transactions → {len(holdings)} open holdings: "
        f"{[h.ticker for h in holdings]}"
    )
    return holdings


def export_holdings_to_csv(holdings: list[Holding], output_path: str | Path) -> None:
    """Write a holdings list to the portfolio CSV format."""
    path = Path(output_path)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "shares", "cost_basis", "purchase_date"])
        for h in holdings:
            writer.writerow([
                h.ticker,
                str(h.shares),
                str(h.cost_basis),
                str(h.purchase_date) if h.purchase_date else "",
            ])
    logger.info(f"Exported {len(holdings)} holdings to {path}")
