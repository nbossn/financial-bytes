"""Wash-sale tracking — IRS 30-day repurchase window.

Maintains an append-only JSON ledger of realized sells. The tax-aware stop
engine calls `is_wash_sale_blocked` before issuing a HARVEST recommendation
to ensure we don't advise selling when the wash-sale rule would disallow the
loss deduction.

IRS rule: if you sell a security at a loss and buy the same (or substantially
identical) security within 30 days before or after the sale, the loss is
disallowed and added to the basis of the replacement shares.

Usage:
    blocked, days_left = is_wash_sale_blocked("FBTC")
    if not blocked:
        # safe to harvest
        record_sell("FBTC", sell_date=date.today(), shares=10.0)
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from loguru import logger

WASH_SALE_DAYS: int = 30  # IRS wash-sale window


def _default_ledger_path() -> Path:
    return Path(__file__).parent.parent.parent / "data" / "wash_sale_log.json"


def load_ledger(ledger_path: Path | None = None) -> list[dict]:
    """Load realized-sell ledger. Returns [] if the file does not exist."""
    path = ledger_path or _default_ledger_path()
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not read wash-sale ledger ({path}): {e}")
        return []


def save_ledger(entries: list[dict], ledger_path: Path | None = None) -> None:
    """Persist the ledger. Creates parent directories as needed."""
    path = ledger_path or _default_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, default=str)


def is_wash_sale_blocked(
    ticker: str,
    check_date: date | None = None,
    ledger_path: Path | None = None,
) -> tuple[bool, int | None]:
    """Check whether a ticker is within the 30-day wash-sale window.

    Args:
        ticker:      Stock symbol (case-insensitive).
        check_date:  Date to check against (defaults to today).
        ledger_path: Override path to the ledger JSON file.

    Returns:
        (blocked, days_remaining)
        blocked is True when a sell was recorded within the last 30 days.
        days_remaining is the number of days left in the window, or None if not blocked.
    """
    entries = load_ledger(ledger_path)
    as_of = check_date or date.today()
    ticker_up = ticker.upper()

    for entry in entries:
        if entry.get("ticker", "").upper() != ticker_up:
            continue
        sell_date_str = entry.get("sell_date")
        if not sell_date_str:
            continue
        try:
            sell_date = date.fromisoformat(str(sell_date_str))
        except ValueError:
            continue
        days_since = (as_of - sell_date).days
        if 0 <= days_since <= WASH_SALE_DAYS:
            days_remaining = WASH_SALE_DAYS - days_since
            logger.debug(
                f"{ticker_up}: wash-sale window active — "
                f"{days_remaining}d remaining (sold {sell_date})"
            )
            return True, days_remaining

    return False, None


def record_sell(
    ticker: str,
    sell_date: date | None = None,
    shares: float | None = None,
    proceeds_per_share: float | None = None,
    ledger_path: Path | None = None,
) -> None:
    """Append a realized sell to the wash-sale ledger.

    Call this when Nick actually executes a harvest or any other sale so
    subsequent HARVEST recommendations correctly respect the window.

    Args:
        ticker:             Stock symbol.
        sell_date:          Date of the sell (defaults to today).
        shares:             Number of shares sold (optional, for audit trail).
        proceeds_per_share: Sale price per share (optional, for audit trail).
        ledger_path:        Override path to the ledger JSON file.
    """
    entries = load_ledger(ledger_path)
    entry: dict = {
        "ticker": ticker.upper(),
        "sell_date": str(sell_date or date.today()),
    }
    if shares is not None:
        entry["shares"] = shares
    if proceeds_per_share is not None:
        entry["proceeds_per_share"] = proceeds_per_share

    entries.append(entry)
    save_ledger(entries, ledger_path)
    logger.info(f"Wash-sale ledger: recorded {ticker.upper()} sell on {entry['sell_date']}")


def wash_sale_status_note(ticker: str, ledger_path: Path | None = None) -> str:
    """Return a human-readable wash-sale status note for display."""
    blocked, days_left = is_wash_sale_blocked(ticker, ledger_path=ledger_path)
    if blocked and days_left is not None:
        return f"wash-sale blocked: {days_left}d remaining"
    return "wash-sale clear"
