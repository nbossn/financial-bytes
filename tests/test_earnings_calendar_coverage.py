"""Tests for earnings-calendar coverage reporting.

Measured on 2026-07-26, and the reason this module exists:

`_run_premarket_earnings_check` runs at 7:10 AM ET every day and logs

    Premarket earnings check: no pre-market events today

**43 runs, 43 identical all-clears, zero events, ever.** That line is literally
true and completely uninformative, because `data/earnings_calendar.json` is
hand-fed: its only writer is the `add-earnings-event` CLI command, it has
exactly one commit (the feature commit, 2026-04-30), and its newest entry is
**2026-05-20**. So since 2026-05-21 the check has been *structurally incapable*
of returning an event, and said the same reassuring thing throughout.

Payload, not topology — across the tickers the 07-26 pipeline actually built
charts for, 5 pre-market earnings events fell in the window, of which **2 landed
on days the 7:10 check genuinely ran**:

    2026-07-14  JPM   (pre-market)  -> "no pre-market events today"
    2026-07-22  GEV   (pre-market)  -> "no pre-market events today"

(JNJ 07-15, GE 07-16 and HON 07-23 also reported pre-market but the host was
down, so those are outage, not this defect. AVGO 06-03 and GOOGL 07-22 are
after-close and out of this feature's scope.)

The fix is deliberately *not* a new alert. It mirrors the food-supply fix: an
edge-triggered check that says nothing while a condition persists gets the
**state printed beside the edge**. An exhausted calendar is a distinct,
detectable condition from a quiet day, so the daily line now says which one it
is. No Discord push, no ntfy — a standing daily alarm reads as background noise
and ends up in the same place as no guard at all.

⚠️ Two things these tests are built to prevent:

- **A checker that can only ever return one answer.** Several tests below are
  positive controls: they prove `calendar_coverage` can return LIVE, can return
  a non-zero upcoming count, and that the ordinary quiet-day line still appears.
  A stub returning EXHAUSTED unconditionally must fail.
- **An assertion that stops one layer short.** The producer being right is not
  the point; the *log line the scheduler emits* is the artifact. So the last
  tests drive `_run_premarket_earnings_check` itself and assert on what it
  logged.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from src.portfolio.earnings_calendar import (
    EMPTY,
    EXHAUSTED,
    LIVE,
    calendar_coverage,
    get_todays_premarket_events,
)


@pytest.fixture
def calendar_file(tmp_path, monkeypatch):
    """Point the module at a throwaway calendar and return a writer for it."""
    path = tmp_path / "earnings_calendar.json"
    monkeypatch.setenv("EARNINGS_CALENDAR_PATH", str(path))

    def write(calendar):
        path.write_text(json.dumps(calendar, indent=2))
        return path

    write.path = path
    return write


# ── coverage states ──────────────────────────────────────────────


def test_missing_file_is_empty(calendar_file, monkeypatch):
    monkeypatch.setenv("EARNINGS_CALENDAR_PATH", str(calendar_file.path))
    # deliberately do NOT create the file
    cov = calendar_coverage(reference_date=date(2026, 7, 26))
    assert cov.state == EMPTY
    assert cov.last_date is None
    assert cov.upcoming == 0
    assert cov.days_stale is None


def test_calendar_with_no_dated_entries_is_empty(calendar_file):
    calendar_file({})
    cov = calendar_coverage(reference_date=date(2026, 7, 26))
    assert cov.state == EMPTY


def test_last_entry_in_the_past_is_exhausted(calendar_file):
    """The real production state on 2026-07-26."""
    calendar_file({
        "2026-04-30": [{"ticker": "LLY", "time": "pre-market"}],
        "2026-05-20": [{"ticker": "NVDA", "time": "after-close"}],
    })
    cov = calendar_coverage(reference_date=date(2026, 7, 26))
    assert cov.state == EXHAUSTED
    assert cov.last_date == date(2026, 5, 20)
    assert cov.days_stale == 67
    assert cov.upcoming == 0


def test_entry_dated_today_is_live_not_exhausted(calendar_file):
    """Boundary: today counts as covered. Off-by-one here would report a
    calendar as exhausted on the very day it is being used."""
    calendar_file({"2026-07-26": [{"ticker": "GEV", "time": "pre-market"}]})
    cov = calendar_coverage(reference_date=date(2026, 7, 26))
    assert cov.state == LIVE
    assert cov.days_stale is None


def test_entry_dated_tomorrow_is_live(calendar_file):
    calendar_file({"2026-07-27": [{"ticker": "MSFT", "time": "after-close"}]})
    cov = calendar_coverage(reference_date=date(2026, 7, 26))
    assert cov.state == LIVE


# ── positive controls: the checker must be able to return non-zero ──


def test_upcoming_counts_events_not_dates(calendar_file):
    """POSITIVE CONTROL. Two events share one date; a stub returning 0, or one
    counting date keys, both fail here."""
    calendar_file({
        "2026-07-28": [
            {"ticker": "GOOGL", "time": "after-close"},
            {"ticker": "GEV", "time": "pre-market"},
        ],
        "2026-08-05": [{"ticker": "LLY", "time": "pre-market"}],
    })
    cov = calendar_coverage(reference_date=date(2026, 7, 26))
    assert cov.state == LIVE
    assert cov.upcoming == 3
    assert cov.last_date == date(2026, 8, 5)


def test_upcoming_excludes_past_events_but_state_stays_live(calendar_file):
    calendar_file({
        "2026-01-05": [{"ticker": "OLD", "time": "pre-market"}],
        "2026-08-05": [{"ticker": "LLY", "time": "pre-market"}],
    })
    cov = calendar_coverage(reference_date=date(2026, 7, 26))
    assert cov.state == LIVE
    assert cov.upcoming == 1


def test_unparseable_date_keys_are_ignored(calendar_file):
    calendar_file({
        "not-a-date": [{"ticker": "XXX", "time": "pre-market"}],
        "2026-08-05": [{"ticker": "LLY", "time": "pre-market"}],
    })
    cov = calendar_coverage(reference_date=date(2026, 7, 26))
    assert cov.state == LIVE
    assert cov.last_date == date(2026, 8, 5)


def test_only_unparseable_keys_is_empty_not_a_crash(calendar_file):
    calendar_file({"not-a-date": [{"ticker": "XXX", "time": "pre-market"}]})
    cov = calendar_coverage(reference_date=date(2026, 7, 26))
    assert cov.state == EMPTY


def test_coverage_does_not_change_what_the_check_returns(calendar_file):
    """Coverage is reporting only. An exhausted calendar must still return []
    for today, and a live one must still return its events."""
    calendar_file({"2026-05-20": [{"ticker": "NVDA", "time": "pre-market"}]})
    assert get_todays_premarket_events(reference_date=date(2026, 7, 26)) == []
    assert len(get_todays_premarket_events(reference_date=date(2026, 5, 20))) == 1


# ── the artifact: what the scheduler actually logs ───────────────


def _run_check_capturing_logs(monkeypatch):
    """Drive the real scheduler function, returning every message it logged."""
    from loguru import logger

    import src.scheduler as scheduler

    messages: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda m: messages.append((m.record["level"].name, m.record["message"])),
        level="DEBUG",
    )
    try:
        scheduler._run_premarket_earnings_check()
    finally:
        logger.remove(sink_id)
    return messages


def test_exhausted_calendar_says_so_in_the_log(calendar_file, monkeypatch):
    """The whole point. A quiet day and a dead calendar must not produce the
    same sentence."""
    calendar_file({"2026-05-20": [{"ticker": "NVDA", "time": "pre-market"}]})
    messages = _run_check_capturing_logs(monkeypatch)

    joined = " | ".join(m for _, m in messages)
    assert "EXHAUSTED" in joined, joined
    assert "2026-05-20" in joined, joined
    # and it must be distinguishable at a glance, not buried at INFO
    assert any(lvl == "WARNING" and "EXHAUSTED" in msg for lvl, msg in messages), messages


def test_live_calendar_quiet_day_keeps_the_ordinary_line(calendar_file, monkeypatch):
    """POSITIVE CONTROL for the log path. A fix that shouted on every run would
    pass the test above and fail here — which is the failure mode that turns a
    guard into background noise."""
    future = date(date.today().year + 1, 1, 5).isoformat()
    calendar_file({future: [{"ticker": "LLY", "time": "pre-market"}]})
    messages = _run_check_capturing_logs(monkeypatch)

    joined = " | ".join(m for _, m in messages)
    assert "no pre-market events today" in joined, joined
    assert "EXHAUSTED" not in joined, joined
    assert not any(lvl == "WARNING" for lvl, _ in messages), messages
    # the quiet line should still say how much runway is left
    assert "1 upcoming" in joined, joined


def test_empty_calendar_says_never_populated(calendar_file, monkeypatch):
    calendar_file({})
    messages = _run_check_capturing_logs(monkeypatch)
    joined = " | ".join(m for _, m in messages)
    assert "EXHAUSTED" in joined or "no events have ever been added" in joined, joined
    assert any(lvl == "WARNING" for lvl, _ in messages), messages
