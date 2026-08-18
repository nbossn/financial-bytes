"""Every position in the account is valued at market, including the tail.

The 2026-08-18 newsletter reported Current Value $1,964,806 against a real
account of $2,213,389. Two independent causes:

1. `max_positions` bounds how many tickers get an analyst call, and prices were
   only fetched for those. Counting the excluded 191 positions in the totals
   (as they should be) while pricing none of them valued them at cost basis —
   reporting exactly $0.00 unrealized P&L on $267,651 of real holdings, and
   understating the account by $112,509. This is the BRKB failure mode the
   codebase already warns about, at 191x the scale.

   Prices are cheap and analyst calls are not, so the two must be decoupled:
   price everything, analyse the top N.

2. FMPXX (FIMM Money Market, $135,399) was dropped outright. It carries a
   quantity but no cost basis, so it fell through the money-market branch —
   which only triggers when quantity is *missing* — and hit "no cost basis
   data". A $1.00 NAV fund is not an unpriceable asset.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

HEADER = (
    "Account number,Account name,Symbol,Description,Quantity,Last price,"
    "Last price change,Current value,Today's gain/loss dollar,"
    "Today's gain/loss percent,Total gain/loss dollar,Total gain/loss percent,"
    "Percent of account,Cost basis total,Average cost basis,Type"
)

# Quantity present, cost basis blank — exactly how Fidelity exports FMPXX.
FMPXX_ROW = (
    "Z38250874,Trust: Under Agreement,FMPXX,FIMM MONEY MARKET PORTFOLIO: CL I,"
    "135399.34,$1.00,,$135399.34,,,,,6.12%,,,Cash,"
)
# Quantity blank — how SPAXX is exported, already handled.
SPAXX_ROW = (
    "Z38250874,Trust: Under Agreement,SPAXX**,HELD IN MONEY MARKET,,,,$1325.71,"
    ",,,,0.06%,,,Cash,"
)
NVDA_ROW = (
    "Z38250874,Trust: Under Agreement,NVDA,NVIDIA CORPORATION COM,1008.126,$219.99,"
    "-$5.02,$221777.63,-$5060.80,-2.24%,+$71920.62,+47.99%,10.02%,$149857.01,$148.65,Cash,"
)


class TestMoneyMarketCashIsHeld:
    def test_fmpxx_with_quantity_but_no_cost_basis_is_kept(self, tmp_path):
        from src.portfolio.fidelity_reader import read_fidelity_positions

        p = tmp_path / "Portfolio_Positions_Aug-18-2026.csv"
        p.write_text("﻿" + HEADER + "\n" + NVDA_ROW + "\n" + FMPXX_ROW + "\n", encoding="utf-8")

        holdings = {h.ticker: h for h in read_fidelity_positions(p, account_filter="Z38250874")}

        assert "FMPXX" in holdings, "$135k of cash is not an unpriceable asset"
        assert holdings["FMPXX"].shares == Decimal("135399.34")
        assert holdings["FMPXX"].cost_basis == Decimal("1.00")

    def test_spaxx_blank_quantity_still_works(self, tmp_path):
        from src.portfolio.fidelity_reader import read_fidelity_positions

        p = tmp_path / "Portfolio_Positions_Aug-18-2026.csv"
        p.write_text("﻿" + HEADER + "\n" + NVDA_ROW + "\n" + SPAXX_ROW + "\n", encoding="utf-8")

        holdings = {h.ticker: h for h in read_fidelity_positions(p, account_filter="Z38250874")}
        assert holdings["SPAXX"].shares == Decimal("1325.71")

    def test_genuine_equity_without_cost_basis_is_still_skipped(self, tmp_path):
        """The money-market allowance must not become a blanket default."""
        from src.portfolio.fidelity_reader import read_fidelity_positions

        row = (
            "Z38250874,Trust: Under Agreement,WEIRD,SOME EQUITY,10,$50.00,"
            "$0.00,$500.00,,,,,0.01%,,,Cash,"
        )
        p = tmp_path / "Portfolio_Positions_Aug-18-2026.csv"
        p.write_text("﻿" + HEADER + "\n" + NVDA_ROW + "\n" + row + "\n", encoding="utf-8")

        holdings = {h.ticker for h in read_fidelity_positions(p, account_filter="Z38250874")}
        assert "WEIRD" not in holdings


class TestExcludedPositionsArePriced:
    """Coverage bounds analysis, not valuation."""

    def test_pipeline_prices_excluded_tickers_too(self, monkeypatch):
        from src.pipeline import main_pipeline
        from src.portfolio.models import Holding, PortfolioSnapshot

        analyzed = [Holding("NVDA", Decimal("10"), Decimal("100"))]
        excluded = [
            Holding("KOS", Decimal("10"), Decimal("10")),
            Holding("DCH", Decimal("5"), Decimal("20")),
        ]

        asked: list[list[str]] = []

        def fake_batch(tickers):
            asked.append(sorted(tickers))
            return {t: Decimal("30") for t in tickers}

        monkeypatch.setattr(main_pipeline, "_fetch_prices_only", fake_batch)

        prices = {"NVDA": Decimal("150")}
        main_pipeline._price_excluded_holdings(excluded, prices)

        assert asked == [["DCH", "KOS"]], "must request exactly the unpriced tail"
        assert prices["KOS"] == Decimal("30")
        assert prices["DCH"] == Decimal("30")

        snap = PortfolioSnapshot(holdings=analyzed, excluded_holdings=excluded, prices=prices)
        # 10*150 + 10*30 + 5*30 = 1500 + 300 + 150
        assert snap.total_value == Decimal("1950")
        assert snap.missing_prices == []

    def test_already_priced_tickers_are_not_refetched(self, monkeypatch):
        from src.pipeline import main_pipeline
        from src.portfolio.models import Holding

        asked: list[list[str]] = []

        def fake_batch(tickers):
            asked.append(sorted(tickers))
            return {t: Decimal("30") for t in tickers}

        monkeypatch.setattr(main_pipeline, "_fetch_prices_only", fake_batch)

        prices = {"KOS": Decimal("11")}
        main_pipeline._price_excluded_holdings(
            [Holding("KOS", Decimal("1"), Decimal("1")), Holding("DCH", Decimal("1"), Decimal("1"))],
            prices,
        )

        assert asked == [["DCH"]]
        assert prices["KOS"] == Decimal("11"), "existing price must not be overwritten"

    def test_no_excluded_holdings_makes_no_call(self, monkeypatch):
        from src.pipeline import main_pipeline

        called = False

        def fake_batch(tickers):
            nonlocal called
            called = True
            return {}

        monkeypatch.setattr(main_pipeline, "_fetch_prices_only", fake_batch)
        main_pipeline._price_excluded_holdings([], {})
        assert called is False

    def test_fetch_failure_leaves_prices_untouched(self, monkeypatch):
        """A price-source outage must not abort the run."""
        from src.pipeline import main_pipeline
        from src.portfolio.models import Holding

        def boom(tickers):
            raise RuntimeError("yfinance down")

        monkeypatch.setattr(main_pipeline, "_fetch_prices_only", boom)

        prices = {}
        main_pipeline._price_excluded_holdings([Holding("KOS", Decimal("1"), Decimal("1"))], prices)
        assert prices == {}


class TestMissingPriceWarningDenominator:
    def test_denominator_counts_all_holdings_not_just_analyzed(self):
        """The log read '191/150', which cannot be right."""
        from decimal import Decimal

        from src.portfolio.models import Holding, PortfolioSnapshot

        snap = PortfolioSnapshot(
            holdings=[Holding("NVDA", Decimal("1"), Decimal("1"))],
            excluded_holdings=[Holding("KOS", Decimal("1"), Decimal("1"))],
            prices={"NVDA": Decimal("2")},
        )
        assert len(snap.missing_prices) <= snap.position_count
        assert snap.position_count == 2
