"""
finviz_data.py — Finviz as a datasource for the stock picker.

Finviz's quote snapshot (finviz.com/quote.ashx?t=TICKER) exposes ~72 fundamental,
technical, ownership, short-interest, and performance fields with NO API KEY.
Validated live against Nick's MU screenshots (P/E, Fwd P/E, Short Float/Ratio,
RSI, Beta, Target, ROE, ROIC, margins all matched exactly).

This module:
1. `fetch(ticker)` — pull the full 72-field snapshot (reuses the existing
   FinvizScraper parser).
2. `squeeze_score(snap)` — a 0-100 short-squeeze setup score (short float +
   days-to-cover + RSI + 52-week-high proximity + relative volume). Per Nick:
   short data is weighted heavily and used to read "is the market betting against
   this name, and is a squeeze building?"
3. `signals(snap)` — finviz-derived signals folded into the composite:
   analyst recom, target upside, quality (ROE/ROIC/margins), short pressure,
   momentum, growth, EPS surprise.
4. `persist_snapshots(...)` — store EVERY field for EVERY candidate each run, so
   we accumulate a dataset to later correlate which finviz metrics actually
   predict forward returns (Nick: "track all of these values to correlate to
   impactful indicators").

Phase 2 (documented, not yet built): scrape the short-interest HISTORY page
(ty=si) and the options/greeks page (ty=oc) for short-trend deviation and IV.
"""
from __future__ import annotations

import json
import time
import warnings
from pathlib import Path
from datetime import date

warnings.filterwarnings("ignore")

from bs4 import BeautifulSoup

from src.scrapers.finviz_scraper import _get_page_html, _parse_snapshot, FINVIZ_QUOTE_URL

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "stockpicker"
SNAP_DIR = DATA_DIR / "finviz_snapshots"
SNAP_DIR.mkdir(parents=True, exist_ok=True)

# Finviz throttling. The quote page has no API key but DOES rate-limit a tight
# loop (a 55-name batch lost ~35 names to silent 429/blocks). A short jittered
# pause between requests + retry-with-backoff gets coverage back to ~100%.
FETCH_MIN_DELAY = 0.6      # base seconds between requests
FETCH_MAX_RETRIES = 3      # attempts per ticker before giving up
_last_fetch_t = [0.0]      # module-level wall-clock of the previous request


def _throttle() -> None:
    """Sleep just enough to keep >= FETCH_MIN_DELAY between requests, jittered."""
    # deterministic per-call jitter (no Math.random/time-of-day dependence on
    # the value itself) — derived from the sub-second fraction of the clock.
    now = time.monotonic()
    elapsed = now - _last_fetch_t[0]
    jitter = 0.1 + (now - int(now)) * 0.3  # 0.1–0.4s
    wait = FETCH_MIN_DELAY + jitter - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_fetch_t[0] = time.monotonic()


def fetch(ticker: str, retries: int = FETCH_MAX_RETRIES) -> dict | None:
    """Return the full finviz snapshot dict for a ticker (None on failure).

    Throttled + retried with exponential backoff so a large batch doesn't get
    rate-limited into partial coverage.
    """
    for attempt in range(1, retries + 1):
        _throttle()
        html = _get_page_html(FINVIZ_QUOTE_URL.format(ticker=ticker))
        if html:
            snap = _parse_snapshot(BeautifulSoup(html, "lxml"))
            if snap:
                return snap
        if attempt < retries:
            time.sleep(FETCH_MIN_DELAY * (2 ** attempt))  # 1.2s, 2.4s backoff
    return None


def enrich_batch(tickers: list[str], progress_every: int = 15) -> dict[str, dict]:
    """Throttled finviz enrichment over a list of tickers.

    Centralizes the polite-rate-limit behaviour so callers (run.py) don't have
    to. Returns {ticker: enrich_ticker(...) result}. Logs any names that still
    failed after retries so partial coverage is never silent.
    """
    out: dict[str, dict] = {}
    failed: list[str] = []
    for i, t in enumerate(tickers):
        out[t] = enrich_ticker(t)
        if not out[t].get("ok"):
            failed.append(t)
        if (i + 1) % progress_every == 0:
            print(f"  finviz {i + 1}/{len(tickers)} ({len(failed)} failed so far)")
    if failed:
        print(f"  [finviz] WARNING: {len(failed)}/{len(tickers)} still failed "
              f"after {FETCH_MAX_RETRIES} retries: {failed}")
    return out


def _f(snap: dict, key: str):
    v = snap.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_cap(text) -> float | None:
    """Parse finviz market-cap / share-count notation like '1.28T', '41.59M'."""
    if not text or not isinstance(text, str):
        return None
    t = text.strip().upper().replace(",", "")
    mult = 1.0
    if t.endswith("T"):
        mult, t = 1e12, t[:-1]
    elif t.endswith("B"):
        mult, t = 1e9, t[:-1]
    elif t.endswith("M"):
        mult, t = 1e6, t[:-1]
    elif t.endswith("K"):
        mult, t = 1e3, t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def squeeze_score(snap: dict) -> dict:
    """0-100 short-squeeze setup score from the snapshot.

    High score = lots of short fuel (high short float + days-to-cover) AND a
    trigger building (price near 52w high, RSI elevated, volume spiking). A name
    like MU (short float 3.7%, ratio 0.81) correctly scores LOW — it's a momentum
    name, not a squeeze. A heavily-shorted name breaking out scores HIGH.
    """
    short_float = _f(snap, "short_float")      # % of float sold short
    short_ratio = _f(snap, "short_ratio")      # days-to-cover
    rsi = _f(snap, "rsi")
    rel_vol = _f(snap, "rel_volume")
    price = _f(snap, "current_price_raw") or _f(snap, "prev_close")
    hi = _f(snap, "high_52w")                  # leading price of the 52w-high cell
    perf_month = _f(snap, "perf_month")

    score = 0.0
    comp = {}

    # Short float — the fuel. Cap ~30%. 40 pts max.
    if short_float is not None:
        comp["short_float_pts"] = min(short_float / 30.0, 1.0) * 40.0
        score += comp["short_float_pts"]
    # Days-to-cover — how hard to unwind. Cap ~10 days. 25 pts.
    if short_ratio is not None:
        comp["days_to_cover_pts"] = min(short_ratio / 10.0, 1.0) * 25.0
        score += comp["days_to_cover_pts"]
    # RSI momentum (only counts the bullish half). 15 pts.
    if rsi is not None:
        comp["rsi_pts"] = max(0.0, (rsi - 50) / 30.0) * 15.0
        score += comp["rsi_pts"]
    # Proximity to 52-week high — the breakout trigger. 10 pts.
    if price and hi and hi > 0:
        prox = price / hi
        comp["breakout_pts"] = max(0.0, (prox - 0.85) / 0.15) * 10.0
        score += comp["breakout_pts"]
    # Relative volume — the spark. 10 pts.
    if rel_vol is not None:
        comp["rel_vol_pts"] = min(max(rel_vol - 1.0, 0.0) / 1.0, 1.0) * 10.0
        score += comp["rel_vol_pts"]

    score = max(0.0, min(100.0, score))
    if score >= 60:
        label = "high squeeze setup"
    elif score >= 35:
        label = "moderate squeeze setup"
    elif score >= 15:
        label = "low squeeze setup"
    else:
        label = "no squeeze"
    return {"score": score, "label": label, "components": comp,
            "short_float": short_float, "short_ratio": short_ratio}


def signals(snap: dict) -> dict:
    """Finviz-derived signals (raw, to be cross-sectionally normalized later).

    All oriented higher = more bullish.
    """
    price = _f(snap, "current_price_raw") or _f(snap, "prev_close")
    target = _f(snap, "target_price")
    recom = _f(snap, "analyst_recom")  # 1=strong buy .. 5=sell
    roe = _f(snap, "roe"); roic = _f(snap, "roic")
    margin = _f(snap, "profit_margin"); oper = _f(snap, "oper_margin")
    short_float = _f(snap, "short_float")
    perf_month = _f(snap, "perf_month"); perf_quarter = _f(snap, "perf_quarter")
    eps_qoq = _f(snap, "eps_qoq"); sales_qoq = _f(snap, "sales_qoq")
    inst_trans = _f(snap, "inst_trans"); insider_trans = _f(snap, "insider_trans")

    quality_parts = [x for x in (roe, roic, margin, oper) if x is not None]
    quality = sum(quality_parts) / len(quality_parts) if quality_parts else None

    return {
        # analyst recommendation, inverted so higher = more bullish (3 - recom)
        "fv_recom": (3.0 - recom) if recom is not None else None,
        # analyst target upside %
        "fv_target_upside": (((target - price) / price * 100)
                             if (target and price) else None),
        # quality (avg of ROE/ROIC/margins)
        "fv_quality": quality,
        # short pressure (higher short float = more bearish positioning) — neg sign
        "fv_short_pressure": (-short_float if short_float is not None else None),
        # momentum (1-quarter performance)
        "fv_momentum": perf_quarter if perf_quarter is not None else perf_month,
        # growth (QoQ EPS + sales)
        "fv_growth": (((eps_qoq or 0) + (sales_qoq or 0)) / 2.0
                      if (eps_qoq is not None or sales_qoq is not None) else None),
        # institutional + insider transaction flow
        "fv_inst_flow": ((inst_trans or 0) + (insider_trans or 0)) or None,
    }


# Fields the picker actually WEIGHTS. A snapshot missing these is useless to the
# composite even though the fetch "succeeded" — which is exactly how a finviz
# layout change went unnoticed from 2026-06-29 to 2026-07-20: `ok` only meant
# "got a non-empty dict", so coverage reported 55/55 while short_float, roe and
# analyst_recom were all None and three weighted signals silently contributed 0.
CRITICAL_FIELDS = ("short_float", "short_ratio", "roe", "analyst_recom")


def snapshot_is_complete(snap: dict | None) -> bool:
    """True when every field the composite weights is actually present."""
    if not snap:
        return False
    return all(snap.get(f) is not None for f in CRITICAL_FIELDS)


def enrich_ticker(ticker: str) -> dict:
    """Full finviz enrichment for one ticker: snapshot + squeeze + signals."""
    snap = fetch(ticker)
    if not snap:
        return {"ticker": ticker, "ok": False, "complete": False}
    return {
        "ticker": ticker, "ok": True,
        # `ok` = the fetch worked. `complete` = the weighted fields are present.
        # Report BOTH; a high ok / low complete split is the signature of a
        # parser drift after a site layout change.
        "complete": snapshot_is_complete(snap),
        "snapshot": snap,
        "squeeze": squeeze_score(snap), "signals": signals(snap),
        "market_cap": _parse_cap(snap.get("market_cap_text")),
        "short_interest": _parse_cap(snap.get("short_interest_text")),
        "earnings_date": snap.get("earnings_date"),
    }


def persist_snapshots(enriched: dict[str, dict], as_of: str | None = None) -> Path:
    """Persist every field for every ticker, building the correlation dataset."""
    as_of = as_of or date.today().isoformat()
    path = SNAP_DIR / f"{as_of}.json"
    payload = {t: e for t, e in enriched.items() if e.get("ok")}
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


if __name__ == "__main__":
    import sys
    tk = sys.argv[1] if len(sys.argv) > 1 else "MU"
    e = enrich_ticker(tk)
    if not e["ok"]:
        print(f"{tk}: fetch failed"); sys.exit(1)
    print(f"{tk}: {len(e['snapshot'])} fields")
    sq = e["squeeze"]
    print(f"  squeeze: {sq['score']:.0f}/100 ({sq['label']}) "
          f"shortFloat={sq['short_float']}% ratio={sq['short_ratio']}")
    print(f"  signals: {json.dumps({k: (round(v,2) if isinstance(v,float) else v) for k,v in e['signals'].items()})}")
    print(f"  market_cap={e['market_cap']:.3e}" if e["market_cap"] else "  market_cap=?")
