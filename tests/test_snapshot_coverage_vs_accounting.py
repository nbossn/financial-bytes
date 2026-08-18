"""Analyst coverage must not silently redefine what the portfolio is worth.

`max_positions` exists to cap how many tickers get an (expensive) analyst call.
But `_apply_max_positions` *dropped* the excluded holdings, and PortfolioSnapshot
derives every total from `holdings` — so capping coverage silently shrank the
account.

Real numbers, nbossn_fidelity 2026-08-18: 343 positions worth $2,213,389, but the
newsletter reported "Current Value $1,504,910 / 50 positions". $708k of Nick's
money was invisible, and nothing said so.

Coverage and accounting are different questions. `holdings` answers "what do we
analyze"; the totals must answer "what does he own".
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.portfolio.models import Holding, PortfolioSnapshot


def _h(ticker: str, shares: str, cost: str) -> Holding:
    return Holding(
        ticker=ticker,
        shares=Decimal(shares),
        cost_basis=Decimal(cost),
        purchase_date=date(2025, 1, 1),
    )


BIG = _h("NVDA", "100", "100")      # cost 10,000
MID = _h("AAPL", "50", "100")       # cost  5,000
TAIL_A = _h("KOS", "10", "10")      # cost    100
TAIL_B = _h("DCH", "5", "20")       # cost    100

PRICES = {
    "NVDA": Decimal("150"),   # value 15,000
    "AAPL": Decimal("120"),   # value  6,000
    "KOS": Decimal("15"),     # value    150
    "DCH": Decimal("30"),     # value    150
}


class TestTotalsIncludeExcludedTail:
    def test_total_cost_covers_the_whole_account(self):
        snap = PortfolioSnapshot(
            holdings=[BIG, MID],
            excluded_holdings=[TAIL_A, TAIL_B],
            prices=PRICES,
        )
        assert snap.total_cost == Decimal("15200")

    def test_total_value_covers_the_whole_account(self):
        snap = PortfolioSnapshot(
            holdings=[BIG, MID],
            excluded_holdings=[TAIL_A, TAIL_B],
            prices=PRICES,
        )
        assert snap.total_value == Decimal("21300")

    def test_pnl_is_computed_on_full_account(self):
        snap = PortfolioSnapshot(
            holdings=[BIG, MID],
            excluded_holdings=[TAIL_A, TAIL_B],
            prices=PRICES,
        )
        assert snap.total_pnl == Decimal("6100")

    def test_counts_distinguish_analyzed_from_held(self):
        snap = PortfolioSnapshot(
            holdings=[BIG, MID],
            excluded_holdings=[TAIL_A, TAIL_B],
            prices=PRICES,
        )
        assert snap.analyzed_count == 2
        assert snap.position_count == 4
        assert snap.is_truncated is True

    def test_excluded_value_is_reportable(self):
        """The newsletter should be able to say how much sits outside coverage."""
        snap = PortfolioSnapshot(
            holdings=[BIG, MID],
            excluded_holdings=[TAIL_A, TAIL_B],
            prices=PRICES,
        )
        assert snap.excluded_value == Decimal("300")


class TestUntruncatedIsUnchanged:
    """The common case — no cap — must behave exactly as before."""

    def test_totals_match_holdings_when_nothing_excluded(self):
        snap = PortfolioSnapshot(holdings=[BIG, MID], prices=PRICES)
        assert snap.total_cost == Decimal("15000")
        assert snap.total_value == Decimal("21000")
        assert snap.position_count == 2
        assert snap.analyzed_count == 2
        assert snap.is_truncated is False
        assert snap.excluded_value == Decimal("0")

    def test_excluded_defaults_to_empty(self):
        assert PortfolioSnapshot(holdings=[BIG]).excluded_holdings == []


class TestMissingPricesStillCoversTail:
    """An unpriced tail position must not hide behind the cap."""

    def test_missing_prices_includes_excluded(self):
        snap = PortfolioSnapshot(
            holdings=[BIG],
            excluded_holdings=[TAIL_A],
            prices={"NVDA": Decimal("150")},
        )
        assert "KOS" in snap.missing_prices
        assert snap.has_complete_prices is False


class TestPipelineFilterKeepsTail:
    """_apply_max_positions must return the tail, not discard it."""

    def test_returns_kept_and_excluded(self):
        from src.pipeline.main_pipeline import _split_by_max_positions

        class _Def:
            allowed_tickers = None
            max_positions = 2
            name = "test"

        kept, excluded = _split_by_max_positions([BIG, MID, TAIL_A, TAIL_B], _Def())

        assert [h.ticker for h in kept] == ["NVDA", "AAPL"]
        assert {h.ticker for h in excluded} == {"KOS", "DCH"}

    def test_allowlist_excluded_tickers_are_returned_too(self):
        from src.pipeline.main_pipeline import _split_by_max_positions

        class _Def:
            allowed_tickers = ["NVDA"]
            max_positions = None
            name = "test"

        kept, excluded = _split_by_max_positions([BIG, MID, TAIL_A], _Def())

        assert [h.ticker for h in kept] == ["NVDA"]
        assert {h.ticker for h in excluded} == {"AAPL", "KOS"}

    def test_no_cap_excludes_nothing(self):
        from src.pipeline.main_pipeline import _split_by_max_positions

        class _Def:
            allowed_tickers = None
            max_positions = None
            name = "test"

        kept, excluded = _split_by_max_positions([BIG, MID], _Def())

        assert len(kept) == 2
        assert excluded == []
