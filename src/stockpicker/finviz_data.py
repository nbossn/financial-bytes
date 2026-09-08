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

Phase 2 (built 2026-09-05, see bottom of this file — `fetch_short_interest_
history`, `fetch_earnings_history`, `fetch_forecast`, `fetch_options_chain`):
the Short Interest history (ty=si), Financials>Earnings (ty=ea),
Financials>Forecast (ty=fc), and Options (ty=oc) tabs on the per-ticker page.

IMPORTANT deviation from this module's own plain-`requests` convention,
confirmed live 2026-09-05: unlike the Overview tab (server-rendered, plain
`requests` works), all four of these tabs are populated by a client-side React
widget AFTER load — a plain `requests.get` on any of them returns HTTP 200
with zero of the target data (confirmed by grepping the raw response for
"Latest Revisions" / "Settlement Date" / "Strong Buy" / "Open Int." — all
absent in the static fetch, all present after a real browser renders the
page). This is the exact same class of problem the bulk screener hit
(`finviz-screener-plan.md` §1), NOT a Cloudflare block (these tabs clear
Cloudflare instantly, same as Overview) — so the fix is the same real-browser
render `finviz_driver.py` already provides, reused here rather than duplicated.
`fetch_page`/`fetch`/`enrich_ticker` above are UNCHANGED and still plain-
`requests` only; the four new functions at the bottom of this file are the
only browser-based code in this module.
"""
from __future__ import annotations

import json
import re
import time
import warnings
from pathlib import Path
from datetime import date

warnings.filterwarnings("ignore")

from bs4 import BeautifulSoup

from src.scrapers.finviz_scraper import _get_page_html, _parse_snapshot, FINVIZ_QUOTE_URL
from src.stockpicker import insider_news
from src.stockpicker.finviz_driver import _get_rendered_html

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


def fetch_page(ticker: str, retries: int = FETCH_MAX_RETRIES):
    """Return ``(snapshot, soup)`` for a ticker — ONE request, parsed once.

    The quote page also carries the insider-trading and news tables that
    `insider_news` needs. Returning the soup lets those be read from the page
    we already paid for, instead of fetching it a second time and doubling the
    rate-limit exposure that FETCH_MIN_DELAY exists to manage.

    Throttled + retried with exponential backoff so a large batch doesn't get
    rate-limited into partial coverage.
    """
    for attempt in range(1, retries + 1):
        _throttle()
        html = _get_page_html(FINVIZ_QUOTE_URL.format(ticker=ticker))
        if html:
            soup = BeautifulSoup(html, "lxml")
            snap = _parse_snapshot(soup)
            if snap:
                return snap, soup
        if attempt < retries:
            time.sleep(FETCH_MIN_DELAY * (2 ** attempt))  # 1.2s, 2.4s backoff
    return None, None


def fetch(ticker: str, retries: int = FETCH_MAX_RETRIES) -> dict | None:
    """Return the full finviz snapshot dict for a ticker (None on failure)."""
    return fetch_page(ticker, retries=retries)[0]


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
    snap, soup = fetch_page(ticker)
    if not snap:
        return {"ticker": ticker, "ok": False, "complete": False}
    # insider_cluster + sentiment, read off the SAME page — no extra request.
    # Keys are always present; a None value means "no data", which becomes NaN
    # downstream rather than a fabricated 0.0.
    extra = insider_news.signals_from_html(insider_html=soup, news_html=soup)
    return {
        "ticker": ticker, "ok": True,
        "insider_cluster": extra["insider_cluster"],
        "sentiment": extra["sentiment"],
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


# ═════════════════════════════════════════════════════════════════════════
# Phase 2, 2026-09-05 — per-ticker Short Interest / Earnings / Forecast /
# Options tabs. Browser-based (see module docstring for why). Each function
# takes an optional already-open `page` (from finviz_driver._make_driver) to
# reuse one browser across several tabs/tickers, same convention as
# finviz_screener.run_screen's `page` parameter.
# ═════════════════════════════════════════════════════════════════════════

FINVIZ_STOCK_URL = "https://finviz.com/stock?t={ticker}&ty={ty}"


def _num(text: str | None) -> float | None:
    """Parse a Finviz numeric cell ('285.96M', '1.23%', '$180.00', '—', '-')
    into a float, using the same suffix convention as `_parse_cap` (T/B/M/K)
    plus '$'/','/'%' stripping. Returns None for dash/empty placeholders."""
    if not text:
        return None
    t = text.strip()
    if t in ("-", "—", "", "N/A"):
        return None
    t = t.replace("$", "").replace(",", "").replace("%", "").strip()
    if t and t[-1].upper() in ("T", "B", "M", "K"):
        return _parse_cap(t)
    try:
        return float(t)
    except ValueError:
        return None


def _parse_short_interest_history(html: str) -> list[dict]:
    """Parse the Short Interest tab's 'Short Interest History' table.

    Real header row confirmed live 2026-09-05 (NVDA, 159-row MSFT spot check):
    Settlement Date, Short Interest, Shares Float, Avg. Daily Volume,
    Short Float, Short Ratio. Column order/labels read from the table's own
    <thead> (same generalized approach as finviz_screener._parse_header_columns)
    rather than hardcoded, so a future column reorder doesn't silently mismap.
    """
    soup = BeautifulSoup(html, "lxml")
    h2 = soup.find(lambda tag: tag.name == "h2" and "Short Interest History" in tag.get_text())
    table = h2.find_next("table") if h2 else None
    if not table or not table.find("thead"):
        return []
    labels = [th.get_text(strip=True) for th in table.find("thead").find_all("th")]
    keys = [re.sub(r"[^a-z0-9]+", "_", lbl.strip().lower()).strip("_") for lbl in labels]
    rows: list[dict] = []
    for tr in table.find("tbody").find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) != len(keys):
            continue
        row = dict(zip(keys, cells))
        row["short_interest"] = _num(row.get("short_interest"))
        row["shares_float"] = _num(row.get("shares_float"))
        row["avg_daily_volume"] = _num(row.get("avg_daily_volume"))
        row["short_float"] = _num(row.get("short_float"))
        row["short_ratio"] = _num(row.get("short_ratio"))
        rows.append(row)
    return rows


def fetch_short_interest_history(ticker: str, page: object | None = None) -> list[dict]:
    """Short interest TREND (not just Overview's single current value) — a
    real time series of settlement dates with short interest shares, shares
    float, avg daily volume, short float %, and short ratio (days-to-cover).
    Confirmed live 2026-09-05: NVDA (~24 rows, ~2yr of biweekly settlements)
    and MSFT (159 rows) both parse cleanly with this same function."""
    html = _get_rendered_html(FINVIZ_STOCK_URL.format(ticker=ticker, ty="si"), page=page)
    if not html:
        return []
    return _parse_short_interest_history(html)


def _parse_financials_table(table) -> dict:
    """Parse one of the Earnings tab's 3 `.financials-table` blocks (EPS,
    GAAP EPS, or Revenue — same markup for all 3, confirmed live 2026-09-05).
    Returns {"periods": [...], "estimate": [...], "num_analysts": [...],
    "reported": [...], "surprise": [...], "surprise_pct": [...]}, one value
    per period, aligned by index."""
    thead = table.find("thead")
    periods = [th.get_text(strip=True) for th in thead.find_all("th")][1:] if thead else []
    out: dict = {"periods": periods}
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        label = tds[0].get_text(strip=True).lower().replace("# of ", "num_").replace(" ", "_")
        vals = [td.get_text(separator="|", strip=True) for td in tds[1:]]
        if label == "surprise":
            abs_vals, pct_vals = [], []
            for v in vals:
                parts = v.split("|")
                abs_vals.append(_num(parts[0]) if parts else None)
                pct_vals.append(_num(parts[1]) if len(parts) > 1 else None)
            out["surprise"] = abs_vals
            out["surprise_pct"] = pct_vals
        else:
            out[label] = [_num(v) for v in vals]
    return out


def _parse_beat_rate(soup) -> dict:
    """Parse the 3-up 'X% <metric> beats the estimate, N/M last quarters'
    summary block. Confirmed live 2026-09-05 (NVDA + MSFT): container is
    `div.justify-evenly` with one `div.flex.text-muted` child per metric."""
    container = soup.find("div", class_=lambda c: c and "justify-evenly" in c)
    out: dict = {}
    if not container:
        return out
    for child in container.find_all("div", recursive=False):
        spans = child.find_all("span", class_="font-semibold")
        pct_div = child.find("div", class_=lambda c: c and "text-2xl" in c)
        if len(spans) < 2 or not pct_div:
            continue
        metric = spans[0].get_text(strip=True).lower().replace(" ", "_")
        out[metric] = {
            "beat_pct": _num(pct_div.get_text(strip=True)),
            "quarters": spans[1].get_text(strip=True),  # e.g. "8/8"
        }
    return out


def _parse_latest_revisions(soup) -> list[dict]:
    """Parse the 'Latest Revisions' panel (per-period estimate + up/down
    revision counts + revision date). Confirmed live 2026-09-05. Only the
    metric shown by default (EPS) is captured — the GAAP EPS/Sales toggle
    buttons require a click this parser doesn't perform (documented
    limitation, not silently dropped)."""
    h4 = soup.find(lambda tag: tag.name == "h4" and tag.get_text(strip=True) == "Latest Revisions")
    if not h4:
        return []
    panel = h4.find_parent("div", class_=lambda c: c and "rounded-md" in c and "border-primary" in c)
    if not panel:
        return []
    out = []
    for grid in panel.find_all("div", class_=lambda c: c and "grid-cols-[102px_auto]" in c):
        cells = grid.find_all("div", recursive=False)
        if len(cells) < 9:
            continue
        period = cells[0].get_text(strip=True)
        up_text = cells[4].get_text(strip=True)   # "9/42"
        down_text = cells[6].get_text(strip=True)  # "0/42"
        up_n, _, up_of = up_text.partition("/")
        down_n, _, down_of = down_text.partition("/")
        out.append({
            "period": period,
            "estimate": _num(cells[2].get_text(strip=True)),
            "up_revisions": _num(up_n), "up_of": _num(up_of),
            "down_revisions": _num(down_n), "down_of": _num(down_of),
            "revision_date": cells[8].get_text(strip=True),
        })
    return out


def fetch_earnings_history(ticker: str, page: object | None = None) -> dict:
    """Financials > Earnings tab: per-quarter consensus EPS (adjusted + GAAP)
    + Revenue, each with estimate/# analysts/reported/surprise%, plus the
    beat-rate summary and the Latest Revisions (up/down) panel. Confirmed
    live 2026-09-05 for NVDA (12 quarters, 8/8 100% beat rate on all 3
    metrics, 47 analysts on the oldest quarter down to 36 on the newest) and
    spot-checked on MSFT (same 3-table/beat-rate/revisions structure).

    Returns {} if the tab's client-side render didn't produce the expected
    tables (e.g. a genuine site change) — never a partially-wrong dict.
    """
    html = _get_rendered_html(FINVIZ_STOCK_URL.format(ticker=ticker, ty="ea"), page=page)
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table", class_="financials-table")
    if len(tables) < 3:
        return {}
    return {
        "eps": _parse_financials_table(tables[0]),
        "gaap_eps": _parse_financials_table(tables[1]),
        "revenue": _parse_financials_table(tables[2]),
        "beat_rate": _parse_beat_rate(soup),
        "latest_revisions_eps": _parse_latest_revisions(soup),
    }


def _parse_forecast_summary(soup) -> dict:
    """Parse the top 'Analyst Consensus (N) / Low / Avg / High Target' block."""
    container = soup.find("div", class_=lambda c: c and "justify-center" in c and "gap-16" in c)
    out: dict = {}
    if not container:
        return out
    for child in container.find_all("div", recursive=False):
        spans = child.find_all("span", recursive=False)
        if len(spans) < 2:
            continue
        label = spans[0].get_text(strip=True)  # "Analyst Consensus (58)" / "Low Target" / ...
        value_text = spans[1].get_text(separator="|", strip=True)
        parts = value_text.split("|")
        date_text = spans[2].get_text(strip=True) if len(spans) > 2 else None
        key_m = re.match(r"(.+?)(?:\s*\((\d+)\))?$", label)
        key = re.sub(r"[^a-z0-9]+", "_", label.split("(")[0].strip().lower()).strip("_")
        entry = {"date": date_text}
        if "consensus" in key:
            entry["rating"] = parts[0] if parts else None
            m = re.search(r"\((\d+)\)", label)
            entry["num_analysts"] = int(m.group(1)) if m else None
        else:
            entry["value"] = _num(parts[0]) if parts else None
            entry["pct"] = _num(parts[1]) if len(parts) > 1 else None
        out[key] = entry
    return out


def _parse_rating_breakdown(soup) -> dict:
    """Parse the 'N Analysts / Strong Buy: n / Buy: n / ... / Consensus: X (s)'
    line. Confirmed live 2026-09-05: NVDA 68 analysts (59/6/2/0/1, consensus
    Strong Buy 1.21) matches the numbers already recorded in
    `finviz-integration-plan.md` §6 from Nick's screenshot walkthrough."""
    row = soup.find("div", class_=lambda c: c and "justify-between" in c and "tabular-nums" in c)
    if not row:
        return {}
    text = row.get_text("|", strip=True)
    out: dict = {}
    m = re.search(r"(\d+)\s*Analysts", text)
    if m:
        out["num_analysts"] = int(m.group(1))
    # "Buy" and "Sell" are substrings of "Strong Buy"/"Strong Sell" in this
    # text, so a plain search for "Buy:" would wrongly match inside "Strong
    # Buy: 59" — negative lookbehind keeps the two pairs distinct (confirmed
    # live 2026-09-05: without it, NVDA's "buy" count came back as 59, a copy
    # of "strong_buy", instead of the real value 6).
    for label in ("Strong Buy", "Strong Sell", "Buy", "Hold", "Sell"):
        pattern = rf"{label}:\s*(\d+)" if label.startswith("Strong") else rf"(?<!Strong ){label}:\s*(\d+)"
        m = re.search(pattern, text)
        if m:
            out[label.lower().replace(" ", "_")] = int(m.group(1))
    m = re.search(r"([A-Za-z ]+)\s*\(([\d.]+)\)", text)
    if m:
        out["consensus_label"] = m.group(1).strip()
        out["consensus_score"] = _num(m.group(2))
    return out


def fetch_forecast(ticker: str, page: object | None = None) -> dict:
    """Financials > Forecast tab: analyst consensus rating breakdown
    (Strong Buy/Buy/Hold/Sell/Strong Sell counts + numeric score) and
    Low/Avg/High price targets with % upside. Confirmed live 2026-09-05:
    NVDA 58-68 analysts (consensus block and rating-breakdown block query
    slightly different analyst subsets on Finviz's own page, both captured
    as-is rather than reconciled), Low/Avg/High $180.00/$334.32/$710.29;
    spot-checked on MSFT (51/60 analysts, $400/$567.45/$700 targets).
    """
    html = _get_rendered_html(FINVIZ_STOCK_URL.format(ticker=ticker, ty="fc"), page=page)
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")
    summary = _parse_forecast_summary(soup)
    breakdown = _parse_rating_breakdown(soup)
    if not summary and not breakdown:
        return {}
    return {"summary": summary, "rating_breakdown": breakdown}


def _parse_options_chain(html: str) -> list[dict]:
    """Parse the Options tab's calls/puts table for whichever expiry is
    selected by default (the nearest one — confirmed live 2026-09-05: NVDA
    defaulted to 09/09/2026, the nearest Friday from today's 2026-09-05).
    Real header order (17 <th> across one table): 7 call columns (Last Close,
    Change $, Change %, Bid, Ask, Volume, Open Int.), a blank spacer, Strike,
    a blank spacer, the same 7 columns again for puts."""
    soup = BeautifulSoup(html, "lxml")
    call_cols = ["last_close", "change", "change_pct", "bid", "ask", "volume", "open_interest"]
    target = None
    for table in soup.find_all("table"):
        ths = [th.get_text(strip=True) for th in table.find_all("th")]
        if ths[:7] == ["Last Close", "Change $", "Change %", "Bid", "Ask", "Volume", "Open Int."]:
            target = table
            break
    if target is None:
        return []
    rows = []
    for tr in target.find("tbody").find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) != 17:
            continue
        call_vals, strike, put_vals = cells[0:7], cells[8], cells[10:17]
        row = {"strike": _num(strike)}
        for k, v in zip(call_cols, call_vals):
            row[f"call_{k}"] = _num(v) if k != "change_pct" else v
        for k, v in zip(call_cols, put_vals):
            row[f"put_{k}"] = _num(v) if k != "change_pct" else v
        rows.append(row)
    return rows


def fetch_options_chain(ticker: str, page: object | None = None) -> list[dict]:
    """Options tab: full calls/puts chain (strike, bid/ask, volume, OI) for
    the nearest expiry shown by default. Lowest priority of the 4 new tabs
    per Nick's own framing ("not an options trader"). Confirmed live
    2026-09-05: NVDA nearest expiry (09/09/2026) returned 64 strike rows,
    both sides populated. Does not select a different expiry — that requires
    clicking the 'Expiry select' combobox, not implemented."""
    html = _get_rendered_html(FINVIZ_STOCK_URL.format(ticker=ticker, ty="oc"), page=page)
    if not html:
        return []
    return _parse_options_chain(html)


# ═════════════════════════════════════════════════════════════════════════
# Breadth-first site pass, 2026-09-05 — Home page Signal table.
#
# The ONE genuinely surprising finding from the breadth-first pass across
# Groups/Calendar/Futures/Forex/News/Home (see
# `Projects/stock-picker/finviz-exploration-2026-09-05.md` for the full
# table): unlike every other section checked tonight, Finviz's HOME page
# Signal table (Top Gainers/Losers, New High/Low, Overbought/Oversold,
# Unusual Volume, Most Active, Most Volatile, Upgrades/Downgrades, Insider
# Buying/Selling — 13 categories total) is SERVER-RENDERED. Confirmed live:
# a plain `requests.get("https://finviz.com/")` returns the real ticker rows
# with no browser at all — the exact opposite of the bulk screener, which
# needs the full Cloudflare+browser treatment for the same category of data
# (`finviz_screener.py`'s `unusual_volume`/`new_52w_high`/`insider_buying`
# presets). This is a strictly cheaper path to the same signal lists the
# screener already exposes, for whichever categories the Home page covers.
# ═════════════════════════════════════════════════════════════════════════

FINVIZ_HOME_URL = "https://finviz.com/"


def fetch_home_signals() -> dict[str, list[dict]]:
    """Home page Signal table — plain `requests`, no browser (see note above).
    Confirmed live 2026-09-05: 38 rows across 13 categories in one page load
    (Top Gainers, Top Losers, New High, New Low, Overbought, Oversold,
    Unusual Volume, Most Active, Most Volatile, Upgrades, Downgrades,
    Insider Buying, Insider Selling — real sample row: AOUT +44.66% on
    4.43M volume under Top Gainers).

    Returns {category_slug: [{"ticker", "price", "change_pct", "volume"}, ...]}.
    Returns {} if the expected row markup isn't found (a real site change,
    not a silently empty result mistaken for zero matches)."""
    html = _get_page_html(FINVIZ_HOME_URL)
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")
    rows = soup.find_all("tr", class_=lambda c: c and "hp_signal-row" in c)
    if not rows:
        return {}
    out: dict[str, list[dict]] = {}
    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) != 6:
            continue
        ticker_td = tds[0]
        ticker = ticker_td.get("data-boxover-ticker")
        if not ticker:
            for logo in ticker_td.select("span.company-ticker"):
                logo.decompose()
            a = ticker_td.find("a")
            ticker = a.get_text(strip=True) if a else ticker_td.get_text(strip=True)
        category_a = tds[5].find("a")
        category = category_a.get_text(strip=True) if category_a else tds[5].get_text(strip=True)
        if not category:
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_")
        entry = {
            "ticker": ticker,
            "price": _num(tds[1].get_text(strip=True)),
            "change_pct": _num(tds[2].get_text(strip=True)),
            "volume": _num(tds[3].get_text(strip=True)),
        }
        out.setdefault(slug, []).append(entry)
    return out


# ═════════════════════════════════════════════════════════════════════════
# News page (finviz.com/news.ashx), 2026-09-05 — resolved after being logged
# genuinely unresolved earlier tonight ("DOM contained only the page's own
# unrendered JS templates ([[url]]/[[summary]] literal placeholders) — the
# real feed didn't populate in the time given").
#
# Re-investigated with the exact same browser infra (`finviz_driver._make_
# driver`/`_get_rendered_html`), just a longer/marker-based wait instead of a
# bare fixed sleep: with a 5s wait (vs. whatever shorter wait the earlier
# attempt used) the page DOES render 186 real `tr.news_table-row` rows with
# real headlines/URLs — confirmed against real content (WSJ/NYT/MarketWatch
# headlines dated the actual day), not just a non-empty DOM. Re-ran via
# `_get_rendered_html(..., content_marker="news_table-row")` (the same
# poll-until-present primitive built earlier tonight for the Insiders feed's
# 0-row race) 4 times in a row on one reused browser: 186/186/186/186 rows,
# zero flaky empty reads — this did NOT need a scroll-trigger or a tab click,
# just enough wall-clock time for the client-side render to finish before
# reading `page.html`.
#
# The `[[url]]`/`[[title]]` placeholder strings the earlier attempt saw ARE
# still present in the page's raw HTML even now (confirmed: `html.count("[[")
# == 56` on a fully-rendered page) — but they are a native-ad slot's OWN
# unfired JS template literal (`<a href="[[url]]">[[title]]</a>` inside a
# `native-container` div), present on every load regardless of whether the
# real news table rendered. That template string being present is not
# evidence the feed failed to render; it's an unrelated, always-present ad
# placeholder. Whatever grepped for `[[` earlier and concluded the page
# never populated was looking at the wrong marker — a genuine finding worth
# recording so a future session doesn't repeat the same false negative.
#
# Real markup: `tr.news_table-row` — but ~3% of them (6/186 in the sample
# validated) are Google ad slots sharing the identical row class with no
# `a.nn-tab-link` inside (just an `img.news_ad-icon-cell` + an ad iframe
# container) — filtered out below by requiring the headline link to exist,
# not by row count/position (ad slot placement isn't fixed).
# ═════════════════════════════════════════════════════════════════════════

FINVIZ_NEWS_URL = "https://finviz.com/news.ashx"


def _parse_news(html: str) -> list[dict]:
    """Parse the News page's headline rows. `html.parser` (not `lxml`) is
    required here — confirmed live 2026-09-05: `lxml` silently drops every
    `<svg><use>` element inside each row (0 found where html.parser finds 2),
    which is where the source-site icon lives; `lxml`'s HTML parser doesn't
    handle the inline SVG foreign-content the same way. Everything else in
    this module uses `lxml` deliberately; this function is the one
    exception, for this one reason."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for tr in soup.find_all("tr", class_=lambda c: c and "news_table-row" in c):
        a = tr.find("a", class_="nn-tab-link")
        if a is None:
            continue  # ad slot sharing the same row class, not a headline
        date_cell = tr.find("td", class_=lambda c: c and "news_date-cell" in c)
        link_cell = tr.find("td", class_=lambda c: c and "news_link-cell" in c)
        use = tr.find("use")
        source = None
        if use and use.get("href"):
            source = re.sub(r"-(light|dark)$", "", use["href"].split("#")[-1])
        out.append({
            "date": date_cell.get_text(strip=True) if date_cell else None,
            "headline": a.get_text(strip=True),
            "url": a.get("href"),
            "source": source,
            "summary": link_cell.get("data-boxover-text") if link_cell else None,
        })
    return out


def fetch_news(page: object | None = None) -> list[dict]:
    """Finviz's main News page (finviz.com/news.ashx) — general market
    headlines (WSJ/MarketWatch/NYT/Reuters/etc.), NOT per-ticker news (that's
    already covered, server-rendered, by `insider_news.parse_news_table` off
    the legacy quote page `fetch_page` already returns).

    Confirmed live 2026-09-05: 180 real headline rows (of 186 total, 6 being
    ad slots — filtered out, see `_parse_news`), e.g. "For Many Individual
    Traders, Prediction Markets Are Hot—and Crypto Is Not" (WSJ, Sep-04).
    Needed a longer render wait than earlier attempts used, NOT a
    scroll-trigger or tab click — see the module-level note above for what
    the earlier "still empty" finding actually was.

    Returns [] if the expected row markup isn't found (a real site change),
    never a partial/wrong result."""
    html = _get_rendered_html(FINVIZ_NEWS_URL, page=page, content_marker="news_table-row")
    if not html:
        return []
    return _parse_news(html)


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
