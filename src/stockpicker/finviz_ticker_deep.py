"""
finviz_ticker_deep.py — per-ticker consensus EPS/revisions, analyst forecast,
and short-interest HISTORY, from Finviz's modern `/stock?t=...` page.

## Why this is a separate module from `finviz_data.py`

`finviz_data.py` scrapes Finviz's LEGACY page (`finviz.com/quote.ashx?t=...`)
with plain `requests` — correct for that page, which is still fully
server-rendered. This module targets Finviz's MODERN page
(`finviz.com/stock?t=...`), which is a client-rendered React app behind a
Cloudflare "non-interactive" JS challenge (see `finviz_screener.py`'s
docstring for the full diagnosis). **These are two different pages with
different data, not two URLs for the same content:**

Verified empirically 2026-09-05, not assumed:
  - Legacy `quote.ashx?ty=ea` (Earnings tab): 0 occurrences of "Surprise",
    "GAAP EPS", "EPS Est", "Revenue Est" — the consensus/revision table this
    module scrapes simply is not on that page.
  - Legacy `quote.ashx?ty=si` (Short Interest tab): has today's single
    current value (already covered by `finviz_data.py`'s existing
    short-float/short-ratio fields) but NOT the ~2-year settlement-date
    history table — a plain-requests regex for the tooltip pattern found
    exactly 1 row, not the ~160 the modern page renders.
  - Legacy `quote.ashx?ty=fc` (Forecast tab): has a bare target-price number
    but zero occurrences of "Strong Buy" / "Analyst Consensus" — no ratings
    breakdown.

So every function here uses the shared Cloudflare-clearing browser from
`finviz_screener.py` (`_make_driver`/`_wait_past_cloudflare`), not
`finviz_data.py`'s plain-requests path. An earlier planning pass
(`Projects/stock-picker/finviz-exploration-2026-09-05.md`, "Framework
architecture" section) proposed extending `finviz_data.py` the same
plain-requests way as Overview for these tabs — that assumption is corrected
here, in code, rather than left to be re-discovered by a later block.

## What's scraped

- `fetch_earnings(ticker)` — per-quarter consensus EPS estimate, # of
  analysts, reported, surprise (both adjusted and GAAP), plus revenue
  estimate/reported/surprise. Closes the `DATA-SOURCES.md:30` consensus-EPS
  gap and, via the `revisions` field, the `:52` revision-momentum gap —
  both previously earmarked for a paid Finnhub/FMP key.
- `fetch_forecast(ticker)` — analyst consensus label + score, low/avg/high
  price targets with % upside, and the full ratings breakdown (Strong Buy/
  Buy/Hold/Sell/Strong Sell counts).
- `fetch_short_interest_history(ticker)` — the settlement-date time series
  (short interest shares, shares float, avg daily volume, short float %,
  short ratio) — upgrades the squeeze score from a point-in-time snapshot to
  a real trend, which `finviz_data.py`'s own docstring already flagged as
  "Phase 2, documented, not yet built."

Each function accepts an optional already-open `page` (from
`finviz_screener._make_driver`) to amortize the ~2s Chrome-launch +
Cloudflare-clear cost across several tickers/tabs in one run — matching
`finviz_screener.run_screen`'s own `page=` parameter convention.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from bs4 import BeautifulSoup

from src.stockpicker import finviz_screener as _fs

FINVIZ_STOCK_URL = "https://finviz.com/quote.ashx"


def _get(ticker: str, ty: str, page) -> str:
    """Navigate to one tab of a ticker's modern page and return the cleared
    HTML. Raises RuntimeError if Cloudflare doesn't clear — callers should
    decide whether that's fatal for their use case.

    One retry on a dropped DrissionPage/CDP connection: observed live
    (2026-09-05) reusing one browser across NVDA -> KMX -> GME — GME's first
    `page.get()` raised a raw `PageDisconnectedError` from DrissionPage, and a
    fresh browser fetched the same URL immediately after with no issue. That
    shape (works standalone, fails mid-batch) is a transient CDP hiccup, not a
    page-specific problem — confirmed by the immediate-retry success, not
    assumed from the error text alone.
    """
    url = f"{FINVIZ_STOCK_URL}?t={ticker}&p=d&ty={ty}"
    last_err = None
    for attempt in range(2):
        try:
            page.get(url)
            break
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(1.0)
                continue
            raise RuntimeError(f"page.get() failed twice for {url}: {last_err}") from last_err
    if not _fs._wait_past_cloudflare(page, timeout=15):
        raise RuntimeError(f"Cloudflare challenge did not clear for {url}")
    time.sleep(1.0)  # the per-quarter tables render a beat after the challenge clears
    return page.html


def _with_page(page, fn):
    """Run fn(page), launching/tearing down a browser if the caller didn't
    pass one — same pattern as finviz_screener.run_screen."""
    owns_page = page is None
    if owns_page:
        page = _fs._make_driver(headless=False)
    try:
        return fn(page)
    finally:
        if owns_page:
            _fs._quit_driver(page)


# ── Earnings: consensus EPS + revisions ─────────────────────────────────────

def _parse_earnings_table(soup: BeautifulSoup, row_labels=("Estimate", "# of Analysts", "Reported", "Surprise")):
    """Find a table by its row labels (each row's first cell is a <label> or
    plain text matching one of row_labels) and return
    {header_col: {row_label: value}}. The "Surprise" cell renders as two text
    nodes joined by a bare `<br>` (no other separator) — e.g. GAAP EPS Q3'24
    is literally `0.08<br>10.68%`. Reading it with a bare `get_text(strip=True)`
    concatenates that into "0.0810.68%", which a regex then splits WRONG
    (`0.081` + `0.68%` — a different, incorrect number that still looks
    plausible). Confirmed by inspecting the raw cell HTML rather than
    guessing a smarter regex. Fix: read with an explicit separator so the
    `<br>` boundary survives into the text."""
    out = {}
    seen_tables = set()
    for label_el in soup.find_all(["label", "span", "td"]):
        text = label_el.get_text(strip=True)
        if text not in row_labels:
            continue
        table = label_el.find_parent("table")
        if table is None or id(table) in seen_tables:
            continue
        seen_tables.add(id(table))

        thead = table.find("thead")
        headers = [th.get_text(strip=True) for th in thead.find_all(["th", "td"])] if thead else []
        # first header cell is usually "Currency: USD" or blank — drop it
        period_headers = [h for h in headers if h and not h.lower().startswith("currency")]

        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            row_label = cells[0].get_text(strip=True)
            if row_label not in row_labels:
                continue
            # sep="|" preserves the <br> boundary in the Surprise cell instead
            # of silently concatenating "0.08" and "10.68%" into "0.0810.68%".
            values = [c.get_text(separator="|", strip=True) for c in cells[1:]]
            for period, val in zip(period_headers, values):
                bucket = out.setdefault(period, {})
                if row_label == "Surprise":
                    parts = [p for p in val.split("|") if p]
                    if len(parts) == 2:
                        bucket["surprise_abs"] = parts[0]
                        bucket["surprise_pct"] = parts[1]
                    elif not parts or parts[0] in ("-", "—"):
                        bucket["surprise_abs"] = None
                        bucket["surprise_pct"] = None
                    else:
                        bucket["surprise_raw"] = val  # unexpected shape — keep it, don't drop it
                else:
                    key = {"Estimate": "estimate", "# of Analysts": "n_analysts",
                           "Reported": "reported"}.get(row_label, row_label)
                    bucket[key] = None if val in ("", "-", "—") else val
    return out


def fetch_earnings(ticker: str, page=None) -> dict:
    """Return {"eps": {...}, "gaap_eps": {...}, "revenue": {...},
    "next_report_date": str|None, "report_period": str|None,
    "revisions": {period: {"estimate": ..., "up": int, "down": int, "revision_date": str}}}."""
    def _run(page):
        html = _get(ticker, "ea", page)
        soup = BeautifulSoup(html, "html.parser")

        result: dict = {}
        # header stat blocks (Next report date / Report period / EPS Est. / etc.)
        for key, label in [("next_report_date", "Next report date"), ("report_period", "Report period"),
                            ("eps_est", "EPS Est."), ("gaap_eps_est", "GAAP EPS Est."),
                            ("revenue_est", "Revenue Est.")]:
            i = html.find(label)
            if i < 0:
                result[key] = None
                continue
            m = re.search(r'text-default">([^<]+)</span>', html[i:i + 200])
            result[key] = m.group(1) if m else None

        # the three per-quarter tables share row labels, so find_all above
        # returns them keyed by table identity already — but we need to
        # distinguish EPS vs GAAP EPS vs Revenue. They appear in that order
        # on the page (confirmed by the section anchors: #earnings-eps,
        # #earnings-gaap-eps, #earnings-revenue), so walk sections instead
        # of relying on label text alone.
        sections = {
            "eps": soup.find(id="earnings-eps"),
            "gaap_eps": soup.find(id="earnings-gaap-eps"),
            "revenue": soup.find(id="earnings-revenue"),
        }
        for key, section in sections.items():
            if section is None:
                result[key] = {}
                continue
            result[key] = _parse_earnings_table(section)

        # Latest Revisions panel: per-period estimate + up/down revision counts.
        # Real markup (found by inspecting the raw HTML, not guessed): each
        # period is a flat run of sibling <div>s —
        #   <div class="col-span-2 ...">Q3 '26</div>  (curly apostrophe, U+2019)
        #   <div>Estimate</div><div class="text-default">2.47</div>
        #   <div>Up Revisions</div><div><span>36</span>/40</div>
        #   <div>Down Revisions</div><div><span>2</span>/40</div>
        #   <div>Revision Date</div><div class="text-default">Sep 03, 2026</div>
        # all inside one grid <div> per period. Parsed via BeautifulSoup on
        # the label text itself rather than a positional regex, since a
        # curly-quote/spacing mismatch already broke one regex attempt at
        # this exact panel.
        revisions = {}
        header = soup.find(string=re.compile("Latest Revisions"))
        panel = header.find_parent("div").find_parent("div") if header else None
        # each period card is a grid div whose first child names the period
        if panel is not None:
            for card in panel.find_all("div", class_=re.compile(r"\bgrid\b")):
                children = [c for c in card.find_all("div", recursive=False)]
                if len(children) < 8:
                    continue
                period = children[0].get_text(strip=True)
                labels = {children[i].get_text(strip=True): children[i + 1] for i in range(1, len(children) - 1, 2)}
                def _num(el):
                    return el.get_text(strip=True) if el is not None else None
                est_el = labels.get("Estimate")
                up_el = labels.get("Up Revisions")
                down_el = labels.get("Down Revisions")
                date_el = labels.get("Revision Date")
                up_txt = _num(up_el) or ""
                down_txt = _num(down_el) or ""
                up_m = re.match(r'(\d+)/(\d+)', up_txt)
                down_m = re.match(r'(\d+)/(\d+)', down_txt)
                revisions[period] = {
                    "estimate": _num(est_el),
                    "up": int(up_m.group(1)) if up_m else None,
                    "up_total": int(up_m.group(2)) if up_m else None,
                    "down": int(down_m.group(1)) if down_m else None,
                    "down_total": int(down_m.group(2)) if down_m else None,
                    "revision_date": _num(date_el),
                }
        result["revisions"] = revisions
        return result

    return _with_page(page, _run)


# ── Forecast: analyst consensus + price targets ─────────────────────────────

def fetch_forecast(ticker: str, page=None) -> dict:
    def _run(page):
        html = _get(ticker, "fc", page)
        result: dict = {}

        m = re.search(r'Analyst Consensus \((\d+)\)</span><span[^>]*>([^<]+)</span><span[^>]*>([^<]+)</span>', html)
        if m:
            result["n_analysts"] = int(m.group(1))
            result["consensus"] = m.group(2)
            result["consensus_date"] = m.group(3)

        for key, label in [("low_target", "Low Target"), ("avg_target", "Avg Target"), ("high_target", "High Target")]:
            m = re.search(rf'{re.escape(label)}</span><span[^>]*>\$([\d,.]+)<span[^>]*>([+\-\d.]+%)</span></span>', html)
            if m:
                result[key] = {"price": float(m.group(1).replace(",", "")), "pct": m.group(2)}
            else:
                result[key] = None

        # Plain 'Buy: (\d+)' also matches inside 'Strong Buy: 59' (a substring,
        # not a false anchor) — caught by checking against the real page
        # rather than trusting the first match. Same risk for 'Sell' inside
        # 'Strong Sell'. Negative lookbehind excludes both.
        ratings = {}
        for label in ("Strong Buy", "Buy", "Hold", "Sell", "Strong Sell"):
            pattern = rf'{re.escape(label)}: (\d+)' if label.startswith("Strong") else rf'(?<!Strong ){re.escape(label)}: (\d+)'
            m = re.search(pattern, html)
            ratings[label.lower().replace(" ", "_")] = int(m.group(1)) if m else None
        result["ratings"] = ratings

        m = re.search(r'Consensus:\s*(Strong Buy|Buy|Hold|Sell|Strong Sell)\s*\(([\d.]+)\)', html)
        if m:
            result["consensus_score"] = float(m.group(2))
        return result

    return _with_page(page, _run)


# ── Short interest: full settlement-date history ────────────────────────────

def fetch_short_interest_history(ticker: str, page=None) -> list[dict]:
    def _run(page):
        html = _get(ticker, "si", page)
        soup = BeautifulSoup(html, "html.parser")
        rows = []
        for table in soup.find_all("table"):
            head = table.find("tr")
            if not head or "Settlement Date" not in head.get_text():
                continue
            header_cells = [c.get_text(strip=True) for c in head.find_all(["th", "td"])]
            keys = [_fs._slugify_header(h) for h in header_cells]
            for tr in table.find_all("tr")[1:]:
                cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) != len(keys) or not cells[0]:
                    continue
                rows.append(dict(zip(keys, cells)))
            break  # first matching table is the one we want
        return rows

    return _with_page(page, _run)
