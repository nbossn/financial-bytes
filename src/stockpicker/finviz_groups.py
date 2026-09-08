"""
finviz_groups.py — Finviz's Groups page (sector/industry/country/cap-size
aggregates), via the same Cloudflare-clearing browser as the screener.

## Why this exists

`sectors.py`'s current sector-deviation scan is yfinance-sector-ETF-return
only. Finviz's Groups page is a strictly richer version of the same idea:
real cap-weighted sector/industry fundamentals (market cap, P/E, dividend
yield, change %, volume) in one view, using the exact same underlying data
Finviz's per-ticker/screener pages already draw from. This module gives
`sectors.py` a second, richer input to compare against — not wired in yet,
see `Projects/stock-picker/finviz-exploration-2026-09-05.md`.

## What's confirmed live (2026-09-05)

Bare `/groups` renders only the filter controls — the results table never
populates (no `<tbody>` rows appear even after a browser clears Cloudflare
and waits). An explicit view/order/sort query string DOES render:
`/groups?g=sector&v=110&o=name&st=d1` returned 11 real sector rows on first
try. Row markup is `<tr class="styled-row">` — the SAME class the bulk
screener's `finviz_screener._parse_result_page` already keys off — but the
first cell is a sector NAME, not a ticker link, so that function's ticker-
anchor requirement doesn't apply here; this module has its own row selector
that doesn't require any anchor at all, just a `styled-row` `<tr>` inside the
results table.

`g=` selects the grouping (`sector`, `industry`, `industry_sector` i.e.
industry-per-sector, `country`, `capitalization`). `v=` selects the view
(matches the screener's view codes — 110 is Groups' own default/Overview
analog). Not all `v=` codes have been tried; 110 is the only one confirmed
live so far.
"""
from __future__ import annotations

import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from loguru import logger

from src.stockpicker.finviz_driver import _get_rendered_html
from src.stockpicker.finviz_screener import _parse_header_columns

FINVIZ_GROUPS_URL = "https://finviz.com/groups.ashx"


def build_url(group: str = "sector", view: int = 110, order: str = "name",
              sort_dir: str = "d1") -> str:
    """Build a groups.ashx URL. `group` is Finviz's `g=` grouping code
    (sector/industry/industry_sector/country/capitalization). `order` is the
    column to sort by (`o=`); `sort_dir` is Finviz's own direction code
    (`st=` — `d1` confirmed live, meaning/alternatives not fully enumerated).
    A bare URL with no `v=`/`o=`/`st=` renders filter controls only and no
    result rows — confirmed live, not assumed — so this always includes them."""
    params = {"g": group, "v": str(view), "o": order, "st": sort_dir}
    return f"{FINVIZ_GROUPS_URL}?{urlencode(params)}"


def _parse_group_rows(html: str) -> list[dict]:
    """Parse one Groups results page into row dicts. Column names come from
    the table's own `<thead>` (reusing `finviz_screener._parse_header_columns`
    — same generalized approach, confirmed to work here too since the Groups
    table renders the same `<table ...><thead><tr><th>` markup shape).

    Row selection deliberately does NOT require a ticker-style anchor (unlike
    `finviz_screener._parse_result_page`) — a group row's first cell is a
    plain sector/industry NAME, sometimes as plain text and sometimes wrapped
    in a link to that group's own filtered screener view. Any `<tr
    class="styled-row">` inside the results table is treated as a real row."""
    soup = BeautifulSoup(html, "html.parser")
    columns = _parse_header_columns(soup)
    rows: list[dict] = []
    for tr in soup.find_all("tr"):
        classes = tr.get("class") or []
        if not any(c == "styled-row" for c in classes):
            continue
        tds = tr.find_all("td")
        if not tds:
            continue
        cells = [td.get_text(strip=True) for td in tds]
        row = {"_raw_cells": cells, "_columns": columns}
        for i, val in enumerate(cells):
            if i < len(columns):
                row[columns[i]] = val
        # The group's own name/label column — confirmed live as "name" for
        # g=sector; NOT cells[0], which is the "No." row-number column
        # (caught live: an early version of this parser set group_name to
        # "1"/"2"/"3" instead of "Basic Materials"/"Communication Services").
        # Falls back to the raw first cell only if no "name" column exists.
        row["group_name"] = row.get("name") or (cells[0] if cells else None)
        rows.append(row)
    return rows


def fetch_groups(group: str = "sector", view: int = 110, order: str = "name",
                  sort_dir: str = "d1", page: object | None = None) -> list[dict]:
    """Fetch one Groups view and return parsed rows. Pass an already-open
    `page` (from `finviz_driver._make_driver`) to reuse one browser across
    several group/view combinations in one run.

    Confirmed live 2026-09-05, `group="sector"`: 11 rows, e.g. Basic
    Materials — 288 stocks, $3047.15B market cap, P/E 21.78, Change % -0.73%,
    Volume 491.78M."""
    url = build_url(group=group, view=view, order=order, sort_dir=sort_dir)
    html = _get_rendered_html(url, page=page, content_marker="styled-row")
    if not html:
        logger.warning(f"[finviz_groups] Cloudflare did not clear for {url}")
        return []
    rows = _parse_group_rows(html)
    if not rows:
        logger.info(f"[finviz_groups] No rows parsed for {url} — bare/unfiltered "
                    f"URLs are known to render filter controls only, no results")
    return rows
