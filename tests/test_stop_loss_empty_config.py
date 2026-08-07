"""A green stop-loss all-clear must mean "checked and safe", never "checked nothing".

Found 2026-08-07 (Dopple autonomous block 14) by running the tool against Nick's
real book while answering his 2026-05-12 ask about placing stops at Fidelity:

    $ python -m src.cli check-stops
    INFO | No positions with stop_loss_pct configured — nothing to check
    ✅ No stop-loss triggers — all positions within thresholds [static mode]

``run_stop_loss_check`` returns ``[]`` both when nothing is configured and when
everything is inside its threshold, and the CLI rendered the same green line for
both. Nothing schedules the check today, so this never misled anyone — the hazard
is the obvious next step: scheduling it unfixed installs a daily green light over
an unprotected portfolio.

The control in ``test_all_clear_still_prints_when_thresholds_exist`` is the point
of the file. A fix that simply stops printing the all-clear would pass every
other test here and would be worthless: a signal that can never say "safe" is not
a safer signal, it is a dead one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from src.alerts.stop_loss import count_evaluable_positions
from src.cli import cli

NO_STOP_COLUMN = "ticker,shares,cost_basis,purchase_date\nAAPL,10,150.0,\nMSFT,5,300.0,\n"
COLUMN_ALL_BLANK = (
    "ticker,shares,cost_basis,stop_loss_pct\nAAPL,10,150.0,\nMSFT,5,300.0,\n"
)
TWO_CONFIGURED = (
    "ticker,shares,cost_basis,stop_loss_pct\n"
    "AAPL,10,150.0,-0.15\n"
    "MSFT,5,300.0,-0.20\n"
    "GOOG,2,100.0,\n"
)

# The stable marker of the all-clear branch. Deliberately NOT the full sentence:
# the fix adds the evaluated count to it, and an assertion pinned to exact prose
# fails on a wording change while a real regression walks past. It still
# discriminates — the empty-config warning says "threshold in [mode]", never
# "within thresholds".
ALL_CLEAR = "within thresholds"


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "portfolio.csv"
    p.write_text(body, encoding="utf-8")
    return p


# ── count_evaluable_positions ────────────────────────────────────────────────


def test_static_count_is_zero_when_column_absent(tmp_path):
    """Nick's real portfolio.csv has no stop_loss_pct column at all."""
    assert count_evaluable_positions(_write(tmp_path, NO_STOP_COLUMN), "static") == 0


def test_static_count_is_zero_when_column_present_but_empty(tmp_path):
    """A present-but-blank column is the same emptiness, and looks more configured."""
    assert count_evaluable_positions(_write(tmp_path, COLUMN_ALL_BLANK), "static") == 0


def test_static_count_matches_configured_rows(tmp_path):
    """Positive control: the counter can return non-zero, and counts only real rows."""
    assert count_evaluable_positions(_write(tmp_path, TWO_CONFIGURED), "static") == 2


@pytest.mark.parametrize("mode", ["dynamic", "hybrid"])
def test_dynamic_modes_count_every_position(tmp_path, mode):
    """Dynamic modes compute a threshold per position, so the column is irrelevant."""
    assert count_evaluable_positions(_write(tmp_path, NO_STOP_COLUMN), mode) == 2


def test_dynamic_count_is_zero_for_an_empty_book(tmp_path):
    """An empty CSV is genuinely nothing to check in every mode."""
    body = "ticker,shares,cost_basis,purchase_date\n"
    assert count_evaluable_positions(_write(tmp_path, body), "dynamic") == 0


def test_unknown_mode_does_not_silently_report_zero(tmp_path):
    """A typo'd mode must raise, not return 0 — 0 is the value that suppresses the alarm."""
    with pytest.raises(ValueError):
        count_evaluable_positions(_write(tmp_path, TWO_CONFIGURED), "statik")


# ── the CLI surface ──────────────────────────────────────────────────────────


def test_no_all_clear_when_nothing_is_configured(tmp_path):
    csv_path = _write(tmp_path, NO_STOP_COLUMN)
    result = CliRunner().invoke(
        cli, ["check-stops", "-p", str(csv_path), "--no-alert"]
    )
    assert result.exit_code == 0, result.output
    assert ALL_CLEAR not in result.output


def test_empty_config_says_so_and_names_the_count(tmp_path):
    """Silence would be a second way to look fine. It has to say what is wrong."""
    csv_path = _write(tmp_path, NO_STOP_COLUMN)
    result = CliRunner().invoke(
        cli, ["check-stops", "-p", str(csv_path), "--no-alert"]
    )
    out = result.output.lower()
    assert "0 of 2" in out
    assert "not protected" in out


def test_all_clear_still_prints_when_thresholds_exist(tmp_path, monkeypatch):
    """THE CONTROL. A signal that can never say 'safe' is dead, not safe.

    Two positions are configured and neither is breached, so the green line is
    the correct output and must survive the fix.
    """
    from decimal import Decimal

    import src.alerts.stop_loss as sl

    monkeypatch.setattr(
        sl, "_fetch_prices", lambda tickers: {t: Decimal("1000") for t in tickers}
    )
    csv_path = _write(tmp_path, TWO_CONFIGURED)
    result = CliRunner().invoke(
        cli, ["check-stops", "-p", str(csv_path), "--no-alert"]
    )
    assert result.exit_code == 0, result.output
    assert ALL_CLEAR in result.output


def test_breach_still_reported_when_thresholds_exist(tmp_path, monkeypatch):
    """Second control: the trigger path is untouched."""
    from decimal import Decimal

    import src.alerts.stop_loss as sl

    monkeypatch.setattr(
        sl, "_fetch_prices", lambda tickers: {t: Decimal("1") for t in tickers}
    )
    csv_path = _write(tmp_path, TWO_CONFIGURED)
    result = CliRunner().invoke(
        cli, ["check-stops", "-p", str(csv_path), "--no-alert"]
    )
    assert "stop-loss trigger" in result.output
    assert ALL_CLEAR not in result.output
