"""A reminder that was never delivered must not be recorded as delivered.

Production incident, reconstructed from `logs/scheduler.log` and proven by
running the real code against the pre-untrack snapshot of the store:

    2026-05-01  Reminder check: 1 reminder(s) due   -> Discord alert sent
    2026-05-04  Reminder check: 1 reminder(s) due   -> Discord alert sent
    2026-05-07  Reminder check: 2 reminder(s) due   -> DISCORD_WEBHOOK_URL not set

On 2026-05-07 the two reminders due were `lilich-msft-trim-may8` (trim 150
shares) and `palantir-portal-may7` (a job application yes/no whose own text
says non-action is recorded after May 7). Neither was delivered. Both were
marked sent, because `_run_reminder_check` called `mark_sent()` for every due
reminder unconditionally — `_send_reminder_discord` returned `None` whether it
posted or not, so there was nothing for the caller to branch on.

Two stacked failures, and the second is the durable one:

  1. The webhook is resolved with a bare `os.getenv`. The daemon is launched by
     cron/@reboot and runs with EIGHT environment variables; DISCORD_WEBHOOK_URL
     is not one of them, while `.env` has held it the whole time. Config that is
     present but unreadable is indistinguishable from config that is absent.

  2. Delivery failure and delivery success wrote the same state. A reminder is
     time-gated — `get_due_reminders` stops returning it once the deadline
     passes — so "marked sent" is final. The alert did not fail loudly; it
     failed into the shape of an alert that had already been handled.

Fixing the env lookup closes today's instance. Making delivery decide what gets
recorded closes the class: any future transport failure (HTTP 500, timeout, a
rotated webhook) now leaves the reminder pending and says so.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest


# ── the shared resolver ───────────────────────────────────────────


def test_resolver_reads_env_file_when_process_env_is_empty(monkeypatch):
    """The daemon case: nothing exported, .env has the value.

    This is the whole production failure in one assertion. `os.getenv` returns
    None here; the resolver must not.
    """
    from src import config

    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    config.settings.discord_webhook_url = "https://discord.test/from-env-file"
    try:
        assert config.discord_webhook() == "https://discord.test/from-env-file"
    finally:
        config.settings.discord_webhook_url = ""


def test_resolver_gives_process_env_precedence(monkeypatch):
    """An explicit export must still win, so an operator override survives."""
    from src import config

    config.settings.discord_webhook_url = "https://discord.test/from-env-file"
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/exported")
    try:
        assert config.discord_webhook() == "https://discord.test/exported"
    finally:
        config.settings.discord_webhook_url = ""


def test_resolver_returns_empty_when_neither_source_has_it(monkeypatch):
    from src import config

    config.settings.discord_webhook_url = ""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert config.discord_webhook() == ""


def test_resolver_treats_whitespace_only_as_absent(monkeypatch):
    """An empty-but-present key is the shape `.env` files actually take."""
    from src import config

    config.settings.discord_webhook_url = "   "
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "  ")
    try:
        assert config.discord_webhook() == ""
    finally:
        config.settings.discord_webhook_url = ""


# ── the sender reports whether it sent ────────────────────────────


@pytest.fixture
def one_reminder():
    return [{"id": "r1", "deadline": "2026-05-08", "context": "Trim 150sh MSFT"}]


def test_sender_returns_false_when_no_webhook(monkeypatch, one_reminder):
    from src import config, scheduler

    config.settings.discord_webhook_url = ""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert scheduler._send_reminder_discord(one_reminder) is False


def test_sender_returns_false_when_post_raises(monkeypatch, one_reminder):
    import requests

    from src import config, scheduler

    config.settings.discord_webhook_url = "https://discord.test/hook"

    def _boom(*a, **k):
        raise requests.exceptions.ConnectTimeout("network down")

    monkeypatch.setattr(requests, "post", _boom)
    try:
        assert scheduler._send_reminder_discord(one_reminder) is False
    finally:
        config.settings.discord_webhook_url = ""


def test_sender_returns_false_on_http_error(monkeypatch, one_reminder):
    """A rotated/revoked webhook answers 404. `requests` does not raise on its
    own — only `raise_for_status` does — so this is the realistic failure."""
    import requests

    from src import config, scheduler

    config.settings.discord_webhook_url = "https://discord.test/hook"

    class _Resp:
        status_code = 404

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("404 Not Found")

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    try:
        assert scheduler._send_reminder_discord(one_reminder) is False
    finally:
        config.settings.discord_webhook_url = ""


def test_sender_returns_true_on_success(monkeypatch, one_reminder):
    import requests

    from src import config, scheduler

    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    config.settings.discord_webhook_url = "https://discord.test/hook"
    posted = {}

    class _Resp:
        status_code = 204

        def raise_for_status(self):
            return None

    def _post(url, **kwargs):
        posted["url"] = url
        posted["content"] = kwargs.get("json", {}).get("content", "")
        return _Resp()

    monkeypatch.setattr(requests, "post", _post)
    try:
        assert scheduler._send_reminder_discord(one_reminder) is True
    finally:
        config.settings.discord_webhook_url = ""

    assert posted["url"] == "https://discord.test/hook"
    # the reminder's own text must reach the message, not just a count
    assert "Trim 150sh MSFT" in posted["content"]


# ── the caller branches on delivery ───────────────────────────────


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real reminders.json on disk, driven through the real module."""
    import json

    path = tmp_path / "reminders.json"
    payload = {
        "reminders": [
            {
                "id": "lilich-msft-trim-may8",
                "context": "Lilich MSFT trim 150sh",
                "deadline": "2026-05-08",
                "remind_hours_before": 24,
                "created_at": "2026-04-30",
                "sent": False,
            },
            {
                "id": "palantir-portal-may7",
                "context": "Palantir FDE portal application - yes/no decision.",
                "deadline": "2026-05-07",
                "remind_hours_before": 24,
                "created_at": "2026-04-30",
                "sent": False,
            },
        ]
    }
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("REMINDERS_PATH", str(path))
    return path


def _sent_flags(path):
    import json

    return {r["id"]: bool(r.get("sent")) for r in json.loads(path.read_text())["reminders"]}


def test_failed_delivery_leaves_reminders_pending(monkeypatch, store):
    """The bug, stated as a test.

    Two reminders due, delivery impossible, and afterwards the store must still
    say they are waiting to be sent.
    """
    from src import config, scheduler
    from src.portfolio import reminders as rem

    config.settings.discord_webhook_url = ""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(rem, "date", _FrozenDate(date(2026, 5, 7)))

    scheduler._run_reminder_check()

    assert _sent_flags(store) == {
        "lilich-msft-trim-may8": False,
        "palantir-portal-may7": False,
    }


def test_successful_delivery_marks_reminders_sent(monkeypatch, store):
    """The control. Without this, the test above passes on code that never
    marks anything sent — which would be a different bug, not a fix."""
    import requests

    from src import config, scheduler
    from src.portfolio import reminders as rem

    config.settings.discord_webhook_url = "https://discord.test/hook"

    class _Resp:
        status_code = 204

        def raise_for_status(self):
            return None

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr(rem, "date", _FrozenDate(date(2026, 5, 7)))
    try:
        scheduler._run_reminder_check()
    finally:
        config.settings.discord_webhook_url = ""

    assert _sent_flags(store) == {
        "lilich-msft-trim-may8": True,
        "palantir-portal-may7": True,
    }


def test_pending_reminder_retries_next_day_within_window(monkeypatch, store):
    """Not marking sent is only worth something if it actually retries."""
    import requests

    from src import config, scheduler
    from src.portfolio import reminders as rem

    # Day 1: delivery impossible.
    config.settings.discord_webhook_url = ""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(rem, "date", _FrozenDate(date(2026, 5, 7)))
    scheduler._run_reminder_check()
    assert _sent_flags(store)["lilich-msft-trim-may8"] is False

    # Day 2: still inside the window, webhook restored -> it goes out.
    calls = []

    class _Resp:
        status_code = 204

        def raise_for_status(self):
            return None

    def _post(url, **kwargs):
        calls.append(kwargs.get("json", {}).get("content", ""))
        return _Resp()

    config.settings.discord_webhook_url = "https://discord.test/hook"
    monkeypatch.setattr(requests, "post", _post)
    monkeypatch.setattr(rem, "date", _FrozenDate(date(2026, 5, 8)))
    try:
        scheduler._run_reminder_check()
    finally:
        config.settings.discord_webhook_url = ""

    assert len(calls) == 1
    assert "Lilich MSFT trim 150sh" in calls[0]
    assert _sent_flags(store)["lilich-msft-trim-may8"] is True


def test_no_due_reminders_does_not_call_the_sender(monkeypatch, store):
    from src import scheduler
    from src.portfolio import reminders as rem

    called = []
    monkeypatch.setattr(scheduler, "_send_reminder_discord", lambda r: called.append(r) or True)
    monkeypatch.setattr(rem, "date", _FrozenDate(date(2026, 1, 1)))

    scheduler._run_reminder_check()
    assert called == []


# ── an undelivered reminder that expires must be reported ─────────


def test_expired_unsent_reminders_are_reported(store):
    """Leaving a reminder pending converts permanent silent loss into loss that
    is still silent once the deadline passes. This is the half that makes it
    audible."""
    from src.portfolio.reminders import get_expired_unsent_reminders

    expired = get_expired_unsent_reminders(date(2026, 5, 20))
    assert {r["id"] for r in expired} == {
        "lilich-msft-trim-may8",
        "palantir-portal-may7",
    }


def test_expired_report_excludes_sent_and_future(monkeypatch, store):
    """Control: it must be able to return fewer than everything."""
    import json

    from src.portfolio.reminders import get_expired_unsent_reminders

    data = json.loads(store.read_text())
    data["reminders"][0]["sent"] = True  # msft: delivered
    store.write_text(json.dumps(data))

    # palantir deadline 05-07 is past; msft is sent -> exactly one
    assert [r["id"] for r in get_expired_unsent_reminders(date(2026, 5, 20))] == [
        "palantir-portal-may7"
    ]
    # before any deadline -> none expired
    assert get_expired_unsent_reminders(date(2026, 5, 1)) == []


def test_check_logs_expired_unsent(monkeypatch, store, caplog):
    from src import config, scheduler
    from src.portfolio import reminders as rem

    config.settings.discord_webhook_url = ""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(rem, "date", _FrozenDate(date(2026, 5, 20)))

    messages = []
    monkeypatch.setattr(scheduler.logger, "warning", lambda m, *a, **k: messages.append(str(m)))
    scheduler._run_reminder_check()

    joined = " ".join(messages)
    assert "palantir-portal-may7" in joined
    assert "lilich-msft-trim-may8" in joined


# ── no posting site may bypass the resolver ───────────────────────


def test_no_discord_posting_site_uses_a_bare_env_lookup():
    """Static sweep with a positive control.

    A bare `os.getenv("DISCORD_WEBHOOK_URL")` is the defect itself: it reads a
    place the daemon's environment does not have. Every module that posts to
    Discord must go through `config.discord_webhook()`.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "src"
    posting_files, offenders = [], []
    bare = re.compile(r"""(os\.getenv|os\.environ\.get|os\.environ\[)\s*\(?\s*['"]DISCORD_WEBHOOK_URL""")

    for py in root.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        if "DISCORD_WEBHOOK_URL" not in text and "discord_webhook" not in text:
            continue
        # The resolver itself is the one place allowed to read the process
        # environment — excluding it by the definition it provides, not by
        # filename, so moving the function does not silently widen the exemption.
        if "def discord_webhook(" in text:
            continue
        posting_files.append(py)
        if bare.search(text):
            offenders.append(str(py.relative_to(root)))

    # A scan that passes on zero inputs is indistinguishable from one that
    # passes on good inputs. Assert it found the surface before judging it.
    assert len(posting_files) >= 4, f"discovery found only {len(posting_files)} files"
    assert offenders == [], f"bare env lookup at Discord posting sites: {offenders}"


def test_premarket_sender_also_uses_the_resolver(monkeypatch):
    """The sibling on the same daemon. It loses no state, but it is silently
    dead in cron for exactly the same reason."""
    from src import config, scheduler

    config.settings.discord_webhook_url = ""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert scheduler._send_premarket_discord([]) is False

    import requests

    class _Resp:
        status_code = 204

        def raise_for_status(self):
            return None

    config.settings.discord_webhook_url = "https://discord.test/hook"
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    try:
        assert scheduler._send_premarket_discord([]) is True
    finally:
        config.settings.discord_webhook_url = ""


def test_sender_makes_no_network_call_without_a_webhook(monkeypatch, one_reminder):
    """Guards the tests themselves: nothing here may reach the real webhook."""
    import requests

    from src import config, scheduler

    config.settings.discord_webhook_url = ""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    def _forbidden(*a, **k):
        raise AssertionError("requests.post must not be reached without a webhook")

    monkeypatch.setattr(requests, "post", _forbidden)
    assert scheduler._send_reminder_discord(one_reminder) is False


class _FrozenDate:
    """Stands in for `datetime.date` inside the reminders module so the
    time-gated logic can be driven without waiting for a calendar."""

    def __init__(self, value: date):
        self._value = value

    def today(self) -> date:
        return self._value

    @staticmethod
    def fromisoformat(s: str) -> date:
        return date.fromisoformat(s)
