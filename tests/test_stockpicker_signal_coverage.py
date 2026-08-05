"""Signal coverage guards for the stock picker.

Background — why this file exists
---------------------------------
`momentum_12_1` carries ~9.6% of the weight vector and contributed **exactly
0.0** to every one of the 706 scored predictions in the ledger, across all 16
run dates. Not noise, not a small effect: zero variance, so it could never
change a single ranking.

The chain, verified end to end on 2026-07-26:

  1. `run.main` downloaded ``period="1y"`` -> **251 bars** -> ``t_now = 250``.
  2. `sig_momentum_12_1` guards ``if t < 252: return Series(nan)``.
     250 < 252, so it returned all-NaN. It missed by **two bars**.
  3. `cross_sectional_z` turns an all-NaN series into ``Series(0.0)`` (its
     ``scale`` is NaN, so it short-circuits) — a confident zero, not a NaN.
  4. ``contrib["momentum_12_1"] = w * 0.0 == 0.0``, logged as a real number.

Step 3 is what made it invisible: a missing signal is indistinguishable from a
signal that legitimately scored flat. Meanwhile `engine.ic_backtest` — which
*derived* the weights — downloads ``period="2y"``, so the weights were fit on a
signal that production never actually computed.

The existing suite could not catch this: eight tests use `momentum_12_1` as
their canonical example signal, and every one of them supplies the contribution
by hand. Fixtures are not the artifact.
"""

import numpy as np
import pandas as pd
import pytest

from src.stockpicker import engine, run


# --------------------------------------------------------------------------
# The lookback requirement, stated as a boundary rather than a magic number
# --------------------------------------------------------------------------

def _frame(n_bars: int, n_tickers: int = 6, seed: int = 0) -> pd.DataFrame:
    """A price frame of `n_bars` rows that is genuinely non-constant."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0005, 0.02, size=(n_bars, n_tickers))
    prices = 100 * np.exp(np.cumsum(steps, axis=0))
    return pd.DataFrame(prices, columns=[f"T{i}" for i in range(n_tickers)])


def test_momentum_is_all_nan_one_bar_below_the_boundary():
    """t == 251 is not enough: the signal reads close.iloc[t - 252]."""
    close = _frame(252)
    out = engine.sig_momentum_12_1(close, t=251)
    assert out.isna().all(), "expected all-NaN below the 252-bar boundary"


def test_momentum_is_finite_at_the_boundary():
    close = _frame(253)
    out = engine.sig_momentum_12_1(close, t=252)
    assert np.isfinite(out.astype(float)).all(), "expected finite values at t == 252"


def test_one_year_of_history_cannot_compute_momentum():
    """The exact production failure: a 1y download yields t_now == 250."""
    close = _frame(251)          # measured: yfinance period="1y" -> 251 bars
    out = engine.sig_momentum_12_1(close, t=len(close) - 1)
    assert out.isna().all()


# --------------------------------------------------------------------------
# The fail-open that hid it
# --------------------------------------------------------------------------

def test_cross_sectional_z_of_all_nan_is_a_confident_zero():
    """Pinned deliberately. This is *why* the bug was invisible for 16 runs.

    Anything downstream reads 0.0 and cannot tell it from a real flat score.
    Detection therefore has to happen before this call, not after it.
    """
    z = engine.cross_sectional_z(pd.Series([np.nan] * 5, index=list("abcde")))
    assert (z == 0.0).all()
    assert not z.isna().any()


# --------------------------------------------------------------------------
# Detection: which signals came back empty on the real frame
# --------------------------------------------------------------------------

def test_all_nan_signals_names_a_dead_signal():
    sigs = {
        "alive": pd.Series([1.0, 2.0, 3.0]),
        "dead": pd.Series([np.nan, np.nan, np.nan]),
    }
    assert engine.all_nan_signals(sigs) == ["dead"]


def test_all_nan_signals_returns_empty_when_everything_is_live():
    """The zero-control. A checker never shown able to return non-zero is not a
    measurement — the test above supplies the non-zero case."""
    sigs = {"a": pd.Series([1.0, 2.0]), "b": pd.Series([0.0, 1.0])}
    assert engine.all_nan_signals(sigs) == []


def test_all_nan_signals_treats_partial_data_as_live():
    """One usable name is still a signal; only a total absence is a defect."""
    sigs = {"partial": pd.Series([np.nan, 4.0, np.nan])}
    assert engine.all_nan_signals(sigs) == []


# --------------------------------------------------------------------------
# The production window must satisfy every signal, not just most of them
# --------------------------------------------------------------------------

def _battery(n_bars: int) -> dict:
    close = _frame(n_bars, seed=3)
    open_ = close * 1.001
    return engine.compute_price_signals_at(close, open_, t=n_bars - 1)


def test_min_history_bars_is_the_measured_boundary_not_a_guessed_number():
    """MIN_HISTORY_BARS must be the *smallest* frame at which nothing flatlines.

    Asserting the relationship rather than the literal: one bar fewer has to
    lose a signal, or the constant is loose and a future short window slips
    through the check in run.main.
    """
    assert engine.all_nan_signals(_battery(engine.MIN_HISTORY_BARS)) == []
    assert engine.all_nan_signals(_battery(engine.MIN_HISTORY_BARS - 1)) != []


def test_history_period_is_long_enough_for_every_price_signal():
    """RED against period="1y" (251 bars < 253 required)."""
    bars = run.PERIOD_BARS[run.HISTORY_PERIOD]
    assert bars >= engine.MIN_HISTORY_BARS, (
        f"run downloads {run.HISTORY_PERIOD} ({bars} bars) but the price "
        f"signals need {engine.MIN_HISTORY_BARS}"
    )


def test_production_window_computes_every_price_signal():
    """The whole battery on a frame the size production actually fetches.

    Asserts a *positive*: every declared price signal produces at least one
    finite value and real cross-sectional spread. A signal that silently
    flatlines fails here.
    """
    n = run.PERIOD_BARS[run.HISTORY_PERIOD]
    close = _frame(n, seed=1)
    open_ = close * (1 + np.random.default_rng(2).normal(0, 0.005, close.shape))
    sigs = engine.compute_price_signals_at(close, open_, t=n - 1)

    assert set(sigs) == set(engine.PRICE_SIGNALS)
    assert engine.all_nan_signals(sigs) == []
    for name in engine.PRICE_SIGNALS:
        z = engine.cross_sectional_z(sigs[name])
        assert z.std() > 0, f"{name} is constant across the cross-section"


def test_one_year_window_would_flatline_momentum():
    """Guards the fix from being reverted: at 1y the battery *does* lose a
    signal, so the test above is not vacuously true."""
    close = _frame(PERIOD_BARS_1Y := 251, seed=1)
    open_ = close * 1.001
    sigs = engine.compute_price_signals_at(close, open_, t=PERIOD_BARS_1Y - 1)
    assert "momentum_12_1" in engine.all_nan_signals(sigs)


# --------------------------------------------------------------------------
# Weighted-but-never-computed signals
# --------------------------------------------------------------------------

def test_every_weighted_signal_has_a_contribution_line():
    """No signal may carry weight without contributing to the composite.

    `insider_cluster` and `sentiment` were the last two holdouts: together
    10.9% of the effective (PRIOR_W) weight vector, present in zero of the 706
    scored ledger rows because no ``contrib[...]`` line existed for either.
    Both are now computed in `insider_news` from the finviz page run.py already
    fetches. This test is the standing guard — it fails if a *new* weighted
    signal is ever added without being wired in.
    """
    emitted = run.emitted_signal_names()

    # Positive control: the scanner must actually see the contribution lines.
    assert len(emitted) >= 8, f"scanner found only {len(emitted)} — it is broken"
    assert "short_squeeze" in emitted and "momentum_12_1" in emitted

    weighted = set(engine.PRICE_SIGNALS) | set(run.PRIOR_W)
    assert weighted - emitted == set(), (
        f"weighted but never computed: {sorted(weighted - emitted)}")


def test_the_two_revived_signals_are_specifically_present():
    """Named explicitly so the guard above cannot be satisfied by deleting a
    weight instead of computing the signal."""
    emitted = run.emitted_signal_names()
    assert "insider_cluster" in emitted
    assert "sentiment" in emitted
    assert "insider_cluster" in run.PRIOR_W and "sentiment" in run.PRIOR_W


# --------------------------------------------------------------------------
# The accuracy report must not claim weights are used when they are not
# --------------------------------------------------------------------------

def _weights_stub(using_measured: bool) -> dict:
    return {
        "generated_at": "2026-07-25", "horizon": "r5",
        "max_obs_per_signal": 12, "using_measured": using_measured,
        "weights": {"momentum_12_1": 1.0},
        "rows": [{"signal": "momentum_12_1", "ic_measured": 0.1, "ic_prior": 0.045,
                  "n": 12, "lambda_prior": 0.8, "ic_shrunk": 0.05,
                  "hit_rate": 0.55, "weight": 1.0}],
    }


def test_report_does_not_claim_prior_dominated_weights_are_in_use():
    """`load_weights` gates on using_measured; while it is False the pipeline
    reads the universe_scores cache instead and this file is ignored."""
    from src.stockpicker import accuracy
    text = accuracy._running_weights_section(_weights_stub(using_measured=False))
    assert "NOT yet used" in text
    assert "Used by the next pipeline run." not in text


def test_report_does_claim_measured_weights_are_in_use():
    """Zero-control: the honest branch must still be reachable.

    This asserted the literal "Used by the next pipeline run." — which was the
    overclaim itself: true of the file, false of the 7 rows the composite pins
    to PRIOR_W. The control property (the measured branch is reachable and is
    distinguishable from the prior-dominated one) is what mattered and is kept;
    see tests/test_weight_application.py for the per-signal assertions.
    """
    from src.stockpicker import accuracy
    text = accuracy._running_weights_section(_weights_stub(using_measured=True))
    assert "by the next pipeline run" in text
    assert "NOT yet used" not in text


# --------------------------------------------------------------------------
# A signal introduced part-way through the ledger crashed signal_stats
# --------------------------------------------------------------------------
#
# `signal_stats` takes its column list from the WHOLE scored frame, then walks
# the ledger date by date and re-derives a per-date frame with
# `_signal_frame(g)`. `pd.json_normalize` only emits columns for keys present in
# the rows it is given, so any date predating a signal's introduction has no
# such column -- and `_signal_frame(g)[col]` raises KeyError.
#
# This is not hypothetical. Measured 2026-07-26 against the real ledger:
#
#     accuracy.signal_stats("r1")  -> KeyError: 'revision_proxy'
#     accuracy.signal_stats("r5")  -> fine
#
# The only difference is coverage. `revision_proxy` (with `earnings_sue`,
# `pead_drift` and the finviz signals) was restored by 1686637 and first appears
# on 2026-07-20. `r1` is realized for 07-20, so the column enters the frame and
# the 06-26 group blows up. `r5` for 07-20 is not realized yet, so that date is
# filtered out and the column never appears at all.
#
# DEFAULT_HORIZON is "r5" -- the horizon `running_weights` and the nightly
# scorecard use. So the default path breaks the moment 07-20's 5-day return
# resolves, and `scripts/stockpicker-nightly.sh` runs that step under
# `|| echo "  (accuracy non-fatal error)"`: the traceback would be swallowed,
# `running_weights.json` would quietly stop updating, and the pipeline would
# keep running on stale weights.
#
# Introducing a signal is a NORMAL event -- it is how every signal here began.

def _ledger_rows(dates, signals_by_date, n_tickers=6, seed=0):
    """Synthetic scored rows. `signals_by_date` maps date -> list of signal names."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in dates:
        for i in range(n_tickers):
            contrib = {s: float(rng.normal()) for s in signals_by_date[d]}
            rows.append({
                "as_of": f"{d}T00:00:00", "ticker": f"T{i}",
                "composite": float(sum(contrib.values())),
                "contrib": contrib,
                "r1": float(rng.normal()), "r5": float(rng.normal()),
            })
    return rows


def _patch_ledger(monkeypatch, rows):
    from src.stockpicker import accuracy as _acc
    monkeypatch.setattr(_acc.ledger, "_read_jsonl", lambda p: rows)


ALL_DATES = ["2026-06-26", "2026-06-29", "2026-06-30", "2026-07-01"]


def test_signal_stats_positive_control_when_every_date_has_every_signal(monkeypatch):
    """Control: with uniform coverage the function returns real stats.

    Without this, a fix that made signal_stats return {} on any irregular input
    would pass the regression test below while measuring nothing at all.
    """
    from src.stockpicker import accuracy
    rows = _ledger_rows(ALL_DATES, {d: ["old_sig", "new_sig"] for d in ALL_DATES})
    _patch_ledger(monkeypatch, rows)

    stats = accuracy.signal_stats("r5")

    assert set(stats) == {"old_sig", "new_sig"}, f"expected both signals, got {set(stats)}"
    assert stats["new_sig"]["n"] == len(ALL_DATES), "every date should count as a cross-section"


def test_signal_stats_survives_a_signal_introduced_midway(monkeypatch):
    """The regression: `new_sig` exists only on the last two dates."""
    from src.stockpicker import accuracy
    by_date = {d: ["old_sig"] for d in ALL_DATES}
    for d in ALL_DATES[-2:]:
        by_date[d] = ["old_sig", "new_sig"]
    rows = _ledger_rows(ALL_DATES, by_date)
    _patch_ledger(monkeypatch, rows)

    stats = accuracy.signal_stats("r5")   # must not raise KeyError

    assert "old_sig" in stats, "the fully-covered signal must still be measured"
    # `n` counts INDEPENDENT CROSS-SECTIONS. The two dates that predate the
    # signal contributed no observation and must not be fabricated into one.
    assert stats["new_sig"]["n"] == 2, (
        f"new_sig should count only the 2 dates it exists on, got {stats['new_sig']['n']}")
    assert stats["old_sig"]["n"] == len(ALL_DATES)
