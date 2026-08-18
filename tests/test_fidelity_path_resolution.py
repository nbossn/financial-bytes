"""Fidelity positions path resolution.

portfolios.json used to hardcode an absolute path to a dated export
(`Portfolio_Positions_Jun-24-2026.csv`). Nothing repointed it when a newer
export landed, so the pipeline ran on a 55-day-stale snapshot for weeks
while reporting current prices — a silent correctness failure.

The config may now carry a glob (`Portfolio_Positions_*.csv`) which resolves
to the newest export by the date embedded in the filename.
"""
from __future__ import annotations

import pytest

from src.portfolio.fidelity_reader import (
    STALE_EXPORT_DAYS,
    resolve_positions_path,
)

HEADER = (
    "Account number,Account name,Symbol,Description,Quantity,Last price,"
    "Last price change,Current value,Today's gain/loss dollar,"
    "Today's gain/loss percent,Total gain/loss dollar,Total gain/loss percent,"
    "Percent of account,Cost basis total,Average cost basis,Type"
)
ROW = (
    "Z38250874,Trust: Under Agreement,NVDA,NVIDIA CORPORATION COM,10,$200.00,"
    "$1.00,$2000.00,$10.00,0.50%,+$500.00,+33.33%,10.00%,$1500.00,$150.00,Cash,"
)


def _export(tmp_path, name: str):
    p = tmp_path / name
    p.write_text("﻿" + HEADER + "\n" + ROW + "\n", encoding="utf-8")
    return p


def test_plain_existing_path_is_returned_unchanged(tmp_path):
    """A concrete path with no glob is passed through untouched."""
    p = _export(tmp_path, "Portfolio_Positions_Jun-24-2026.csv")

    assert resolve_positions_path(str(p)) == p


def test_glob_picks_newest_by_filename_date(tmp_path):
    """Newest export wins — by the date in the name, not lexical order."""
    _export(tmp_path, "Portfolio_Positions_Jun-24-2026.csv")
    _export(tmp_path, "Portfolio_Positions_May-07-2026.csv")
    newest = _export(tmp_path, "Portfolio_Positions_Aug-18-2026.csv")

    resolved = resolve_positions_path(str(tmp_path / "Portfolio_Positions_*.csv"))

    assert resolved == newest


def test_lexical_order_would_pick_the_wrong_file(tmp_path):
    """Guard the actual trap: 'Aug' sorts before 'Jun' and 'May' as text."""
    _export(tmp_path, "Portfolio_Positions_Aug-18-2026.csv")
    dec = _export(tmp_path, "Portfolio_Positions_Dec-01-2026.csv")

    resolved = resolve_positions_path(str(tmp_path / "Portfolio_Positions_*.csv"))

    assert resolved == dec, "must compare parsed dates, not filename strings"


def test_same_day_duplicate_downloads_tie_break_on_mtime(tmp_path):
    """Fidelity writes 'name (1).csv', '(2)' … for repeat downloads same day."""
    first = _export(tmp_path, "Portfolio_Positions_Aug-18-2026.csv")
    latest = _export(tmp_path, "Portfolio_Positions_Aug-18-2026 (3).csv")

    import os

    os.utime(first, (1_000_000, 1_000_000))
    os.utime(latest, (2_000_000, 2_000_000))

    resolved = resolve_positions_path(str(tmp_path / "Portfolio_Positions_*.csv"))

    assert resolved == latest


def test_undated_filenames_fall_back_to_mtime(tmp_path):
    """Files whose names carry no parseable date still resolve, by mtime."""
    import os

    old = _export(tmp_path, "positions_old.csv")
    new = _export(tmp_path, "positions_new.csv")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    assert resolve_positions_path(str(tmp_path / "positions_*.csv")) == new


def test_glob_matching_nothing_raises(tmp_path):
    """A glob that matches nothing must fail loudly, not return None."""
    with pytest.raises(FileNotFoundError, match="No Fidelity positions export"):
        resolve_positions_path(str(tmp_path / "Portfolio_Positions_*.csv"))


def test_missing_concrete_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_positions_path(str(tmp_path / "nope.csv"))


@pytest.fixture
def loguru_warnings():
    """Collect loguru WARNING records — loguru does not propagate to caplog."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def test_stale_export_warns_but_still_resolves(tmp_path, loguru_warnings):
    """A stale newest-export is the Jun-24 failure mode — warn, don't fail."""
    from datetime import date, timedelta

    old_day = date.today() - timedelta(days=STALE_EXPORT_DAYS + 10)
    name = f"Portfolio_Positions_{old_day.strftime('%b-%d-%Y')}.csv"
    stale = _export(tmp_path, name)

    resolved = resolve_positions_path(str(tmp_path / "Portfolio_Positions_*.csv"))

    assert resolved == stale
    assert any("stale" in m.lower() for m in loguru_warnings)


def test_fresh_export_does_not_warn(tmp_path, loguru_warnings):
    from datetime import date

    name = f"Portfolio_Positions_{date.today().strftime('%b-%d-%Y')}.csv"
    _export(tmp_path, name)

    resolve_positions_path(str(tmp_path / "Portfolio_Positions_*.csv"))

    assert not any("stale" in m.lower() for m in loguru_warnings)


def test_reader_accepts_a_glob_end_to_end(tmp_path):
    """read_fidelity_positions resolves globs, so every call site benefits."""
    from src.portfolio.fidelity_reader import read_fidelity_positions

    _export(tmp_path, "Portfolio_Positions_Jun-24-2026.csv")
    _export(tmp_path, "Portfolio_Positions_Aug-18-2026.csv")

    holdings = read_fidelity_positions(
        str(tmp_path / "Portfolio_Positions_*.csv"), account_filter="Z38250874"
    )

    assert [h.ticker for h in holdings] == ["NVDA"]
