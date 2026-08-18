"""Downloaded positions exports land in one folder under a canonical name.

Chrome will not overwrite an existing download, so every scrape dropped another
numbered copy into ~/Downloads: five `Portfolio_Positions_Aug-18-2026 (N).csv`
files accumulated in a single session. Beyond the clutter, the glob resolver then
has to tie-break near-identical files by mtime to decide which one is real.

Same date means same snapshot, so the archive keeps one canonical file per export
date and moves superseded copies aside rather than deleting them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.portfolio.fidelity_scraper import _archive_downloaded_csv


def _make(tmp_path: Path, name: str, content: str = "x") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_moves_download_into_export_dir(tmp_path):
    dl = _make(tmp_path, "Portfolio_Positions_Aug-18-2026.csv", "fresh")
    export = tmp_path / "exports"

    archived = _archive_downloaded_csv(dl, export_dir=export)

    assert archived == export / "Portfolio_Positions_Aug-18-2026.csv"
    assert archived.read_text() == "fresh"
    assert not dl.exists(), "download should be moved, leaving Downloads clean"


def test_numbered_duplicate_is_normalised_to_canonical_name(tmp_path):
    dl = _make(tmp_path, "Portfolio_Positions_Aug-18-2026 (4).csv", "fourth")
    export = tmp_path / "exports"

    archived = _archive_downloaded_csv(dl, export_dir=export)

    assert archived.name == "Portfolio_Positions_Aug-18-2026.csv"
    assert archived.read_text() == "fourth"


def test_preexisting_same_date_file_is_superseded_not_duplicated(tmp_path):
    export = tmp_path / "exports"
    export.mkdir()
    existing = export / "Portfolio_Positions_Aug-18-2026.csv"
    existing.write_text("old", encoding="utf-8")

    dl = _make(tmp_path, "Portfolio_Positions_Aug-18-2026.csv", "new")
    archived = _archive_downloaded_csv(dl, export_dir=export)

    assert archived.read_text() == "new"
    # exactly one canonical file for that date
    assert len(list(export.glob("Portfolio_Positions_Aug-18-2026.csv"))) == 1
    # and the previous content is preserved, not destroyed
    superseded = list((export / "superseded").glob("*.csv"))
    assert len(superseded) == 1
    assert superseded[0].read_text() == "old"


def test_different_dates_coexist(tmp_path):
    export = tmp_path / "exports"
    _archive_downloaded_csv(_make(tmp_path, "Portfolio_Positions_Jun-24-2026.csv", "a"), export_dir=export)
    _archive_downloaded_csv(_make(tmp_path, "Portfolio_Positions_Aug-18-2026.csv", "b"), export_dir=export)

    assert len(list(export.glob("Portfolio_Positions_*.csv"))) == 2


def test_undated_filename_is_left_alone(tmp_path):
    """Don't invent a canonical name we can't derive."""
    dl = _make(tmp_path, "weird_export.csv", "z")
    export = tmp_path / "exports"

    archived = _archive_downloaded_csv(dl, export_dir=export)

    assert archived == dl, "unrecognised names stay where they are"
    assert dl.exists()


def test_resolver_finds_the_archived_export(tmp_path):
    """The archive folder must work with the glob resolver end to end."""
    from src.portfolio.fidelity_reader import resolve_positions_path

    export = tmp_path / "exports"
    _archive_downloaded_csv(_make(tmp_path, "Portfolio_Positions_Jun-24-2026.csv", "a"), export_dir=export)
    _archive_downloaded_csv(_make(tmp_path, "Portfolio_Positions_Aug-18-2026.csv", "b"), export_dir=export)

    resolved = resolve_positions_path(str(export / "Portfolio_Positions_*.csv"))

    assert resolved.name == "Portfolio_Positions_Aug-18-2026.csv"


def test_superseded_copies_are_not_picked_by_the_resolver(tmp_path):
    """A stale superseded/ copy must never win the glob."""
    from src.portfolio.fidelity_reader import resolve_positions_path

    export = tmp_path / "exports"
    _archive_downloaded_csv(_make(tmp_path, "Portfolio_Positions_Aug-18-2026.csv", "old"), export_dir=export)
    _archive_downloaded_csv(_make(tmp_path, "Portfolio_Positions_Aug-18-2026.csv", "new"), export_dir=export)

    resolved = resolve_positions_path(str(export / "Portfolio_Positions_*.csv"))

    assert resolved.read_text() == "new"
    assert resolved.parent == export
