"""
finviz_macro.py — Finviz's Futures and Forex pages, via the same
Cloudflare-clearing browser as the other Finviz modules.

## Why this exists

The picker's macro overlay currently proxies through index ETFs (yfinance).
Finviz's Futures/Forex pages give the real underlying instruments directly
(Treasury yields via bond futures, energy, metals, ags, FX majors) — a
cleaner macro input than an ETF proxy, at least for context/risk framing.

## What's confirmed live (2026-09-05)

Both pages are a TILE/heatmap layout, not a table (unlike every other Finviz
page built tonight) — real markup:

    <a data-boxover-ticker="@es" data-boxover-wiim-instrument="futures" ...>
      <div>S&amp;P 500</div>              <- name
      <div>7715.00</div>                  <- last price
      <div>
        <div><span>H</span><span>7764.50</span></div>  <- high
        <div><span>-32.75</span><svg/></div>            <- change (absolute)
      </div>
      <div>
        <div><span>L</span><span>7711.75</span></div>  <- low
        <div>-0.42<span>%</span></div>                  <- change (%)
      </div>
    </a>

`data-boxover-ticker` is a clean, already-slugified instrument code (`@es`,
`@eurusd`) on both pages — but the surrounding tile markup is NOT the same
between the two pages (verified live, not assumed from one working): Futures
tiles nest H/L and their changes as two [value, change] row-pairs; Forex
tiles put name+change%+change on one line and price+H/L on the next. Two
separate parsers, `_parse_tile` (futures) and `_parse_forex_tile` (forex).

The Forex page also embeds a few cross-reference commodity tiles (Gold,
Crude Oil WTI) using the FUTURES markup shape inside an otherwise-Forex page
— `_parse_forex_tile` happens to parse these too (confirmed live: GC/CL rows
come through with sane values), but their `change` field is the page's own
unsigned point-magnitude (the sign only appears on `change_pct` in that
tile's display), unlike `fetch_futures()`'s `change`, which IS signed. Not
reconciled — reported as each page actually displays it, not forced to agree.
"""
from __future__ import annotations

from bs4 import BeautifulSoup
from loguru import logger

from src.stockpicker.finviz_driver import _get_rendered_html

FINVIZ_FUTURES_URL = "https://finviz.com/futures.ashx"
FINVIZ_FOREX_URL = "https://finviz.com/forex.ashx"


def _num(text: str | None):
    if not text:
        return None
    t = text.strip().replace(",", "")
    try:
        return float(t)
    except ValueError:
        return t or None


def _parse_tile(a) -> dict | None:
    ticker = a.get("data-boxover-ticker")
    if not ticker:
        return None
    top_divs = a.find_all("div", recursive=False)
    if len(top_divs) < 3:
        return None
    name = top_divs[0].get_text(strip=True)
    last = _num(top_divs[1].get_text(strip=True))

    hl_wrapper = top_divs[2]
    rows = hl_wrapper.find_all("div", recursive=False)
    high = low = change_abs = change_pct_text = None
    if len(rows) >= 1:
        cells = rows[0].find_all("div", recursive=False)
        if len(cells) >= 2:
            hi_spans = cells[0].find_all("span")
            high = _num(hi_spans[-1].get_text(strip=True)) if hi_spans else None
            change_abs = _num(cells[1].get_text(strip=True).split("\n")[0]) \
                if cells[1] else None
            # change_abs cell text may include a trailing icon with no text —
            # get_text(strip=True) alone is safe here (svg <use> has no text node)
            change_abs = _num(cells[1].get_text(strip=True))
    if len(rows) >= 2:
        cells = rows[1].find_all("div", recursive=False)
        if len(cells) >= 2:
            lo_spans = cells[0].find_all("span")
            low = _num(lo_spans[-1].get_text(strip=True)) if lo_spans else None
            change_pct_text = cells[1].get_text(strip=True)  # e.g. "-0.42%"

    return {
        "ticker": ticker.lstrip("@").upper(),
        "instrument": a.get("data-boxover-wiim-instrument"),
        "name": name,
        "last": last,
        "high": high,
        "low": low,
        "change": change_abs,
        "change_pct": _num(change_pct_text.rstrip("%")) if change_pct_text else None,
        "href": a.get("href"),
    }


def _parse_tiles(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all(attrs={"data-boxover-ticker": True}):
        row = _parse_tile(a)
        if row:
            out.append(row)
    return out


def fetch_futures(page: object | None = None) -> list[dict]:
    """Confirmed live 2026-09-05: 56 tiles (indices, rates, energy, metals,
    grains, softs&meats, currencies all on one page), e.g. {'ticker': 'ES',
    'instrument': 'futures', 'name': 'S&P 500', 'last': 7715.0, 'high': 7764.5,
    'low': 7711.75, 'change': -32.75, 'change_pct': -0.42, 'href':
    '/futures?p=d&t=ES'}."""
    html = _get_rendered_html(FINVIZ_FUTURES_URL, page=page,
                                content_marker="data-boxover-ticker")
    if not html:
        logger.warning("[finviz_macro] Cloudflare did not clear for futures.ashx")
        return []
    return _parse_tiles(html)


def _parse_forex_tile(a) -> dict | None:
    """Forex tiles are NOT the same markup as Futures tiles — verified live,
    not assumed from the Futures parser working. Real structure:

        <a data-boxover-ticker="@eurusd" ...>
          <div>
            <div class="...justify-between...">
              <div class="...uppercase...">EUR/USD</div>        <- name
              <div>-0.11%<span>•</span>0.0013</div>              <- change_pct + change_abs (magnitude only)
            </div>
            <div class="...items-start...">
              <div class="text-xl ...">1.1613</div>              <- last
              <div>
                <div><span>H</span><span>1.1633</span></div>
                <div><span>L</span><span>1.1584</span></div>
              </div>
            </div>
          </div>
          <div><canvas class="sparkline" .../></div>              <- ignored
        </a>
    """
    ticker = a.get("data-boxover-ticker")
    if not ticker:
        return None
    outer = a.find("div", recursive=False)
    if outer is None:
        return None
    rows = outer.find_all("div", recursive=False)
    if len(rows) < 2:
        return None
    header_cells = rows[0].find_all("div", recursive=False)
    name = header_cells[0].get_text(strip=True) if header_cells else None
    change_pct = change_abs = None
    if len(header_cells) >= 2:
        raw = header_cells[1].get_text(strip=True)
        # e.g. "-0.11%•0.0013" (bullet has no surrounding spaces once stripped)
        pct_part, _, abs_part = raw.partition("%")
        change_pct = _num(pct_part)
        change_abs = _num(abs_part.lstrip("•").strip()) if abs_part else None

    price_cells = rows[1].find_all("div", recursive=False)
    last = _num(price_cells[0].get_text(strip=True)) if price_cells else None
    high = low = None
    if len(price_cells) >= 2:
        hl_rows = price_cells[1].find_all("div", recursive=False)
        if len(hl_rows) >= 1:
            spans = hl_rows[0].find_all("span")
            high = _num(spans[-1].get_text(strip=True)) if spans else None
        if len(hl_rows) >= 2:
            spans = hl_rows[1].find_all("span")
            low = _num(spans[-1].get_text(strip=True)) if spans else None

    return {
        "ticker": ticker.lstrip("@").upper(),
        "instrument": a.get("data-boxover-wiim-instrument"),
        "name": name,
        "last": last,
        "high": high,
        "low": low,
        "change": change_abs,
        "change_pct": change_pct,
        "href": a.get("href"),
    }


def fetch_forex(page: object | None = None) -> list[dict]:
    """Forex uses a genuinely different tile layout than Futures (verified
    live, not assumed) — see `_parse_forex_tile`'s docstring for the real
    markup. Confirmed live 2026-09-05: 12 tiles, e.g. {'ticker': 'EURUSD',
    'instrument': 'forex', 'name': 'EUR/USD', 'last': 1.1613, 'high': 1.1633,
    'low': 1.1584, 'change': 0.0013, 'change_pct': -0.11, 'href':
    '/forex?p=d1&t=EURUSD'}."""
    html = _get_rendered_html(FINVIZ_FOREX_URL, page=page,
                                content_marker="data-boxover-ticker")
    if not html:
        logger.warning("[finviz_macro] Cloudflare did not clear for forex.ashx")
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all(attrs={"data-boxover-ticker": True}):
        row = _parse_forex_tile(a)
        if row:
            out.append(row)
    return out
