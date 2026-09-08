"""
finviz_screener.py — the Finviz BULK screener (screener.ashx), via a real browser.

## Why this exists

`finviz_data.py` already scrapes Finviz's per-ticker quote page with plain
`requests` and it works great — no key, 72 fields, validated live. That
covers "give me everything about ticker X."

It does NOT cover "give me every ticker matching filter X" — Finviz's own
bulk screener, which is free, has no per-ticker cost, and supports filters
(short float, RSI, unusual volume, earnings date, insider buying, 52-week
high/low, and dozens more) that would otherwise require pulling and scoring
the entire market ticker-by-ticker. Nick asked for this directly: "great
ticker-based information is available for free on the site" and it's "vital
information that is being missed."

## Why it didn't just work (diagnosed 2026-09-04)

Two SEPARATE failures, easy to conflate:

1. `requests.get(screener.ashx?...)` returns HTTP 200 with a real ~200KB page
   — filter dropdowns, sort options, the works — but ZERO result rows. The
   screener's result table is populated by client-side JavaScript AFTER page
   load (an XHR the static HTML never triggers). This is not a blocked
   request; it's an incomplete one. `_get_page_html`'s plain-requests
   approach, which is exactly right for the quote page, cannot render this.

2. A naive headless Chrome hitting the same URL gets served Cloudflare's
   "Just a moment... Performing security verification" managed-challenge
   page instead of Finviz's HTML at all (`cType: 'non-interactive'` in the
   page's own `_cf_chl_opt` — confirmed via `--dump-dom`). Screener requests
   get this challenge; per-ticker quote-page requests via plain `requests`
   apparently don't (or the challenge only triggers on repeated/bot-shaped
   traffic patterns) — another reason the two data sources needed different
   fixes.

The fix for both: a REAL rendered browser that (a) can pass Cloudflare's
JS-based non-interactive challenge (it's designed to filter out things that
can't execute JS — a real browser clears it in a few seconds with no human
interaction) and (b) waits for the client-side table population to complete
before parsing. `financial-bytes` already has exactly this capability, proven
against a harder target (Fidelity's Akamai bot detection, gating a live
brokerage login) in `src/portfolio/fidelity_scraper.py::_make_driver`. This
module adapts that same recipe — Windows Chrome via DrissionPage over a
remote-debugging port, with the same stealth JS patches — scoped to a
separate Chrome profile and debug port so it can never collide with or
interfere with the live Fidelity automation.

## Usage

    from src.stockpicker.finviz_screener import run_screen

    rows = run_screen(filters=["earningsdate_thismonth"], sort="-marketcap", limit=100)
    # -> [{"ticker": "ORCL", "company": "...", "sector": "...", ...}, ...]

`filters` are Finviz's own filter codes (the same ones the site's screener
UI writes into its URL — see https://finviz.com/screener.ashx for the full
list, or read one off an existing bookmark). This module does not validate
filter codes; an unrecognized one is simply ignored by Finviz same as it
would be by hand.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlencode

from loguru import logger

from src.stockpicker.finviz_driver import _make_driver, _quit_driver, _wait_past_cloudflare

FINVIZ_SCREENER_URL = "https://finviz.com/screener.ashx"


# Finviz's per-row ticker link. Was `quote.ashx?t=TICKER`; as of 2026-09-04
# the screener table (though not the per-ticker quote page) renders
# `stock?t=TICKER&ty=c&p=d&b=1` instead — diagnosed by capturing a live page
# and grepping for a known ticker (ORCL) rather than guessing. Matching both
# forms means a future revert or partial rollout doesn't retrigger this.
_ROW_LINK_RE = re.compile(r'href="(?:quote\.ashx\?t=|stock\?t=)([A-Z][A-Z0-9.\-]*)')


def _wait_for_rows(page, timeout: float = 15.0) -> bool:
    """Poll until at least one screener result row (a ticker link inside the
    results table) is present in the rendered DOM."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            html = page.html
        except Exception:
            html = ""
        if _ROW_LINK_RE.search(html):
            return True
        time.sleep(0.5)
    return False


# ── URL building + row parsing ───────────────────────────────────────────────

def build_url(filters: list[str] | None = None, sort: str | None = None,
              view: int = 111, offset: int = 1, signal: str | None = None) -> str:
    """Build a screener.ashx URL. `sort` is a Finviz column code, optionally
    prefixed with '-' for descending (e.g. '-marketcap'). `offset` is the
    1-indexed result row to start from (Finviz paginates 20/page in view 111;
    r=21 is page 2, r=41 is page 3, ...). `signal` is one of Finviz's own
    built-in screener presets (the `s=` param — e.g. `ta_unusualvolume`,
    `it_latestbuys`, `n_upgrades`; see `PRESETS` below), read live off the
    screener's own "Signal" dropdown 2026-09-05, combinable with `filters`."""
    params: dict[str, str] = {"v": str(view)}
    if filters:
        params["f"] = ",".join(filters)
    if signal:
        params["s"] = signal
    if sort:
        params["o"] = sort
    if offset > 1:
        params["r"] = str(offset)
    return f"{FINVIZ_SCREENER_URL}?{urlencode(params)}"


# Column headers as Finviz's default Overview view (v=111) presents them.
# Kept only as the fallback for the rare case _parse_header_columns can't find
# a <thead> at all (e.g. a captured page fragment in a test fixture) — normal
# parsing now reads real column names from the page itself (see
# _parse_header_columns), so a future view change or column reorder no longer
# silently mismaps data the way a hardcoded list would.
_DEFAULT_COLUMNS = [
    "no", "ticker", "company", "sector", "industry", "country",
    "market_cap", "pe", "price", "change", "volume",
]

# Exact-label overrides so the default Overview view's column keys stay
# byte-identical to the pre-2026-09-05 hardcoded names (callers/tests already
# depend on `row["pe"]`, `row["change"]`, etc.) even though "P/E" and
# "Change %" would otherwise slugify to "p_e" / "change_pct". Every other
# view's headers fall through to the generic slugify below.
_HEADER_ALIASES = {
    "no.": "no", "ticker": "ticker", "company": "company", "sector": "sector",
    "industry": "industry", "country": "country", "market cap": "market_cap",
    "p/e": "pe", "price": "price", "change %": "change", "volume": "volume",
}


def _slugify_header(label: str) -> str:
    """Turn a live column header (e.g. 'Forward P/E', 'Short Float', '52W High')
    into a stable dict-key form, with the Overview view's labels pinned to
    their pre-existing exact names via _HEADER_ALIASES."""
    key = label.strip().lower()
    if key in _HEADER_ALIASES:
        return _HEADER_ALIASES[key]
    s = key.replace("%", "pct")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return s or "col"


def _parse_header_columns(soup) -> list[str]:
    """Read the real column names/order off the results table's own <thead>
    at parse time. Confirmed live 2026-09-05 across 6 view codes (111, 121,
    161, 131, 141, 171) — every one renders a
    `<table class="...screener_table"><thead><tr><th>Label</th>...` header row
    matching its body columns 1:1, so this generalizes cleanly instead of
    assuming Overview's 11-column layout everywhere. Falls back to
    _DEFAULT_COLUMNS only if no <thead>/<th> is found at all."""
    thead = soup.find("thead")
    ths = thead.find_all("th") if thead else []
    if not ths:
        return list(_DEFAULT_COLUMNS)
    keys: list[str] = []
    seen: dict[str, int] = {}
    for th in ths:
        key = _slugify_header(th.get_text(strip=True))
        if key in seen:
            seen[key] += 1
            key = f"{key}_{seen[key]}"
        else:
            seen[key] = 1
        keys.append(key)
    return keys


def _parse_result_page(html: str) -> list[dict]:
    """Parse one screener results page into row dicts, for WHATEVER view
    (`v=`) it was requested with — column names are read from the table's own
    `<thead>` (see _parse_header_columns) rather than a hardcoded 11-column
    list keyed to Overview only. Confirmed live 2026-09-05 against 6 views
    (Overview/Valuation/Financial/Ownership/Performance/Technical), each with
    its own distinct column set, all correctly parsed by this same function.

    Finviz's screener table rows are `<tr class="styled-row ...">` with one
    `<td>` per column; the ticker cell holds a `class="tab-link"` anchor
    (`href="stock?t=TK&ty=c&p=d&b=1"`, or the older `quote.ashx?t=TK` form)
    plus a separate logo anchor (`class="company-ticker"`) whose single-letter
    fallback glyph (e.g. "O" for ORCL) sits in the same `<td>` and would
    otherwise get glued onto the ticker text — that logo anchor is dropped
    before reading cell text. This is intentionally tolerant of the href
    form — it keys off the row link rather than exact class names, which have
    already changed once during this diagnosis.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    columns = _parse_header_columns(soup)
    rows: list[dict] = []
    seen: set[str] = set()

    for tr in soup.find_all("tr"):
        classes = tr.get("class") or []
        if not any(c == "styled-row" for c in classes):
            continue
        a = tr.find("a", class_="tab-link")
        if a is None:
            a = tr.find("a", href=_ROW_LINK_RE)
        if a is None:
            continue
        ticker = a.get_text(strip=True)
        if not ticker or not re.match(r'^[A-Z][A-Z0-9.\-]*$', ticker) or ticker in seen:
            continue
        seen.add(ticker)
        for logo in tr.select("a.company-ticker"):
            logo.decompose()
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        row = {"ticker": ticker, "_raw_cells": cells, "_columns": columns}
        # Label mapping from the page's own live header row; extra or fewer
        # cells than headers (a markup change mid-row) still come through in
        # _raw_cells rather than being silently dropped.
        for i, val in enumerate(cells):
            if i < len(columns):
                row[columns[i]] = val
        row["ticker"] = ticker
        rows.append(row)
    return rows


def run_screen(filters: list[str] | None = None, sort: str | None = None,
                view: int = 111, max_rows: int = 200, headless: bool = True,
                page: object | None = None, signal: str | None = None) -> list[dict]:
    """Run a Finviz bulk screener query and return parsed rows across
    pagination, up to `max_rows`.

    Pass an already-open `page` (from `_make_driver`) to reuse one browser
    across multiple screens in the same run — each `run_screen` call with no
    `page` launches and tears down its own Chrome, which is correct for a
    single ad-hoc query but wasteful for several filters back to back.

    `signal` is one of Finviz's own built-in screener presets (see
    `build_url`'s docstring and `PRESETS`) — combinable with `filters`.
    """
    owns_page = page is None
    if owns_page:
        page = _make_driver(headless=headless)

    all_rows: list[dict] = []
    try:
        offset = 1
        while len(all_rows) < max_rows:
            url = build_url(filters=filters, sort=sort, view=view, offset=offset, signal=signal)
            page.get(url)

            if not _wait_past_cloudflare(page):
                logger.warning(f"[finviz_screener] Cloudflare challenge did not clear for {url}")
                break

            if not _wait_for_rows(page, timeout=12.0):
                # Could be genuinely zero matches, or a slow render. Either
                # way there is nothing more to parse from this URL.
                logger.info(f"[finviz_screener] No result rows detected for {url} (zero matches or timeout)")
                break

            page_rows = _parse_result_page(page.html)
            if not page_rows:
                break

            new = [r for r in page_rows if r["ticker"] not in {x["ticker"] for x in all_rows}]
            if not new:
                break  # pagination looped back to the same page — stop
            all_rows.extend(new)

            if len(page_rows) < 20:
                break  # last page (fewer than a full page of results)
            offset += 20
    finally:
        if owns_page:
            _quit_driver(page)

    return all_rows[:max_rows]


# ── Named presets ─────────────────────────────────────────────────────────
#
# Filter codes and Finviz-built-in "Signal" (`s=`) values enumerated live
# 2026-09-05 off the real screener page's own filter dropdowns and Signal
# select (87 filter fields exist under the "All" filter tab; the Signal
# dropdown has 33 of Finviz's own preset scans) — not transcribed from memory.
# Each entry below was run live the same session; the comment records what
# was actually observed (total match count + a few sample tickers), not an
# assumed filter code. Counts will drift day to day (these are point-in-time
# market scans); re-run to refresh, don't treat the numbers as static.
#
# One real gap found: Finviz's screener has NO filterable "short ratio /
# days to cover" field (87 filter codes enumerated, none of them that) even
# though "Short Ratio" IS a column in the Ownership view (v=131). Days-to-cover
# can be read as data (view=131) but not filtered on directly — the
# short-squeeze preset below filters on short float % + relative volume only;
# days-to-cover screening still requires the per-ticker path
# (finviz_data.squeeze_score, which already computes it from the quote page).
PRESETS: dict[str, dict] = {
    # Short float > 20% AND relative volume > 1.5x — the two screener-filterable
    # halves of a squeeze setup (days-to-cover isn't filterable, see above).
    # Confirmed live 2026-09-05: 19 matches, e.g. BAOS, TYRA, OFAL, AIRS, CHPT.
    "short_squeeze": {"filters": ["sh_short_o20", "sh_relvol_o1.5"], "sort": "-change"},

    # Finviz's own built-in "Unusual Volume" signal (s=ta_unusualvolume).
    # Confirmed live 2026-09-05: 200 matches (Finviz caps this signal's pool
    # at 200), e.g. GPRO, DVLT, BAOS, IMRN, OFAL.
    "unusual_volume": {"signal": "ta_unusualvolume", "sort": "-volume"},

    # New 52-week high (ta_highlow52w=nh). Confirmed live 2026-09-05: 326
    # matches, e.g. NX, PDEX, TITN, MUG, HSCS.
    "new_52w_high": {"filters": ["ta_highlow52w_nh"], "sort": "-change"},

    # New 52-week low (ta_highlow52w=nl). Confirmed live 2026-09-05: 237
    # matches, e.g. TRBG, BTAI, LULG, CMND, IPEX.
    "new_52w_low": {"filters": ["ta_highlow52w_nl"], "sort": "change"},

    # Finviz's own built-in "Recent Insider Buying" signal (s=it_latestbuys).
    # Confirmed live 2026-09-05: 100 matches (also a capped pool), e.g. MSGM,
    # TENX, FTEK, EQPT, BETA. This is "recent buys across the whole market",
    # not per-ticker clustering (2+ insiders buying the same name) — Finviz
    # doesn't expose a cluster count as a filter; clustering would need a
    # second pass counting insider_trans/insider transactions per ticker via
    # finviz_data or the insider_news module.
    "insider_buying": {"signal": "it_latestbuys", "sort": "-change"},

    # Finviz's own built-in "Upgrades" signal (s=n_upgrades). Confirmed live
    # 2026-09-05: only 7 matches (SFNC, VOD, ORA, SHEL, WPC, SNDA, PL) — this
    # signal is narrower than a literal "this week" window; it appears to
    # reflect only the most recent trading day or two of upgrades Finviz has
    # ingested, not a full 7-day roll-up. Named "analyst_upgrades" here
    # rather than "..._this_week" because that week-wide framing is NOT
    # confirmed — if Nick wants a guaranteed 7-day window, this needs a
    # second per-ticker date check (finviz_data / the quote page's ratings
    # table) rather than trusting the signal's own recency window.
    "analyst_upgrades": {"signal": "n_upgrades", "sort": "-change"},
}


def run_preset(name: str, max_rows: int = 200, headless: bool = True,
                page: object | None = None, view: int = 111) -> list[dict]:
    """Run one of the named PRESETS above. Raises KeyError for an unknown name."""
    cfg = PRESETS[name]
    return run_screen(
        filters=cfg.get("filters"), sort=cfg.get("sort"), signal=cfg.get("signal"),
        view=view, max_rows=max_rows, headless=headless, page=page,
    )
