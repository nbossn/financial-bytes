"""
finviz_calendar.py — Finviz's Economic Calendar (`calendar.ashx`), via the
same Cloudflare-clearing browser as the screener/groups modules.

## Why this exists

The picker's macro overlay currently has no real economic-release calendar
at all. Finviz's Calendar page gives real event data (release name, impact
severity, actual/expected/prior values) that the picker could use as an
event-risk flag around scheduled macro releases.

## What's confirmed live (2026-09-05)

`calendar.ashx` with no query params renders **one `<table class="styled-
table-new">` per day** (5 tables seen on one load: Mon-Fri of the current
week), each with its own `<thead>` whose first `<th>` is a date LABEL
(e.g. `"TueSep 01"` — weekday and month glued with no space, no year) rather
than a generic "Date" column header. Real column set: `Release, Impact, For,
Actual, Expected, Prior, Alerts` (the first column is a per-row TIME, e.g.
`"10:30 AM"`, not part of the header's own label).

The `Impact` cell has NO text — it's an `<svg><use
href="...#impactDark{N}">`, an icon reference, not a table cell value. This
module reads the fragment (`impactDark2` etc.) as the impact code rather
than silently returning an empty string for every row, which is what a
plain `td.get_text()` would do.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup
from loguru import logger

from src.stockpicker.finviz_driver import _get_rendered_html

FINVIZ_CALENDAR_URL = "https://finviz.com/calendar.ashx"

_DATE_LABEL_RE = re.compile(r"^([A-Za-z]{3})([A-Za-z]+)\s*(\d{1,2})$")


def _split_date_label(label: str) -> dict:
    """Split a glued date-table header like 'TueSep 01' into parts. Returns
    the raw label unchanged under `raw` regardless of whether the split
    succeeds, since Finviz gives no year and the exact glue format isn't
    guaranteed to hold for every locale/week Finviz might render."""
    m = _DATE_LABEL_RE.match(label.strip())
    if not m:
        return {"raw": label, "weekday": None, "month": None, "day": None}
    return {"raw": label, "weekday": m.group(1), "month": m.group(2), "day": m.group(3)}


def _impact_code(td) -> str | None:
    """Read the Impact cell's icon reference (e.g. 'impactDark2') instead of
    its text, which is always empty — confirmed live: the cell is
    `<svg><use href=".../icons_calendar.svg#impactDarkN">`, no text node."""
    use = td.find("use")
    if not use:
        return None
    href = use.get("href") or use.get("xlink:href") or ""
    frag = href.split("#")[-1] if "#" in href else None
    return frag or None


def _parse_calendar_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict] = []
    for table in soup.find_all("table", class_="styled-table-new"):
        thead = table.find("thead")
        ths = thead.find_all("th") if thead else []
        if not ths:
            continue
        date_info = _split_date_label(ths[0].get_text(strip=True))
        # Header labels after the date column: Release, Impact, For, Actual,
        # Expected, Prior, Alerts (confirmed live) — read live rather than
        # hardcoded, in case a future column is added/reordered.
        col_labels = [th.get_text(strip=True) for th in ths[1:]]
        tbody = table.find("tbody")
        if not tbody:
            continue
        for tr in tbody.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            event: dict = {"date": date_info, "time": tds[0].get_text(strip=True)}
            for i, label in enumerate(col_labels):
                td_idx = i + 1
                if td_idx >= len(tds):
                    continue
                key = label.strip().lower().replace(" ", "_") or f"col_{i}"
                if key == "impact":
                    event["impact"] = _impact_code(tds[td_idx])
                else:
                    event[key] = tds[td_idx].get_text(strip=True)
            events.append(event)
    return events


def fetch_calendar(page: object | None = None) -> list[dict]:
    """Fetch Finviz's Economic Calendar (default range — confirmed live to be
    the current week, Mon-Fri, 5 per-day tables) and return a flat list of
    event dicts, each tagged with its own day's date label.

    Confirmed live 2026-09-05: 73 events across 5 days (3/17/19/22/12 rows
    Mon-Fri), e.g. {'date': {'raw': 'TueSep 01', 'weekday': 'Tue', 'month':
    'Sep', 'day': '01'}, 'time': '10:30 AM', 'release': 'Dallas Fed
    Manufacturing Index', 'impact': 'impactDark2', 'for': 'Aug', 'actual':
    '11.6', 'expected': '-', 'prior': '1.3', 'alerts': ''}."""
    html = _get_rendered_html(FINVIZ_CALENDAR_URL, page=page,
                                content_marker="styled-table-new")
    if not html:
        logger.warning("[finviz_calendar] Cloudflare did not clear for calendar.ashx")
        return []
    events = _parse_calendar_html(html)
    if not events:
        logger.info("[finviz_calendar] Page rendered but no events parsed")
    return events
