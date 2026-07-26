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

def test_weighted_signals_without_a_contribution_are_exactly_the_known_two():
    """`insider_cluster` and `sentiment` carry weight and are never computed.

    They hold 25.3% of the live `universe_scores` weight vector and appear in
    zero of the 706 ledger rows — there is no ``contrib[...]`` line for either
    one anywhere in run.py. That is a build, not a bug fix, so it is Nick's
    call; this test pins the set so a *third* one cannot appear silently.
    """
    emitted = run.emitted_signal_names()

    # Positive control: the scanner must actually see the contribution lines.
    assert len(emitted) >= 8, f"scanner found only {len(emitted)} — it is broken"
    assert "short_squeeze" in emitted and "momentum_12_1" in emitted

    weighted = set(engine.PRICE_SIGNALS) | set(run.PRIOR_W)
    assert weighted - emitted == {"insider_cluster", "sentiment"}
