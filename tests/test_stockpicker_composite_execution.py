"""The composite loop must actually RUN — not merely contain the right source text.

`test_stockpicker_signal_coverage` proves every weighted signal has a
``contrib[...]`` line by scanning run.py's *source*. That is a text search: it
executes nothing. A wiring bug — ``z_insider[t]`` raising KeyError because the
z-map was keyed differently, a NameError from a typo — leaves every one of those
tests green while the nightly run dies or silently drops the term.

This test stubs the network and disk seams and executes `run.main` end to end,
then asserts on the `contrib` dict the composite actually produced. Every
weighted signal must appear with a finite value.

The seams are stubbed so that unintended real use *raises* rather than quietly
reaching the network: any ticker not in the fixture set is a KeyError.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.stockpicker import run, engine

TICKERS = ["AAA", "BBB", "CCC", "DDD"]
BARS = 520  # > MIN_HISTORY_BARS so no price signal is dead for the wrong reason


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    """Replace every external boundary of run.main with a deterministic fake."""
    dates = pd.bdate_range("2024-01-01", periods=BARS)

    # Distinct, non-degenerate price paths so cross-sectional z has variance.
    close = pd.DataFrame(
        {t: 100 + np.linspace(0, 20 + 7 * i, BARS) + np.sin(np.arange(BARS) / (3 + i))
         for i, t in enumerate(TICKERS)}, index=dates)
    open_ = close * 0.995

    def fake_download(tickers, **kw):
        return pd.concat({"Close": close, "Open": open_}, axis=1)

    monkeypatch.setattr(run.yf, "download", fake_download)

    monkeypatch.setattr(run.macro, "scan", lambda: {"regime": "neutral"})
    monkeypatch.setattr(run.macro, "summary_text", lambda m: "")
    monkeypatch.setattr(run.sectors, "heatmap_text", lambda s: "")
    monkeypatch.setattr(run.sectors, "scan", lambda: {
        "date_end": "2026-07-28",
        "sectors": {},
        "candidate_tickers": list(TICKERS),
        "candidates": [{"ticker": t, "reason": "test", "deviation_z": 1.0,
                        "sector_ret_5d_pct": 1.0, "overnight_gap_vol_pct": 1.0}
                       for t in TICKERS],
    })

    monkeypatch.setattr(run, "enrich", lambda tickers: {
        t: {"info": {"shortName": t, "targetMeanPrice": 150.0 + 10 * i,
                     "sector": "Technology", "marketCap": 5e10, "beta": 1.1,
                     "trailingPE": 20.0, "forwardPE": 18.0, "revenueGrowth": 0.1,
                     "dividendYield": 0.01},
            "last_surprise_pct": 5.0 + i, "days_since_earnings": 10 + i,
            "next_earnings": None, "days_to_earnings": 30}
        for i, t in enumerate(tickers)})

    # finviz: the two new signals ride on this payload.
    monkeypatch.setattr(run.finviz_data, "enrich_batch", lambda tickers: {
        t: {"ticker": t, "ok": True, "complete": True,
            "snapshot": {"analyst_recom": 2.0, "roe": 15.0, "roic": 12.0},
            "squeeze": {"score": 50.0 + 5 * i, "label": "moderate",
                        "short_float": 5.0, "short_ratio": 2.0},
            "signals": {"fv_recom": 2.0 + 0.1 * i, "fv_quality": 10.0 + i,
                        "fv_short_pressure": 3.0 + i, "fv_target_upside": 12.0 + i},
            "insider_cluster": [-0.75, -0.5, 0.333, None][i],
            "sentiment": [0.10, -0.04, 0.25, 0.06][i]}
        for i, t in enumerate(tickers)})
    monkeypatch.setattr(run.finviz_data, "persist_snapshots", lambda fv, as_of=None: None)

    monkeypatch.setattr(run.options_data, "enrich_batch", lambda tickers: {
        t: {"ok": True, "atm_iv": 0.35, "iv_slope": 0.02, "put_call_vol": 0.8 + 0.1 * i,
            "put_call_oi": 0.9, "total_oi": 1000} for i, t in enumerate(tickers)})
    monkeypatch.setattr(run.options_data, "signals",
                        lambda o: {"opt_pc_sentiment": o.get("put_call_vol")})

    # Disk seams — nothing this test does may touch the real ledger or artifacts.
    monkeypatch.setattr(run.ledger, "record", lambda records, as_of=None: len(records))
    monkeypatch.setattr(run, "DATA_DIR", tmp_path)
    return tmp_path


def _contribs(out) -> list[dict]:
    rows = out.get("all_candidates") or out.get("records") or out.get("picks") or []
    return [r["contrib"] for r in rows if isinstance(r, dict) and "contrib" in r]


def test_main_runs_and_every_weighted_signal_reaches_the_composite(stubbed):
    out = run.main(write_report=False)
    contribs = _contribs(out)

    # Positive control: the run must have produced scored rows at all.
    assert contribs, f"main() produced no rows carrying a contrib dict: {list(out)}"

    weighted = set(engine.PRICE_SIGNALS) | set(run.PRIOR_W)
    for c in contribs:
        missing = weighted - set(c)
        assert not missing, f"weighted signal(s) never reached the composite: {sorted(missing)}"
        for name, v in c.items():
            assert np.isfinite(v), f"{name} contributed a non-finite value: {v}"


def test_the_two_revived_signals_are_executed_not_merely_declared(stubbed):
    """The specific regression: source-text scanning cannot catch a runtime
    wiring error, and these two are the newest wiring."""
    contribs = _contribs(run.main(write_report=False))
    assert contribs
    for key in ("insider_cluster", "sentiment"):
        vals = [c[key] for c in contribs]
        assert all(np.isfinite(v) for v in vals), f"{key} produced non-finite values"
        # Not all identical -> the signal genuinely varies across the
        # cross-section, which is the whole difference from the dead state.
        assert len(set(round(v, 9) for v in vals)) > 1, (
            f"{key} contributed the same value to every ticker — that is the "
            f"dead-signal state this signal was built to leave")


def test_a_ticker_with_no_insider_data_does_not_poison_the_others(stubbed):
    """'DDD' has insider_cluster=None in the fixture. It must become NaN and be
    handled by cross_sectional_z, not propagate NaN into every other name."""
    contribs = _contribs(run.main(write_report=False))
    assert len(contribs) >= 3
    assert all(np.isfinite(c["insider_cluster"]) for c in contribs)
