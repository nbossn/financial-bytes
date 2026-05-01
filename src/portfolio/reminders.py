"""Decision reminders — time-gated alerts for portfolio action deadlines.

Stores reminders as a JSON file. Scheduler fires Discord webhook notification
when a reminder's deadline is within 24 hours and it hasn't been sent yet.

File location: data/reminders.json (configurable via REMINDERS_PATH env var)

Format:
    {
      "reminders": [
        {
          "id": "amd-trim-2026-05-02",
          "context": "AMD trim: 5 shares before May 5 earnings. Cooling-off window closes today.",
          "deadline": "2026-05-02",
          "remind_hours_before": 24,
          "created_at": "2026-04-30",
          "sent": false
        }
      ]
    }

Usage:
    financial-bytes add-reminder --context "AMD trim" --deadline 2026-05-02
    financial-bytes list-reminders
    # Scheduler fires check-reminders at 6:00 AM ET daily
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


def _reminders_path() -> Path:
    env_path = os.getenv("REMINDERS_PATH")
    if env_path:
        return Path(env_path)
    return Path(__file__).parent.parent.parent / "data" / "reminders.json"


def _load() -> dict:
    path = _reminders_path()
    if not path.exists():
        return {"reminders": []}
    with open(path) as f:
        return json.load(f)


def _save(data: dict) -> None:
    path = _reminders_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def add_reminder(
    context: str,
    deadline: date,
    remind_hours_before: int = 24,
    reminder_id: Optional[str] = None,
) -> str:
    """Add a decision reminder. Returns the reminder ID."""
    data = _load()
    rid = reminder_id or f"{deadline.isoformat()}-{uuid.uuid4().hex[:6]}"

    # Remove any existing reminder with the same id (idempotent)
    data["reminders"] = [r for r in data["reminders"] if r.get("id") != rid]

    data["reminders"].append(
        {
            "id": rid,
            "context": context,
            "deadline": deadline.isoformat(),
            "remind_hours_before": remind_hours_before,
            "created_at": date.today().isoformat(),
            "sent": False,
        }
    )
    _save(data)
    logger.info(f"Reminder added: {rid} — deadline {deadline}")
    return rid


def remove_reminder(reminder_id: str) -> bool:
    """Remove a reminder by ID. Returns True if found and removed."""
    data = _load()
    before = len(data["reminders"])
    data["reminders"] = [r for r in data["reminders"] if r.get("id") != reminder_id]
    if len(data["reminders"]) == before:
        return False
    _save(data)
    return True


def get_due_reminders(reference_date: Optional[date] = None) -> list[dict]:
    """Return unsent reminders that are due within their remind_hours_before window.

    A reminder is due when: deadline - remind_hours_before <= reference_date < deadline + 1 day
    This means a 24h reminder is returned on the day before and the day of deadline.
    """
    today = reference_date or date.today()
    data = _load()
    due = []
    for r in data["reminders"]:
        if r.get("sent"):
            continue
        try:
            dl = date.fromisoformat(r["deadline"])
        except (KeyError, ValueError):
            continue
        hours_before = r.get("remind_hours_before", 24)
        trigger_date = dl - timedelta(hours=hours_before)
        if trigger_date <= today <= dl:
            due.append(r)
    return due


def mark_sent(reminder_id: str) -> None:
    """Mark a reminder as sent so it doesn't fire again."""
    data = _load()
    for r in data["reminders"]:
        if r.get("id") == reminder_id:
            r["sent"] = True
            r["sent_at"] = date.today().isoformat()
            break
    _save(data)


def list_reminders(include_sent: bool = False) -> list[dict]:
    """Return all reminders, optionally including already-sent ones."""
    data = _load()
    reminders = data.get("reminders", [])
    if not include_sent:
        reminders = [r for r in reminders if not r.get("sent")]
    return sorted(reminders, key=lambda r: r.get("deadline", ""))
