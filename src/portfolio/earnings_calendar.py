"""Earnings calendar — tracks upcoming earnings dates for portfolio positions.

Stores earnings events as a JSON file so the daemon can automatically run
premarket_check at 7:10 AM ET on earnings days.

File location: data/earnings_calendar.json (configurable via EARNINGS_CALENDAR_PATH env var)

Format:
    {
        "2026-04-30": [
            {
                "ticker": "LLY",
                "prev_close": 851.21,        # Set when known; null means fetch at runtime
                "time": "pre-market",         # "pre-market" or "after-close"
                "guide": "Mounjaro+Zepbound vs. $9-10B"  # Optional decision-guide hint
            }
        ],
        "2026-05-20": [
            {"ticker": "NVDA", "prev_close": null, "time": "after-close"}
        ]
    }

The daemon reads today's date at 7:05 AM ET and fires premarket_check for any
tickers marked "pre-market" in the calendar entry for today's date.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Optional

from loguru import logger


def _calendar_path() -> Path:
    env_path = os.getenv("EARNINGS_CALENDAR_PATH")
    if env_path:
        return Path(env_path)
    return Path(__file__).parent.parent.parent / "data" / "earnings_calendar.json"


def load_calendar() -> dict[str, list[dict]]:
    """Load the earnings calendar from JSON. Returns empty dict if file doesn't exist."""
    path = _calendar_path()
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def save_calendar(calendar: dict[str, list[dict]]) -> None:
    """Write the earnings calendar to JSON."""
    path = _calendar_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(calendar, f, indent=2, default=str)
    logger.info(f"Earnings calendar saved to {path}")


def add_earnings_event(
    earnings_date: date,
    ticker: str,
    time: str = "after-close",
    prev_close: Optional[float] = None,
    guide: Optional[str] = None,
) -> None:
    """Add or update an earnings event in the calendar.

    Args:
        earnings_date: The date earnings will be reported
        ticker: Stock ticker symbol
        time: "pre-market" or "after-close"
        prev_close: Previous close price (optional; fetched at runtime if None)
        guide: Optional hint about which metric to watch (for premarket inference context)
    """
    if time not in ("pre-market", "after-close"):
        raise ValueError(f"time must be 'pre-market' or 'after-close', got: {time!r}")

    calendar = load_calendar()
    date_key = earnings_date.isoformat()

    if date_key not in calendar:
        calendar[date_key] = []

    # Remove existing entry for this ticker on this date (idempotent update)
    calendar[date_key] = [e for e in calendar[date_key] if e["ticker"].upper() != ticker.upper()]

    entry: dict = {"ticker": ticker.upper(), "time": time}
    if prev_close is not None:
        entry["prev_close"] = prev_close
    if guide:
        entry["guide"] = guide

    calendar[date_key].append(entry)
    save_calendar(calendar)
    logger.info(f"Added earnings event: {ticker} on {date_key} ({time})")


def remove_earnings_event(earnings_date: date, ticker: str) -> bool:
    """Remove an earnings event. Returns True if removed, False if not found."""
    calendar = load_calendar()
    date_key = earnings_date.isoformat()
    if date_key not in calendar:
        return False
    before = len(calendar[date_key])
    calendar[date_key] = [e for e in calendar[date_key] if e["ticker"].upper() != ticker.upper()]
    if len(calendar[date_key]) == before:
        return False
    if not calendar[date_key]:
        del calendar[date_key]
    save_calendar(calendar)
    return True


def get_todays_premarket_events(reference_date: Optional[date] = None) -> list[dict]:
    """Return pre-market earnings events for today (or reference_date).

    Used by the daemon to determine which tickers to check at 7:10 AM ET.
    """
    target = reference_date or date.today()
    calendar = load_calendar()
    events = calendar.get(target.isoformat(), [])
    return [e for e in events if e.get("time") == "pre-market"]


def get_todays_afterclose_events(reference_date: Optional[date] = None) -> list[dict]:
    """Return after-close earnings events for today (or reference_date)."""
    target = reference_date or date.today()
    calendar = load_calendar()
    events = calendar.get(target.isoformat(), [])
    return [e for e in events if e.get("time") == "after-close"]


def upcoming_events(days_ahead: int = 30) -> list[tuple[date, list[dict]]]:
    """Return earnings events for the next N days, sorted by date."""
    from datetime import timedelta
    calendar = load_calendar()
    today = date.today()
    results = []
    for date_str, events in calendar.items():
        try:
            event_date = date.fromisoformat(date_str)
        except ValueError:
            continue
        if today <= event_date <= today + timedelta(days=days_ahead):
            results.append((event_date, events))
    return sorted(results, key=lambda x: x[0])
