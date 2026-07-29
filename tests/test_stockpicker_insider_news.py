"""Tests for the two weighted-but-never-computed signals: insider_cluster, sentiment.

Both carry weight in IC_PRIORS and, until now, had no ``contrib[...]`` line in
run.py at all — 10.9% of the effective (PRIOR_W) weight vector contributing
literally nothing to 706 scored rows.

Two properties are load-bearing throughout and are asserted repeatedly:

1. **A missing signal must be ``None``, never ``0.0``.** ``cross_sectional_z``
   maps an all-NaN series to ``Series(0.0)``, so a signal that silently returns
   a number when it has no data is indistinguishable from one that legitimately
   scored flat. That fail-open is exactly how momentum_12_1 stayed dead across
   16 runs and 40 green tests.
2. **Every parser has a positive control.** A checker that has never been shown
   able to return a non-zero count is not a measurement.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from src.stockpicker import insider_news as ins

FIX = Path(__file__).parent / "fixtures" / "finviz"

# The fixtures were captured 2026-07-28; every window assertion is relative to
# this date rather than "now", so the suite does not rot into a false pass.
AS_OF = dt.date(2026, 7, 28)


def _fx(name: str) -> str:
    return FIX.joinpath(name).read_text()


# ---------------------------------------------------------------------------
# Insider table parsing
# ---------------------------------------------------------------------------

def test_parses_the_real_insider_fixture():
    rows = ins.parse_insider_table(_fx("mu_insider.html"))
    # Positive control: the parser must actually find rows in a real page.
    assert len(rows) >= 40, f"parser found only {len(rows)} rows — it is broken"
    for r in rows:
        assert r["insider"] and r["transaction"]
        assert isinstance(r["date"], dt.date)


def test_parses_the_smaller_fixture_and_finds_the_one_buy():
    rows = ins.parse_insider_table(_fx("intc_insider.html"))
    assert len(rows) >= 8
    buys = [r for r in rows if r["transaction"] == "Buy"]
    assert len(buys) == 1
    assert "ZINSNER" in buys[0]["insider"].upper()
    assert buys[0]["date"] == dt.date(2026, 1, 26)


def test_apostrophe_year_dates_parse():
    rows = ins.parse_insider_table(_fx("mu_insider.html"))
    assert dt.date(2026, 7, 24) in {r["date"] for r in rows}


def test_junk_rows_are_dropped_not_counted():
    html = """<table class="body-table">
      <tr><th>Insider Trading</th><th>Relationship</th><th>Date</th>
          <th>Transaction</th><th>Cost</th><th>#Shares</th><th>Value ($)</th></tr>
      <tr><td colspan="7">Loading…</td></tr>
    </table>"""
    assert ins.parse_insider_table(html) == []


# ---------------------------------------------------------------------------
# Insider cluster score
# ---------------------------------------------------------------------------

def _row(insider, txn, day, shares=100, value=1000):
    return {"insider": insider, "relationship": "Officer", "date": day,
            "transaction": txn, "cost": 10.0, "shares": shares, "value": value}


def test_a_named_insider_with_a_real_sale_does_score():
    """Control for the two exclusion tests below.

    Without this, 'returns None' there is ambiguous: a name whose tokens are
    all single letters normalizes to an empty key and is dropped *before* the
    transaction type is ever consulted, so the exclusion under test would never
    run and the assertion would pass for the wrong reason. (It did, until a
    mutation that should have broken those tests survived.)
    """
    rows = [_row("Ann Smith", "Sale", dt.date(2026, 7, 20))]
    # one seller: -1 * 1/(1+2) = -1/3
    assert ins.insider_cluster_score(rows, as_of=AS_OF) == pytest.approx(-1 / 3)


def test_no_qualifying_transactions_returns_none_not_zero():
    """The whole point. 0.0 is a legitimate score; 'no data' is not 0.0."""
    assert ins.insider_cluster_score([], as_of=AS_OF) is None


def test_proposed_sales_alone_are_not_data():
    """A Form 144 'Proposed Sale' is an intent to sell, and finviz lists it
    *alongside* the executed Sale for the same shares. Counting both
    double-counts one decision; counting only proposals is counting nothing."""
    rows = [_row("Ann Smith", "Proposed Sale", dt.date(2026, 7, 20))]
    assert ins.insider_cluster_score(rows, as_of=AS_OF) is None


def test_option_exercises_are_not_signal():
    rows = [_row("Ann Smith", "Option Exercise", dt.date(2026, 7, 20))]
    assert ins.insider_cluster_score(rows, as_of=AS_OF) is None


def test_unanimous_buying_is_positive_unanimous_selling_negative():
    buys = [_row("Ann Smith", "Buy", dt.date(2026, 7, 20)),
            _row("Bob Jones", "Buy", dt.date(2026, 7, 21))]
    sells = [_row("Ann Smith", "Sale", dt.date(2026, 7, 20)),
             _row("Bob Jones", "Sale", dt.date(2026, 7, 21))]
    assert ins.insider_cluster_score(buys, as_of=AS_OF) > 0
    assert ins.insider_cluster_score(sells, as_of=AS_OF) < 0


def test_a_bigger_cluster_scores_harder_than_a_lone_insider():
    """The saturation fix. Without the n/(n+k) term both of these are exactly
    -1.0, and a signal that is -1.0 for almost every name carries no
    cross-sectional information once z-scored — which is the dead-signal state
    this module was written to end."""
    one = [_row("Ann Smith", "Sale", dt.date(2026, 7, 20))]
    six = [_row(f"Person{chr(65 + i)} Surname{chr(65 + i)}", "Sale", dt.date(2026, 7, 20))
           for i in range(6)]
    s_one = ins.insider_cluster_score(one, as_of=AS_OF)
    s_six = ins.insider_cluster_score(six, as_of=AS_OF)
    assert s_six < s_one < 0
    assert s_six == pytest.approx(-6 / 8)
    assert -1.0 <= s_six <= 1.0


def test_cluster_score_never_leaves_the_unit_interval():
    huge = [_row(f"Person{i} Surname{i}", "Buy", dt.date(2026, 7, 20)) for i in range(200)]
    assert 0 < ins.insider_cluster_score(huge, as_of=AS_OF) < 1.0


def test_score_counts_distinct_insiders_not_transactions():
    """Lakonishok-Lee cluster: it is *how many insiders* agree, not how many
    trades were filed. Ten sales by one officer is one seller."""
    many = [_row("Ann Smith", "Sale", dt.date(2026, 7, 10 + i)) for i in range(10)]
    many.append(_row("Bob Jones", "Buy", dt.date(2026, 7, 20)))
    # 1 buyer, 1 seller -> 0.0, not -9/11.
    assert ins.insider_cluster_score(many, as_of=AS_OF) == pytest.approx(0.0)


def test_name_spelling_variants_are_one_insider():
    """finviz prints the same person two ways on the same page — 'Miller Boise
    April' and 'APRIL V BOISE'. Unnormalized, one person becomes two."""
    rows = [_row("Frank D. Yeary", "Buy", dt.date(2026, 7, 20)),
            _row("FRANK D YEARY", "Buy", dt.date(2026, 7, 21)),
            _row("Someone Else", "Sale", dt.date(2026, 7, 21))]
    # 1 distinct buyer, 1 distinct seller -> 0.0. If the variants counted as
    # two buyers the score would be +1/3.
    assert ins.insider_cluster_score(rows, as_of=AS_OF) == pytest.approx(0.0)


def test_transactions_outside_the_window_are_excluded():
    old = [_row("Ann Smith", "Buy", dt.date(2025, 1, 5))]
    assert ins.insider_cluster_score(old, as_of=AS_OF, window_days=90) is None
    recent = [_row("Ann Smith", "Buy", dt.date(2026, 7, 20))]
    assert ins.insider_cluster_score(recent, as_of=AS_OF, window_days=90) == pytest.approx(1 / 3)


def test_real_fixture_produces_a_real_score():
    """End-to-end on captured bytes: parse -> score, and it must not be None."""
    rows = ins.parse_insider_table(_fx("mu_insider.html"))
    score = ins.insider_cluster_score(rows, as_of=AS_OF, window_days=90)
    assert score is not None
    assert -1.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# News parsing
# ---------------------------------------------------------------------------

def test_parses_the_real_news_fixture():
    items = ins.parse_news_table(_fx("mu_news.html"), as_of=AS_OF)
    assert len(items) >= 50, f"parser found only {len(items)} headlines — broken"
    for it in items:
        assert it["headline"]
        assert isinstance(it["ts"], dt.datetime)


def test_date_is_carried_forward_across_time_only_rows():
    """finviz prints the date once per day, then bare times. A parser that
    ignores this either drops most rows or dates them all today."""
    html = """<table class="news-table">
      <tr><td>Jul-25-26 09:01PM</td><td><a href="#">Alpha rises</a><span>(X)</span></td></tr>
      <tr><td>07:55PM</td><td><a href="#">Beta falls</a><span>(Y)</span></td></tr>
    </table>"""
    items = ins.parse_news_table(html, as_of=AS_OF)
    assert len(items) == 2
    assert {i["ts"].date() for i in items} == {dt.date(2026, 7, 25)}


def test_today_keyword_resolves_to_as_of():
    html = """<table class="news-table">
      <tr><td>Today 09:01PM</td><td><a href="#">Alpha rises</a></td></tr>
    </table>"""
    items = ins.parse_news_table(html, as_of=AS_OF)
    assert items[0]["ts"].date() == AS_OF


def test_placeholder_rows_are_dropped():
    html = """<table class="news-table">
      <tr><td>Today 04:00PM</td><td>Loading…</td></tr>
      <tr><td>Today 03:00PM</td><td><a href="#">Real headline here</a></td></tr>
    </table>"""
    items = ins.parse_news_table(html, as_of=AS_OF)
    assert [i["headline"] for i in items] == ["Real headline here"]


def test_source_is_extracted_from_the_span_on_the_real_page():
    items = ins.parse_news_table(_fx("mu_news.html"), as_of=AS_OF)
    assert any(i["source"] for i in items), "no source ever extracted"
    # Sources are publisher names, never a whole headline.
    assert all(len(i["source"]) < 40 for i in items)


def test_source_suffix_is_split_off_when_there_is_no_span():
    """The span-less shape is the layout-drift fallback. The captured page
    always uses a span, so without this test that branch is never executed by
    the suite at all — a mutation deleting it survived, which is how it was
    found."""
    html = """<table class="news-table">
      <tr><td>Today 09:01PM</td><td><a href="#">Alpha rises on strong demand(Reuters)</a></td></tr>
    </table>"""
    items = ins.parse_news_table(html, as_of=AS_OF)
    assert len(items) == 1
    assert items[0]["source"] == "Reuters"
    assert items[0]["headline"] == "Alpha rises on strong demand"


# ---------------------------------------------------------------------------
# News sentiment score
# ---------------------------------------------------------------------------

def _news(headline, when):
    return {"ts": dt.datetime.combine(when, dt.time(12, 0)),
            "headline": headline, "source": "X"}


def test_no_news_in_window_returns_none_not_zero():
    assert ins.news_sentiment_score([], as_of=AS_OF) is None
    stale = [_news("Alpha rises", dt.date(2026, 1, 1))]
    assert ins.news_sentiment_score(stale, as_of=AS_OF, window_days=14) is None


def test_positive_and_negative_headlines_score_with_the_right_sign():
    pos = [_news("Company crushes estimates and raises guidance", dt.date(2026, 7, 28))]
    neg = [_news("Company slashes guidance after brutal earnings miss", dt.date(2026, 7, 28))]
    assert ins.news_sentiment_score(pos, as_of=AS_OF) > 0
    assert ins.news_sentiment_score(neg, as_of=AS_OF) < 0


def test_finance_terms_that_vanilla_vader_scores_neutral_are_scored():
    """VADER is a general-purpose social-media lexicon. On its own it scores
    'beats earnings estimates, raises guidance' at exactly 0.000 — the single
    most common *positive* headline shape in equity news. The finance lexicon
    augmentation is the difference between a signal and noise."""
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    text = "Micron beats earnings estimates, raises guidance"
    vanilla = SentimentIntensityAnalyzer().polarity_scores(text)["compound"]
    assert vanilla == pytest.approx(0.0, abs=1e-9), (
        "vanilla VADER changed; the premise of this test needs re-checking")
    assert ins.headline_sentiment(text) > 0.2


def test_downgrade_and_upgrade_are_directional():
    assert ins.headline_sentiment("Analyst downgrades stock to sell") < 0
    assert ins.headline_sentiment("Analyst upgrades stock to buy") > 0


def test_recent_news_outweighs_older_news():
    """'noisy, decays fast' is in the prior's own description — a 10-day-old
    headline must not cancel today's."""
    items = [_news("Company crushes estimates and raises guidance", dt.date(2026, 7, 28)),
             _news("Company slashes guidance after brutal earnings miss", dt.date(2026, 7, 18))]
    # Equal-weighted this is ~0; decayed it must stay clearly positive.
    assert ins.news_sentiment_score(items, as_of=AS_OF, window_days=14) > 0.1


def test_score_is_bounded():
    items = [_news("crushes estimates raises guidance surges soars", dt.date(2026, 7, 28))] * 20
    s = ins.news_sentiment_score(items, as_of=AS_OF)
    assert -1.0 <= s <= 1.0


def test_real_fixture_produces_a_real_sentiment():
    items = ins.parse_news_table(_fx("mu_news.html"), as_of=AS_OF)
    s = ins.news_sentiment_score(items, as_of=AS_OF, window_days=14)
    assert s is not None
    assert -1.0 <= s <= 1.0


# ---------------------------------------------------------------------------
# Wiring: the signals must reach the composite
# ---------------------------------------------------------------------------

def test_enrich_payload_exposes_both_signals():
    """Given a page, the finviz adapter must surface both keys — present with a
    None value when there is no data, rather than absent."""
    payload = ins.signals_from_html(
        insider_html=_fx("intc_insider.html"),
        news_html=_fx("intc_news.html"),
        as_of=AS_OF,
    )
    assert set(payload) == {"insider_cluster", "sentiment"}
    assert payload["sentiment"] is not None


def test_missing_page_yields_none_for_both():
    payload = ins.signals_from_html(insider_html=None, news_html=None, as_of=AS_OF)
    assert payload == {"insider_cluster": None, "sentiment": None}
