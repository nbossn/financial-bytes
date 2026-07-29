"""insider_news.py — the two signals that carried weight but were never computed.

``insider_cluster`` and ``sentiment`` both appear in ``IC_PRIORS`` (and so in
``PRIOR_W``, which is what the composite actually uses), together holding
**10.9% of the effective weight vector**. Neither had a ``contrib[...]`` line
anywhere in ``run.py``, so both contributed exactly nothing to all 706 scored
rows. ``run.py``'s own comment records that this same bug was found and fixed
once before for four sibling signals — that sweep missed these two.

Both are derived from the finviz quote page that ``finviz_data.fetch`` already
downloads for every candidate. **No new HTTP request, no API key, no new
rate-limit surface** — the page was always being fetched and two of its tables
were being thrown away.

Design notes that are not obvious from the field names
------------------------------------------------------

*Insider.* Finviz lists a Form 144 ``Proposed Sale`` **alongside** the executed
``Sale`` for the same shares, so counting both double-counts one decision. It
also prints the same person under different spellings on the same page
("Frank D. Yeary" / "FRANK D YEARY"), which splits one insider into two. And
the Lakonishok-Lee result is about *how many insiders agree*, not how many
forms were filed — ten sales by one officer is one seller. All three are
handled here; each has a test.

*Sentiment.* VADER is a general-purpose social-media lexicon and scores
"beats earnings estimates, raises guidance" at exactly **0.000** — the most
common positive headline shape in equity news. It is augmented below with a
finance term list in the spirit of Loughran-McDonald. A half-life decay
implements the "noisy, decays fast" caveat attached to the 0.025 prior.

*Both* return ``None`` — never ``0.0`` — when they have no data. This is
load-bearing: ``engine.cross_sectional_z`` maps an all-NaN series to
``Series(0.0)``, so a signal that returns a number when it knows nothing is
indistinguishable from one that legitimately scored flat. That fail-open is
precisely how ``momentum_12_1`` stayed dead across 16 runs and 40 green tests.
"""
from __future__ import annotations

import datetime as dt
import re
from functools import lru_cache

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
INSIDER_WINDOW_DAYS = 90     # Lakonishok-Lee cluster window (~1 quarter)
# Shrinkage pseudo-count for the cluster score. The raw net-purchase ratio is
# sign-only and saturates: measured live over 8 large caps, 7 scored exactly
# -1.0 because routine comp-driven selling is near-universal, leaving 2 distinct
# values across the cross-section — which after z-scoring is barely
# distinguishable from the dead signal this module exists to fix. Scaling by
# n/(n+k) makes the score grow with the number of insiders who agree, which is
# what "cluster" means in Lakonishok-Lee anyway: one seller is weak evidence,
# six sellers is not.
INSIDER_SHRINKAGE_K = 2.0
NEWS_WINDOW_DAYS = 14        # headlines older than this are dropped entirely
NEWS_HALF_LIFE_DAYS = 3.0    # decay: a 3-day-old headline counts half

# Transactions that represent an actual executed decision by an insider.
# 'Proposed Sale' is a Form 144 intent and is listed next to the Sale it
# anticipates; 'Option Exercise' is compensation, not a market view.
_REAL_BUYS = {"buy"}
_REAL_SELLS = {"sale", "sell"}

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_JUNK = {"loading", "loading…", "loading...", "", "-"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _soup(html) -> BeautifulSoup | None:
    if html is None:
        return None
    if isinstance(html, BeautifulSoup):
        return html
    return BeautifulSoup(str(html), "html.parser")


def _num(text: str) -> float | None:
    """'8,294,275' -> 8294275.0 ; '' -> None."""
    if not text:
        return None
    cleaned = re.sub(r"[,$%\s]", "", text)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _insider_key(name: str) -> tuple[str, ...]:
    """Canonical identity for one insider.

    Uppercase, drop punctuation, drop single-letter initials, sort the
    remaining tokens. 'Frank D. Yeary' and 'FRANK D YEARY' collapse to
    ``('FRANK', 'YEARY')``.

    Deliberately *exact* on the token set rather than subset-matching: merging
    on subsets would fuse genuinely different people who share a surname, and
    understating the cluster count is the more damaging error for a signal
    whose whole content is "how many insiders agree".
    """
    tokens = [t for t in re.split(r"[^A-Za-z]+", name.upper()) if len(t) > 1]
    return tuple(sorted(tokens))


# ---------------------------------------------------------------------------
# Insider table
# ---------------------------------------------------------------------------
def parse_insider_date(text: str, as_of: dt.date | None = None) -> dt.date | None:
    """Parse finviz's ``Jul 24 '26`` insider date form."""
    if not text:
        return None
    m = re.search(r"([A-Za-z]{3})\s+(\d{1,2})\s*'\s*(\d{2})", text)
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if mon:
            return dt.date(2000 + int(m.group(3)), mon, int(m.group(2)))
    # Year-less fallback ('Jul 24') — assume the most recent such date at or
    # before as_of, so a January run does not read December as the future.
    m = re.search(r"([A-Za-z]{3})\s+(\d{1,2})\b", text)
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if mon:
            ref = as_of or dt.date.today()
            try:
                cand = dt.date(ref.year, mon, int(m.group(2)))
            except ValueError:
                return None
            return cand if cand <= ref else cand.replace(year=ref.year - 1)
    return None


def parse_insider_table(html, as_of: dt.date | None = None) -> list[dict]:
    """Rows of the finviz insider-trading table (``table.body-table``)."""
    soup = _soup(html)
    if soup is None:
        return []
    table = soup if soup.name == "table" else soup.find("table", class_="body-table")
    if table is None:
        return []

    out: list[dict] = []
    for tr in table.find_all("tr"):
        if tr.find("th"):
            continue
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 7:
            continue                       # placeholder / colspan junk rows
        name, rel, date_s, txn = cells[0], cells[1], cells[2], cells[3]
        if name.strip().lower() in _JUNK or not txn:
            continue
        day = parse_insider_date(date_s, as_of=as_of)
        if day is None:
            continue
        out.append({
            "insider": name,
            "relationship": rel,
            "date": day,
            "transaction": txn,
            "cost": _num(cells[4]),
            "shares": _num(cells[5]),
            "value": _num(cells[6]),
        })
    return out


def insider_cluster_score(rows: list[dict],
                          as_of: dt.date | None = None,
                          window_days: int = INSIDER_WINDOW_DAYS,
                          shrinkage_k: float = INSIDER_SHRINKAGE_K) -> float | None:
    """Confidence-scaled net purchase ratio over **distinct insiders**.

    ``(buyers - sellers) / (buyers + sellers) * n / (n + k)`` in [-1, +1],
    where ``n`` is the number of distinct insiders transacting; ``None`` when
    no executed transaction falls in the window.

    The ``n/(n+k)`` term is what makes this a *cluster* score rather than a
    sign: six insiders selling is stronger evidence than one, and without it
    both score -1.0 and the signal carries almost no cross-sectional
    information (see INSIDER_SHRINKAGE_K).
    """
    as_of = as_of or dt.date.today()
    cutoff = as_of - dt.timedelta(days=window_days)

    buyers: set[tuple[str, ...]] = set()
    sellers: set[tuple[str, ...]] = set()
    for r in rows:
        day = r.get("date")
        if not isinstance(day, dt.date) or not (cutoff <= day <= as_of):
            continue
        txn = str(r.get("transaction", "")).strip().lower()
        key = _insider_key(str(r.get("insider", "")))
        if not key:
            continue
        if txn in _REAL_BUYS:
            buyers.add(key)
        elif txn in _REAL_SELLS:
            sellers.add(key)
        # everything else (Proposed Sale, Option Exercise, ...) is not a view

    total = len(buyers) + len(sellers)
    if total == 0:
        return None
    ratio = (len(buyers) - len(sellers)) / total
    return ratio * (total / (total + shrinkage_k))


# ---------------------------------------------------------------------------
# News table
# ---------------------------------------------------------------------------
_NEWS_DATE_RE = re.compile(
    r"^\s*(?:(Today|Yesterday)|([A-Za-z]{3})-(\d{1,2})-(\d{2}))?\s*"
    r"(\d{1,2}):(\d{2})\s*([AP]M)", re.I)


def parse_news_datetime(text: str, last_date: dt.date | None,
                        as_of: dt.date | None = None):
    """Parse one finviz news timestamp cell.

    Finviz prints the date only on the first headline of each day and bare
    times thereafter, so ``last_date`` carries it forward. Returns
    ``(datetime | None, date | None)`` — the second value is the new carry.
    """
    as_of = as_of or dt.date.today()
    m = _NEWS_DATE_RE.match(text or "")
    if not m:
        return None, last_date

    word, mon_s, day_s, yr_s, hh, mm, ampm = m.groups()
    if word:
        day = as_of if word.lower() == "today" else as_of - dt.timedelta(days=1)
    elif mon_s:
        mon = _MONTHS.get(mon_s.lower())
        if not mon:
            return None, last_date
        day = dt.date(2000 + int(yr_s), mon, int(day_s))
    else:
        day = last_date
    if day is None:
        return None, last_date

    hour = int(hh) % 12 + (12 if ampm.upper() == "PM" else 0)
    return dt.datetime.combine(day, dt.time(hour, int(mm))), day


def parse_news_table(html, as_of: dt.date | None = None) -> list[dict]:
    """Headlines from the finviz news table (``table.news-table``)."""
    soup = _soup(html)
    if soup is None:
        return []
    table = soup if soup.name == "table" else soup.find("table", class_="news-table")
    if table is None:
        return []

    out: list[dict] = []
    carry: dt.date | None = None
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        ts, carry = parse_news_datetime(tds[0].get_text(strip=True), carry, as_of=as_of)
        if ts is None:
            continue

        link = tds[1].find("a")
        headline = (link.get_text(strip=True) if link
                    else tds[1].get_text(strip=True))
        if headline.strip().lower().rstrip(".…") in _JUNK or len(headline) < 8:
            continue

        # The publisher is rendered after the link, e.g. '(Investopedia)'.
        source = ""
        span = tds[1].find("span")
        if span:
            source = span.get_text(strip=True).strip("()")
        else:
            m = re.search(r"\(([^()]{2,40})\)\s*$", headline)
            if m:
                source = m.group(1)
                headline = headline[:m.start()].strip()

        out.append({"ts": ts, "headline": headline, "source": source})
    return out


# ---------------------------------------------------------------------------
# Sentiment
# ---------------------------------------------------------------------------
# Finance terms VADER's general-purpose lexicon either lacks or misreads.
# Scale matches VADER's own (roughly -4.0 .. +4.0).
FINANCE_LEXICON: dict[str, float] = {
    # results vs expectations
    "beat": 2.2, "beats": 2.5, "crushes": 2.6, "tops": 2.0, "topped": 1.8,
    "miss": -2.0, "misses": -2.4, "missed": -2.0, "shortfall": -2.2,
    # guidance / outlook
    "raises": 1.8, "raised": 1.6, "boosts": 2.0, "hikes": 1.5,
    "cuts": -2.0, "cut": -1.6, "slashes": -2.6, "lowers": -1.8,
    "warns": -2.4, "warning": -2.0, "guidance": 0.0,
    # analyst actions
    "upgrade": 2.4, "upgrades": 2.5, "upgraded": 2.3,
    "downgrade": -2.4, "downgrades": -2.5, "downgraded": -2.3,
    "outperform": 2.0, "underperform": -2.0,
    "overweight": 1.5, "underweight": -1.5,
    "bullish": 2.5, "bearish": -2.5,
    # price action
    "surges": 2.8, "soars": 3.0, "rallies": 2.4, "jumps": 2.2, "climbs": 1.8,
    "plunges": -3.0, "tumbles": -2.7, "slumps": -2.5, "sinks": -2.5,
    "plummets": -3.0, "selloff": -2.2, "pullback": -1.6, "slides": -1.8,
    # corporate events
    "buyback": 1.8, "dividend": 1.2, "acquisition": 0.8, "record": 1.5,
    "bankruptcy": -3.4, "lawsuit": -2.0, "probe": -1.8, "investigation": -1.8,
    "recall": -2.0, "layoffs": -2.0, "halts": -1.8, "delisting": -3.0,
    "downside": -1.8, "upside": 1.8, "bubble": -1.5, "fears": -2.0,
}


@lru_cache(maxsize=1)
def _analyzer():
    """VADER with the finance lexicon folded in. ``None`` if unavailable.

    Returning ``None`` rather than raising means a missing dependency degrades
    the signal to "no data" (and so to NaN, which ``all_nan_signals`` reports)
    instead of taking down the whole nightly run — or, worse, quietly scoring
    every ticker 0.0.
    """
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    except ImportError:                                   # pragma: no cover
        print("[insider_news] WARNING: vaderSentiment not installed — "
              "`sentiment` will be reported as a dead signal")
        return None
    a = SentimentIntensityAnalyzer()
    a.lexicon.update(FINANCE_LEXICON)
    return a


def headline_sentiment(text: str) -> float | None:
    """VADER compound score in [-1, 1] for one headline."""
    a = _analyzer()
    if a is None:
        return None
    return a.polarity_scores(text)["compound"]


def news_sentiment_score(items: list[dict],
                         as_of: dt.date | None = None,
                         window_days: int = NEWS_WINDOW_DAYS,
                         half_life_days: float = NEWS_HALF_LIFE_DAYS) -> float | None:
    """Half-life-decayed mean headline sentiment in [-1, 1], or ``None``."""
    if _analyzer() is None:
        return None
    as_of = as_of or dt.date.today()
    ref = dt.datetime.combine(as_of, dt.time(23, 59))

    num = 0.0
    den = 0.0
    for it in items:
        ts = it.get("ts")
        if not isinstance(ts, dt.datetime):
            continue
        age = (ref - ts).total_seconds() / 86400.0
        if age < 0 or age > window_days:
            continue
        s = headline_sentiment(it.get("headline", ""))
        if s is None:
            continue
        w = 0.5 ** (age / half_life_days)
        num += w * s
        den += w

    if den == 0:
        return None
    return max(-1.0, min(1.0, num / den))


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------
def signals_from_html(insider_html=None, news_html=None,
                      as_of: dt.date | None = None) -> dict:
    """Both signals from the two tables of one already-fetched quote page.

    Keys are always present; a value is ``None`` when that table was missing or
    carried nothing usable.
    """
    insider = insider_cluster_score(
        parse_insider_table(insider_html, as_of=as_of), as_of=as_of)
    sentiment = news_sentiment_score(
        parse_news_table(news_html, as_of=as_of), as_of=as_of)
    return {"insider_cluster": insider, "sentiment": sentiment}
