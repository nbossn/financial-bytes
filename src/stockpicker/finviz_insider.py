"""
finviz_insider.py — Finviz Insider trading feed + institutional Managers
(13F), via a real browser.

Built 2026-09-05, reusing recon already done and documented in
`finviz-screener-plan.md` §5 (real URLs, real row markup for the two list
pages, found live 2026-09-04/05) rather than re-discovering it, plus the
Managers drill-down page's markup, captured live for the FIRST time this
session (that page had never been inspected before tonight).

## Why browser, not plain `requests`

Both list pages clear Cloudflare cleanly with the existing stealth recipe —
lower friction than the bulk screener, no retry needed — but (like
`finviz_data.py`'s new Short Interest/Earnings/Forecast/Options tabs) the
actual table content is populated by a client-side widget after load, not
present in a static `requests.get`. Same mechanism, same fix: a real browser
via `finviz_driver.py`.

## What's here

- `fetch_insiders(tc=7, page=None)` — the live Form 4 feed
  (`insidertrading.ashx`). Real row markup:
  `<tr class="fv-insider-row is-{type}-{n} cursor-pointer">` — the
  transaction type (buy/sale/proposedSale/option) is encoded directly in the
  row's own CSS class. Real header (confirmed live, 10 columns): Ticker,
  Owner, Relationship, Date, Transaction, Cost, #Shares, Value ($),
  #Shares Total, SEC Form 4 (the last column is a link to the actual SEC.gov
  Form 4 filing, not literal text).
- `fetch_managers(page=None)` — the Managers list (`/insidertrading/managers`),
  one card per institutional manager: name, AUM, QoQ change %, and a
  `/insidertrading/managers/{slug}-{cik}` drill-down URL.
- `fetch_manager_detail(slug_cik, page=None)` — the drill-down page for ONE
  manager: General Statistics (market value this/prev quarter, # holdings,
  new/added/reduced counts, top-10 concentration, avg time held) and Top
  Buys / Top Sells (ticker, option type if any, $ change). Sector Allocation
  is a `recharts` SVG chart with no accompanying legend/table in the DOM —
  genuinely not scrapable as structured data without either simulating chart
  hover events or reverse-engineering the chart's own data props; logged as
  NOT built rather than faked.

## Known limitation, not silently worked around

Nick's own walkthrough demoed BlackRock's manager page, but BlackRock was
NOT among the 12 unique manager cards the Managers list actually rendered on
first page load tonight (2026-09-05) — the list may paginate/lazy-load
further managers on scroll, or BlackRock's specific card requires a search/
filter not exercised here. `fetch_manager_detail` was validated instead
against Susquehanna International Group (the largest-AUM manager actually
present on that first render, $1,284.00B, +43.73% last quarter) — the parser
itself is generic to any `{slug}-{cik}` URL, but Nick's own demoed name was
not the one used to prove it live. Flagging this rather than assuming the
same URL pattern silently covers it.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from src.stockpicker.finviz_driver import _get_rendered_html

FINVIZ_INSIDERS_URL = "https://finviz.com/insidertrading.ashx"
FINVIZ_MANAGERS_URL = "https://finviz.com/insidertrading/managers"
FINVIZ_BASE = "https://finviz.com"

_TYPE_RE = re.compile(r"is-([a-zA-Z]+)-\d+")
_CIK_RE = re.compile(r"[?&]oc=(\d+)")
_MANAGER_HREF_RE = re.compile(r"^/insidertrading/managers/(?P<slug_cik>[^/?]+)$")
_CIK_SUFFIX_RE = re.compile(r"-(\d+)$")


def _num(text: str | None) -> float | None:
    """Parse a Finviz numeric cell ('1,600,917', '3.21', '+43.73%', '1,284.00B')
    into a float. Same suffix/sign convention used across this session's new
    Finviz parsers (finviz_data.py's `_num`, duplicated here rather than
    imported to keep this module standalone/importable on its own)."""
    if not text:
        return None
    t = text.strip()
    if t in ("-", "—", "", "N/A"):
        return None
    sign = -1.0 if t.startswith("-") else 1.0
    t = t.lstrip("+-").replace("$", "").replace(",", "").replace("%", "").strip()
    mult = 1.0
    if t and t[-1].upper() in ("T", "B", "M", "K"):
        mult = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3}[t[-1].upper()]
        t = t[:-1]
    try:
        return sign * float(t) * mult
    except ValueError:
        return None


def _parse_insider_row(tr) -> dict | None:
    classes = tr.get("class") or []
    row_type = None
    for c in classes:
        m = _TYPE_RE.match(c)
        if m:
            row_type = m.group(1)
            break
    cells = tr.find_all("td")
    if len(cells) != 10:
        return None

    ticker_td = cells[0]
    ticker = ticker_td.get("data-boxover-ticker")
    if not ticker:
        for logo in ticker_td.select("span.company-ticker"):
            logo.decompose()
        a = ticker_td.find("a")
        ticker = a.get_text(strip=True) if a else ticker_td.get_text(strip=True)

    owner_td = cells[1]
    owner_a = owner_td.find("a")
    owner_cik = None
    if owner_a and owner_a.get("href"):
        m = _CIK_RE.search(owner_a["href"])
        owner_cik = m.group(1) if m else None

    sec_td = cells[9]
    sec_a = sec_td.find("a")

    return {
        "ticker": ticker,
        "company": ticker_td.get("data-boxover-company"),
        "owner": owner_td.get_text(strip=True),
        "owner_cik": owner_cik,
        "relationship": cells[2].get_text(strip=True),
        "date": cells[3].get_text(strip=True),
        "transaction": cells[4].get_text(strip=True),
        "type": row_type,
        "cost": _num(cells[5].get_text(strip=True)),
        "shares": _num(cells[6].get_text(strip=True)),
        "value_usd": _num(cells[7].get_text(strip=True)),
        "shares_total": _num(cells[8].get_text(strip=True)),
        "filed_at": sec_td.get_text(strip=True),
        "sec_form_url": sec_a.get("href") if sec_a else None,
    }


def _parse_insiders(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for tr in soup.find_all("tr", class_=lambda c: c and "fv-insider-row" in c):
        parsed = _parse_insider_row(tr)
        if parsed:
            rows.append(parsed)
    return rows


def fetch_insiders(tc: int = 7, page: object | None = None) -> list[dict]:
    """Live Form 4 insider-trading feed. `tc=7` is Finviz's own "Latest
    Insider Trading" filter code (confirmed live 2026-09-04/05). Confirmed
    live again 2026-09-05: 203 rows in one page load, 3 transaction types
    observed (sale/buy/proposedSale)."""
    url = f"{FINVIZ_INSIDERS_URL}?tc={tc}"
    html = _get_rendered_html(url, page=page, content_marker="fv-insider-row")
    if not html:
        return []
    return _parse_insiders(html)


def _parse_managers(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    seen: dict[str, dict] = {}
    for a in soup.find_all("a", href=_MANAGER_HREF_RE):
        href = a["href"]
        if href in seen:
            continue
        h4s = a.find_all("h4")
        if len(h4s) < 2:
            continue
        name = h4s[0].get_text(strip=True)
        aum = _num(h4s[1].get_text(strip=True))
        h5 = a.find_all("h5")
        change_text = h5[1].get_text(strip=True) if len(h5) > 1 else ""
        m = re.match(r"([+\-][\d.]+)%", change_text)
        change_pct = float(m.group(1)) if m else None
        slug_cik = href.rsplit("/", 1)[-1]
        cik_m = _CIK_SUFFIX_RE.search(slug_cik)
        seen[href] = {
            "name": name,
            "aum": aum,
            "qoq_change_pct": change_pct,
            "slug_cik": slug_cik,
            "cik": cik_m.group(1) if cik_m else None,
            "url": FINVIZ_BASE + href,
        }
    return list(seen.values())


def fetch_managers(page: object | None = None) -> list[dict]:
    """Managers (13F institutional holdings) list. Confirmed live
    2026-09-05: 12 unique manager cards on one page load (e.g. Susquehanna
    International Group $1,284.00B +43.73% last quarter, Goldman Sachs Group
    $1,151.63B +32.23%, Citadel Advisors $875.01B +41.48%) — a prior session
    found 24 links on the same page, so this list may lazy-load more cards
    on scroll/pagination that this single-render fetch doesn't trigger; not
    assumed to be the full manager universe."""
    html = _get_rendered_html(FINVIZ_MANAGERS_URL, page=page, content_marker="/insidertrading/managers/")
    if not html:
        return []
    return _parse_managers(html)


def _parse_stat_table(table) -> dict:
    out = {}
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) != 2:
            continue
        key = re.sub(r"[^a-z0-9]+", "_", tds[0].get_text(strip=True).lower()).strip("_")
        out[key] = tds[1].get_text(strip=True)
    return out


def _parse_top_table(h5_text: str, soup) -> list[dict]:
    h5 = soup.find(lambda tag: tag.name == "h5" and tag.get_text(strip=True) == h5_text)
    if not h5 or not h5.parent:
        return []
    table = h5.parent.find_next_sibling("table")
    if not table:
        return []
    out = []
    for tr in table.find("tbody").find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) != 3:
            continue
        a = tds[0].find("a")
        href = a.get("href") if a else None
        ticker = None
        if href:
            m = re.search(r"[?&]t=([A-Z][A-Z0-9.\-]*)", href)
            ticker = m.group(1) if m else None
        out.append({
            "ticker": ticker,
            "option_type": tds[1].get_text(strip=True) or None,
            "change_usd": _num(tds[2].get_text(strip=True)),
        })
    return out


def fetch_manager_detail(slug_cik: str, page: object | None = None) -> dict:
    """Drill-down page for ONE manager (`{slug}-{cik}`, from `fetch_managers`'
    `slug_cik` field). Returns {} if the expected sections aren't found (a
    real site change, not silently partial). Confirmed live 2026-09-05
    against `susquehanna-international-group-llp-1446194`:
    General Statistics (Market Value This Q $1,284.00B, Prev Q $893.33B,
    13K holdings, 2K new purchases, 6K added to, 5K reduced, Top 10
    Holdings 26.66%, Time Held Top 10 42.50 quarters) and Top Buys/Sells
    (6 rows each, e.g. top buy MU Call +$30.51B, top sell SPY Put -$11.74B).
    Sector Allocation is NOT parsed — see module docstring."""
    url = f"{FINVIZ_MANAGERS_URL}/{slug_cik}"
    html = _get_rendered_html(url, page=page, content_marker="General Statistics")
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")

    stats_h5 = soup.find(lambda tag: tag.name == "h5" and tag.get_text(strip=True) == "General Statistics")
    general_stats = {}
    if stats_h5 and stats_h5.parent:
        stats_table = stats_h5.parent.find_next_sibling("table")
        if stats_table:
            general_stats = _parse_stat_table(stats_table)

    top_buys = _parse_top_table("Top Buys", soup)
    top_sells = _parse_top_table("Top Sells", soup)

    if not general_stats and not top_buys and not top_sells:
        return {}
    return {
        "general_stats": general_stats,
        "top_buys": top_buys,
        "top_sells": top_sells,
        "sector_allocation": None,  # chart-only, not scraped — see docstring
    }


if __name__ == "__main__":
    from src.stockpicker.finviz_driver import _make_driver, _quit_driver

    p = _make_driver(headless=False)
    try:
        ins = fetch_insiders(page=p)
        print(f"insiders: {len(ins)} rows, sample: {ins[0] if ins else None}")
        mgrs = fetch_managers(page=p)
        print(f"managers: {len(mgrs)} cards, sample: {mgrs[0] if mgrs else None}")
        if mgrs:
            detail = fetch_manager_detail(mgrs[0]["slug_cik"], page=p)
            print(f"detail for {mgrs[0]['name']}: {detail}")
    finally:
        _quit_driver(p)
