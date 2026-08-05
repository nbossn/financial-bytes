"""A price that could not be fetched must not become a number.

Two stacked fail-opens put a $62K equity position into Nick's newsletter as
"near-inert" cash, every day for 41 days:

  1. `BRKB` is what Fidelity calls Berkshire Hathaway Class B. Yahoo calls it
     `BRK-B`. yfinance answers the broker form with an EMPTY frame rather than
     raising, so every one of the 27 market-data call sites got nothing back
     and none of them said so.

  2. `PortfolioSnapshot.total_value` substitutes `cost_basis` for a missing
     price. Cost basis is the one value that makes unrealized P&L exactly
     zero — so "we could not price this" renders identically to "this position
     has not moved."

The second is the durable one: a missing price was converted into a value, and
the value it was converted into is the one that looks like a normal answer.
Fixing the symbol closes today's instance; making the absence loud closes the
class.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.api.symbols import (
    BROKER_TO_YAHOO,
    resolve_symbol,
    to_yahoo_symbol,
)
from src.portfolio.models import Holding, PortfolioSnapshot


def _h(ticker: str, shares: str, cost: str) -> Holding:
    return Holding(ticker=ticker, shares=Decimal(shares), cost_basis=Decimal(cost))


# ── The symbol map itself ────────────────────────────────────────


class TestSymbolMap:
    def test_brkb_is_the_live_case(self):
        """Nick holds BRKB in two portfolios. This is the entry that matters."""
        assert to_yahoo_symbol("BRKB") == "BRK-B"

    def test_every_mapping_was_verified_against_yahoo(self):
        """Each entry resolved to the right company on 2026-08-05 (see module docstring).

        A mapping is more dangerous than a missing one: an unverified guess
        would fabricate a *wrong company's* price rather than no price. So the
        map is a closed, checked set, not a pattern.
        """
        assert BROKER_TO_YAHOO == {
            "BRKA": "BRK-A",
            "BRKB": "BRK-B",
            "BFA": "BF-A",
            "BFB": "BF-B",
            "LENB": "LEN-B",
            "HEIA": "HEI-A",
            "PBRA": "PBR-A",
        }

    def test_unmapped_tickers_pass_through_unchanged(self):
        for t in ("MSFT", "NVDA", "SPY", "BTC-USD", "^VIX", "SPAXX"):
            assert to_yahoo_symbol(t) == t

    def test_a_ticker_ending_in_a_or_b_is_not_rewritten_by_pattern(self):
        """NVDA ends in 'A'. A regex-based rule would break it.

        This is why the map is explicit: the broker form of a dual-class
        ticker is not distinguishable from an ordinary ticker by shape.
        """
        assert to_yahoo_symbol("NVDA") == "NVDA"
        assert to_yahoo_symbol("TSLA") == "TSLA"
        assert to_yahoo_symbol("META") == "META"

    def test_lookup_is_case_insensitive_but_preserves_unmapped_input(self):
        assert to_yahoo_symbol("brkb") == "BRK-B"
        # An unmapped symbol is handed back byte-identical — callers may be
        # passing something Yahoo-specific we should not touch.
        assert to_yahoo_symbol("btc-usd") == "btc-usd"

    def test_blank_and_none_are_returned_unchanged(self):
        assert to_yahoo_symbol("") == ""
        assert to_yahoo_symbol(None) is None

    def test_no_mapping_targets_the_broker_form_of_another_entry(self):
        """Guard against a map that would send one holding to another's symbol."""
        assert not (set(BROKER_TO_YAHOO.values()) & set(BROKER_TO_YAHOO))

    def test_all_yahoo_forms_carry_the_separator(self):
        """Every value differs from its key by exactly a hyphen insertion."""
        for broker, yahoo in BROKER_TO_YAHOO.items():
            assert "-" in yahoo
            assert yahoo.replace("-", "") == broker


# ── resolve_symbol: the reporting half ───────────────────────────


class TestResolveSymbol:
    def test_reports_that_a_rewrite_happened(self):
        resolved, rewritten = resolve_symbol("BRKB")
        assert resolved == "BRK-B"
        assert rewritten is True

    def test_reports_that_no_rewrite_happened(self):
        resolved, rewritten = resolve_symbol("MSFT")
        assert resolved == "MSFT"
        assert rewritten is False


# ── PortfolioSnapshot: absence must be representable ─────────────


class TestMissingPricesAreVisible:
    def test_missing_prices_lists_holdings_with_no_price(self):
        snap = PortfolioSnapshot(
            holdings=[_h("MSFT", "10", "100"), _h("BRKB", "120", "487")],
            prices={"MSFT": Decimal("150")},
        )
        assert snap.missing_prices == ["BRKB"]

    def test_missing_prices_is_empty_when_all_priced(self):
        snap = PortfolioSnapshot(
            holdings=[_h("MSFT", "10", "100")],
            prices={"MSFT": Decimal("150")},
        )
        assert snap.missing_prices == []

    def test_every_holding_is_missing_when_the_whole_fetch_failed(self):
        """The failure mode nobody would notice: prices={} reports +0.00% overall.

        `_fetch_prices` returns {} when every call fails, and the pipeline's
        `if market_prices:` guard then builds the snapshot with no prices at
        all. Total P&L becomes exactly zero and the newsletter reports a flat
        portfolio rather than an outage.
        """
        snap = PortfolioSnapshot(
            holdings=[_h("MSFT", "10", "100"), _h("NVDA", "5", "200")],
            prices={},
        )
        assert snap.missing_prices == ["MSFT", "NVDA"]
        assert snap.total_pnl == Decimal(0)
        assert snap.has_complete_prices is False

    def test_has_complete_prices_is_true_only_when_nothing_is_missing(self):
        priced = PortfolioSnapshot(
            holdings=[_h("MSFT", "10", "100")], prices={"MSFT": Decimal("150")}
        )
        assert priced.has_complete_prices is True

    def test_missing_prices_preserves_holding_order_and_does_not_dedupe_away_reality(self):
        """Order and multiplicity both have to survive.

        The obvious `sorted({...})` implementation passes an alphabetical
        fixture — the first version of this test used one and a mutation
        walked straight through it. Two things make it discriminate now:

        - `ZBRA` sorts *after* `BRKB`, so set-ordering is detectable.
        - The same ticker is held twice. That is not hypothetical: lilich has
          16 holding rows across 15 distinct tickers, because one position is
          split across two accounts. Deduping would under-count how much of
          the portfolio is being valued at cost basis.
        """
        snap = PortfolioSnapshot(
            holdings=[
                _h("MSFT", "10", "100"),
                _h("ZBRA", "1", "2"),
                _h("BRKB", "120", "487"),
                _h("BRKB", "27.613", "448.11"),
            ],
            prices={"MSFT": Decimal("150")},
        )
        assert snap.missing_prices == ["ZBRA", "BRKB", "BRKB"]
        assert len(snap.missing_prices) == 3

    def test_a_missing_price_still_falls_back_to_cost_basis(self):
        """Deliberately unchanged: cost basis remains the least-bad estimate.

        The defect was never that the fallback exists — it is that the fallback
        was indistinguishable from a real answer. The number stays; the silence
        does not.
        """
        snap = PortfolioSnapshot(
            holdings=[_h("BRKB", "120", "487")], prices={}
        )
        assert snap.total_value == Decimal("58440")

    def test_the_fallback_is_exactly_what_makes_pnl_zero(self):
        """Pins the mechanism, so a future change to the fallback fails here.

        120 shares at a real 517.22 is +$3,626. Priced at cost it is +$0.00 —
        which is what shipped to Nick as 'near-inert', beside SPAXX cash.
        """
        holdings = [_h("BRKB", "120", "487")]
        unpriced = PortfolioSnapshot(holdings=holdings, prices={})
        priced = PortfolioSnapshot(holdings=holdings, prices={"BRKB": Decimal("517.22")})

        assert unpriced.total_pnl == Decimal(0)
        assert priced.total_pnl == Decimal("3626.40")
        assert priced.total_pnl != unpriced.total_pnl


# ── _fetch_prices: an empty frame is a failure, and must say so ──


class _FakeHistory:
    def __init__(self, empty: bool, close: float = 0.0):
        self.empty = empty
        self._close = close

    def __getitem__(self, key):
        assert key == "Close"
        return self

    @property
    def iloc(self):
        return [self._close]


class _FakeTicker:
    """Records what symbol it was actually asked for."""

    asked: list[str] = []

    def __init__(self, symbol):
        _FakeTicker.asked.append(symbol)
        self._symbol = symbol

    def history(self, period=None, **kw):
        # Only the Yahoo form has data — exactly Yahoo's real behaviour.
        if self._symbol == "BRK-B":
            return _FakeHistory(empty=False, close=517.22)
        if self._symbol == "MSFT":
            return _FakeHistory(empty=False, close=500.0)
        return _FakeHistory(empty=True)


@pytest.fixture
def fake_yf(monkeypatch):
    import src.alerts.stop_loss as sl

    _FakeTicker.asked = []
    monkeypatch.setattr(sl.yf, "Ticker", _FakeTicker)
    return _FakeTicker


class TestFetchPricesResolvesAndReports:
    def test_broker_symbol_is_translated_before_the_fetch(self, fake_yf):
        from src.alerts.stop_loss import _fetch_prices

        prices = _fetch_prices(["BRKB"])
        assert "BRK-B" in fake_yf.asked, f"asked Yahoo for {fake_yf.asked}"

    def test_price_is_keyed_by_the_brokers_ticker_not_yahoos(self, fake_yf):
        """Every downstream consumer keys off the holding's ticker: 'BRKB'.

        Returning 'BRK-B' would leave the price unreachable and reproduce the
        exact bug through a different door.
        """
        from src.alerts.stop_loss import _fetch_prices

        prices = _fetch_prices(["BRKB"])
        assert prices == {"BRKB": Decimal("517.22")}

    def test_positive_control_ordinary_ticker_still_works(self, fake_yf):
        from src.alerts.stop_loss import _fetch_prices

        prices = _fetch_prices(["MSFT"])
        assert prices == {"MSFT": Decimal("500.0000")}
        assert "MSFT" in fake_yf.asked

    def test_an_empty_frame_is_logged_rather_than_silently_dropped(self, fake_yf, caplog):
        """The branch that had no log line at all.

        `hist.empty` is not an exception, so the `except` never fired and the
        ticker just failed to appear in the dict. Nothing anywhere said so.
        """
        import logging

        from loguru import logger

        from src.alerts.stop_loss import _fetch_prices

        records: list[str] = []
        sink_id = logger.add(lambda m: records.append(m), level="WARNING")
        try:
            prices = _fetch_prices(["ZZZZ"])
        finally:
            logger.remove(sink_id)

        assert prices == {}
        assert any("ZZZZ" in r for r in records), f"no warning mentioned ZZZZ: {records}"

    def test_the_log_control_stays_quiet_on_success(self, fake_yf):
        """Proves the assertion above can distinguish — it is not always-true."""
        from loguru import logger

        from src.alerts.stop_loss import _fetch_prices

        records: list[str] = []
        sink_id = logger.add(lambda m: records.append(m), level="WARNING")
        try:
            _fetch_prices(["MSFT"])
        finally:
            logger.remove(sink_id)

        assert not any("MSFT" in r for r in records), records

    def test_a_mixed_batch_prices_what_it_can_and_reports_what_it_cannot(self, fake_yf):
        from src.alerts.stop_loss import _fetch_prices

        prices = _fetch_prices(["MSFT", "BRKB", "ZZZZ"])
        assert set(prices) == {"MSFT", "BRKB"}


# ── yfinance_client: the path the newsletter actually takes ──────
#
# `_fetch_prices` is NOT what prices the newsletter. The pipeline calls
# `get_ticker_signals` → `get_quote_yfinance`. Fixing only `_fetch_prices`
# would have been a fix that changed nothing Nick receives.


class _FakeFastInfo:
    def __init__(self, last, prev):
        self.last_price = last
        self.previous_close = prev
        self.three_month_average_volume = None
        self.market_cap = None


class _FakeQuoteTicker:
    asked: list[str] = []
    KNOWN = {"BRK-B": (517.22, 513.10), "MSFT": (492.81, 484.0)}

    def __init__(self, symbol):
        _FakeQuoteTicker.asked.append(symbol)
        self._symbol = symbol

    @property
    def fast_info(self):
        last, prev = self.KNOWN.get(self._symbol, (None, None))
        return _FakeFastInfo(last, prev)


@pytest.fixture
def fake_quote_yf(monkeypatch):
    import yfinance as yf

    _FakeQuoteTicker.asked = []
    monkeypatch.setattr(yf, "Ticker", _FakeQuoteTicker)
    return _FakeQuoteTicker


class TestNewsletterQuotePath:
    def test_get_quote_resolves_the_broker_symbol(self, fake_quote_yf):
        from src.api.yfinance_client import get_quote_yfinance

        q = get_quote_yfinance("BRKB")
        assert q is not None, "BRKB returned None — this is the live bug"
        assert "BRK-B" in fake_quote_yf.asked

    def test_the_snapshot_is_keyed_by_the_brokers_ticker(self, fake_quote_yf):
        from src.api.yfinance_client import get_quote_yfinance

        q = get_quote_yfinance("BRKB")
        assert q.ticker == "BRKB"
        assert q.current_price == Decimal("517.22")

    def test_positive_control_an_ordinary_ticker_is_unaffected(self, fake_quote_yf):
        from src.api.yfinance_client import get_quote_yfinance

        q = get_quote_yfinance("MSFT")
        assert q.ticker == "MSFT"
        assert fake_quote_yf.asked == ["MSFT"], "MSFT must not be rewritten"

    def test_a_genuinely_unknown_symbol_still_returns_none(self, fake_quote_yf):
        """The fix must not make failure unrepresentable — that is the other
        half of this bug class."""
        from src.api.yfinance_client import get_quote_yfinance

        assert get_quote_yfinance("ZZZZ") is None


class TestBatchQuoteShape:
    """`get_quotes_batch_yfinance([one])` returned {} for *every* ticker.

    `data["Close"]` is a one-column DataFrame in current yfinance, so the
    unconditional `.to_frame()` raised AttributeError, which the blanket
    `except` converted into an empty dict — MSFT included. Measured against
    the pre-fix source, not inferred.
    """

    def _fake_download(self, columns: list[str], rows: int = 2):
        import pandas as pd

        idx = pd.date_range("2026-08-01", periods=rows)
        close = pd.DataFrame({c: [100.0 + i for i in range(rows)] for c in columns}, index=idx)
        return pd.concat({"Close": close}, axis=1)

    def test_single_ticker_batch_returns_a_quote(self, monkeypatch):
        import yfinance as yf

        from src.api.yfinance_client import get_quotes_batch_yfinance

        monkeypatch.setattr(yf, "download", lambda **kw: self._fake_download(["MSFT"]))
        out = get_quotes_batch_yfinance(["MSFT"])
        assert list(out) == ["MSFT"]

    def test_single_broker_ticker_batch_downloads_the_yahoo_symbol(self, monkeypatch):
        import yfinance as yf

        from src.api.yfinance_client import get_quotes_batch_yfinance

        seen = {}

        def _dl(**kw):
            seen["tickers"] = kw["tickers"]
            return self._fake_download(["BRK-B"])

        monkeypatch.setattr(yf, "download", _dl)
        out = get_quotes_batch_yfinance(["BRKB"])
        assert seen["tickers"] == "BRK-B"
        assert list(out) == ["BRKB"], "result must be keyed by the broker ticker"

    def test_mixed_batch_keys_back_to_broker_tickers(self, monkeypatch):
        import yfinance as yf

        from src.api.yfinance_client import get_quotes_batch_yfinance

        monkeypatch.setattr(yf, "download", lambda **kw: self._fake_download(["MSFT", "BRK-B"]))
        out = get_quotes_batch_yfinance(["MSFT", "BRKB"])
        assert sorted(out) == ["BRKB", "MSFT"]

    def test_a_symbol_absent_from_the_frame_is_skipped_not_invented(self, monkeypatch):
        import yfinance as yf

        from src.api.yfinance_client import get_quotes_batch_yfinance

        monkeypatch.setattr(yf, "download", lambda **kw: self._fake_download(["MSFT"]))
        out = get_quotes_batch_yfinance(["MSFT", "BRKB"])
        assert list(out) == ["MSFT"]
