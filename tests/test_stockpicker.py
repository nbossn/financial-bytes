"""
Unit tests for the stock-picker's pure logic (no network).

Covers the deterministic core: finviz squeeze scoring + signal derivation +
cap parsing, options IV/put-call signal derivation + IV cleaning, and the
confidence-matrix prior normalization. Network-bound fetches (finviz_data.fetch,
options_data.fetch_options) are exercised only via injected/fake data.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.stockpicker import finviz_data, options_data, confidence, ledger


# ─────────────────────────── finviz squeeze_score ───────────────────────────

def test_squeeze_high_setup_scores_high():
    snap = {"short_float": 25.0, "short_ratio": 8.0, "rsi": 70,
            "rel_volume": 2.5, "current_price_raw": 98, "high_52w": 100}
    out = finviz_data.squeeze_score(snap)
    assert out["score"] >= 60
    assert out["label"] == "high squeeze setup"


def test_squeeze_momentum_name_scores_low():
    # MU-like: tiny short float + sub-1-day cover => not a squeeze
    snap = {"short_float": 3.7, "short_ratio": 0.81, "rsi": 59,
            "rel_volume": 1.0, "current_price_raw": 1130, "high_52w": 1150}
    out = finviz_data.squeeze_score(snap)
    assert out["score"] < 35
    assert out["short_float"] == 3.7


def test_squeeze_clamped_0_100():
    snap = {"short_float": 999, "short_ratio": 999, "rsi": 100,
            "rel_volume": 99, "current_price_raw": 100, "high_52w": 100}
    out = finviz_data.squeeze_score(snap)
    assert 0.0 <= out["score"] <= 100.0


def test_squeeze_missing_fields_no_crash():
    out = finviz_data.squeeze_score({})
    assert out["score"] == 0.0
    assert out["label"] == "no squeeze"


def test_squeeze_rsi_below_50_adds_nothing():
    low = finviz_data.squeeze_score({"short_float": 10, "short_ratio": 2, "rsi": 40})
    none_rsi = finviz_data.squeeze_score({"short_float": 10, "short_ratio": 2})
    assert low["score"] == pytest.approx(none_rsi["score"])


# ─────────────────────────── finviz signals ───────────────────────────

def test_signals_recom_inverted_higher_is_bullish():
    buy = finviz_data.signals({"analyst_recom": 1.0})["fv_recom"]
    sell = finviz_data.signals({"analyst_recom": 5.0})["fv_recom"]
    assert buy > sell


def test_signals_target_upside():
    s = finviz_data.signals({"target_price": 150, "current_price_raw": 100})
    assert s["fv_target_upside"] == pytest.approx(50.0)


def test_signals_short_pressure_negative_for_high_short():
    s = finviz_data.signals({"short_float": 20.0})
    assert s["fv_short_pressure"] == -20.0


def test_signals_quality_is_mean_of_available():
    s = finviz_data.signals({"roe": 40, "roic": 20})  # margins absent
    assert s["fv_quality"] == pytest.approx(30.0)


# ─────────────────────────── _parse_cap ───────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("1.28T", 1.28e12),
    ("41.59M", 41.59e6),
    ("3.5B", 3.5e9),
    ("500K", 5.0e5),
    ("1,234", 1234.0),
])
def test_parse_cap(text, expected):
    assert finviz_data._parse_cap(text) == pytest.approx(expected)


def test_parse_cap_bad_input():
    assert finviz_data._parse_cap(None) is None
    assert finviz_data._parse_cap("n/a") is None
    assert finviz_data._parse_cap(42) is None


# ─────────────────────────── finviz_data.fetch retry (no real net) ──────────

def test_fetch_retries_then_gives_up(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(finviz_data, "_get_page_html", lambda url: calls.__setitem__("n", calls["n"] + 1) or None)
    monkeypatch.setattr(finviz_data.time, "sleep", lambda *_: None)  # don't actually wait
    out = finviz_data.fetch("FAKE", retries=3)
    assert out is None
    assert calls["n"] == 3  # exactly `retries` attempts


def test_fetch_succeeds_first_try(monkeypatch):
    monkeypatch.setattr(finviz_data, "_get_page_html", lambda url: "<html>x</html>")
    monkeypatch.setattr(finviz_data, "_parse_snapshot", lambda soup: {"pe": 10.0})
    monkeypatch.setattr(finviz_data.time, "sleep", lambda *_: None)
    assert finviz_data.fetch("MU") == {"pe": 10.0}


# ─────────────────────────── options signals + IV cleaning ──────────────────

def test_options_pc_sentiment_negated():
    s = options_data.signals({"put_call_vol": 2.5, "atm_iv": 0.9, "iv_slope": 0.04})
    assert s["opt_pc_sentiment"] == -2.5
    assert s["opt_iv_level"] == 0.9
    assert s["opt_event_premium"] == 0.04


def test_options_signals_handle_none():
    s = options_data.signals({})
    assert s["opt_pc_sentiment"] is None
    assert s["opt_iv_level"] is None


def test_clean_iv_drops_artifacts():
    s = pd.Series([1e-05, 9.88, 0.45, 0.50, np.nan])
    cleaned = options_data._clean_iv(s)
    # 1e-05 (too small) and 9.88 (too large) and NaN dropped; 0.45/0.50 kept
    assert sorted(cleaned.tolist()) == [0.45, 0.50]


def test_atm_iv_picks_near_spot():
    calls = pd.DataFrame({"strike": [50, 100, 150], "impliedVolatility": [0.30, 0.40, 0.90]})
    puts = pd.DataFrame({"strike": [50, 100, 150], "impliedVolatility": [0.32, 0.42, 0.88]})
    # spot 100: nsmallest(3) takes all three here, mean of calls and puts then averaged
    iv = options_data._atm_iv(calls, puts, spot=100.0)
    assert iv is not None and 0.3 < iv < 0.6


def test_fetch_options_no_options_listed(monkeypatch):
    class FakeTk:
        options = []
    out = options_data.fetch_options("NOPT", tk=FakeTk())
    assert out["ok"] is False
    assert out["n_expirations"] == 0
    assert out["greeks_available"] is False  # never faked


# ─────────────────────────── confidence priors ──────────────────────────

def test_priors_include_options_and_finviz_signals():
    assert "opt_pc_sentiment" in confidence.IC_PRIORS
    assert "short_squeeze" in confidence.IC_PRIORS
    assert confidence.IC_PRIORS["short_squeeze"] >= confidence.IC_PRIORS["short_pressure"]


def test_all_priors_positive():
    assert all(v > 0 for v in confidence.IC_PRIORS.values())


# ─────────────────────────── prediction ledger ──────────────────────────

@pytest.fixture
def tmp_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "PRED_PATH", tmp_path / "predictions.jsonl")
    monkeypatch.setattr(ledger, "SCORED_PATH", tmp_path / "scored.jsonl")
    return tmp_path


def _rec(ticker, comp, contrib):
    return {"ticker": ticker, "composite": comp, "last_close": 100.0,
            "risk_tier": "MODERATE", "contrib": contrib}


def test_ledger_record_writes_rows(tmp_ledger):
    n = ledger.record([_rec("AAA", 1.0, {"momentum_12_1": 0.5})], as_of="2026-06-01")
    assert n == 1
    rows = ledger._read_jsonl(ledger.PRED_PATH)
    assert len(rows) == 1 and rows[0]["ticker"] == "AAA"
    assert rows[0]["contrib"]["momentum_12_1"] == 0.5


def test_ledger_record_idempotent_per_date(tmp_ledger):
    ledger.record([_rec("AAA", 1.0, {}), _rec("BBB", 0.5, {})], as_of="2026-06-01")
    # re-running the same date replaces, doesn't duplicate
    ledger.record([_rec("AAA", 2.0, {})], as_of="2026-06-01")
    rows = ledger._read_jsonl(ledger.PRED_PATH)
    aaa = [r for r in rows if r["ticker"] == "AAA"]
    assert len(aaa) == 1 and aaa[0]["composite"] == 2.0
    assert not any(r["ticker"] == "BBB" for r in rows)  # old date wiped


def test_ledger_record_keeps_other_dates(tmp_ledger):
    ledger.record([_rec("AAA", 1.0, {})], as_of="2026-06-01")
    ledger.record([_rec("BBB", 1.0, {})], as_of="2026-06-02")
    dates = {r["as_of"] for r in ledger._read_jsonl(ledger.PRED_PATH)}
    assert dates == {"2026-06-01", "2026-06-02"}


def test_signal_ic_insufficient_rows_returns_empty(tmp_ledger):
    ledger._write_jsonl(ledger.SCORED_PATH,
                        [{"composite": 1.0, "r5": 0.02, "contrib": {"x": 0.1}}])
    assert ledger.signal_ic("r5") == {}
