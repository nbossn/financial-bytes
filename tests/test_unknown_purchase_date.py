"""An unknown acquisition date must stay unknown, end to end.

Fidelity's positions export carries no lot dates. Two layers invented one anyway:

  fidelity_reader:  purchase_date = date.today()
  reader.py:        purchase_date = date(2000, 1, 1)  # "treats position as LTCG"

Both are fabrications, and each is harmful in its own direction:

* Stamping *today* told the analyst agent every position was bought this morning.
  On 2026-08-18 that produced the claim "MRVL's CEO sold 7,500 shares on your
  day-of-entry" — a coincidence manufactured entirely by the bug.
* Defaulting to *2000-01-01* classifies every unknown position as long-term,
  understating tax on anything actually held under a year.

They also compose: the pipeline round-trips holdings through a CSV, so fixing
only the first would hand the second an empty string and silently reclassify the
whole account as LTCG. All three layers (reader, CSV, DB) have to agree that
None means unknown.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

HEADER = (
    "Account number,Account name,Symbol,Description,Quantity,Last price,"
    "Last price change,Current value,Today's gain/loss dollar,"
    "Today's gain/loss percent,Total gain/loss dollar,Total gain/loss percent,"
    "Percent of account,Cost basis total,Average cost basis,Type"
)
ROW = (
    "Z38250874,Trust: Under Agreement,MRVL,MARVELL TECH,71.85,$222.75,"
    "-$11.58,$16005.00,-$832.03,-4.95%,-$1697.00,-9.59%,0.72%,$17702.00,$246.36,Cash,"
)


class TestFidelityReaderDoesNotInventToday:
    def test_purchase_date_is_none_when_export_has_no_lot_dates(self, tmp_path):
        from src.portfolio.fidelity_reader import read_fidelity_positions

        p = tmp_path / "Portfolio_Positions_Aug-18-2026.csv"
        p.write_text("﻿" + HEADER + "\n" + ROW + "\n", encoding="utf-8")

        holdings = read_fidelity_positions(p, account_filter="Z38250874")

        assert len(holdings) == 1
        assert holdings[0].purchase_date is None, (
            "stamping today makes the analyst think the position was bought today"
        )

    def test_purchase_date_is_never_today(self, tmp_path):
        from src.portfolio.fidelity_reader import read_fidelity_positions

        p = tmp_path / "Portfolio_Positions_Aug-18-2026.csv"
        p.write_text("﻿" + HEADER + "\n" + ROW + "\n", encoding="utf-8")

        holdings = read_fidelity_positions(p, account_filter="Z38250874")

        assert holdings[0].purchase_date != date.today()


class TestPortfolioCsvRoundTrip:
    """The pipeline writes holdings to CSV and reads them back. Unknown must survive."""

    def test_blank_date_reads_back_as_none_not_year_2000(self, tmp_path):
        from src.portfolio.reader import read_portfolio

        csv = tmp_path / "p.csv"
        csv.write_text(
            "ticker,shares,cost_basis,purchase_date\nMRVL,71.85,246.36,\n",
            encoding="utf-8",
        )

        holdings = read_portfolio(csv)

        assert holdings[0].purchase_date is None, (
            "date(2000,1,1) silently classifies unknown positions as long-term"
        )

    def test_present_date_still_parses(self, tmp_path):
        from src.portfolio.reader import read_portfolio

        csv = tmp_path / "p.csv"
        csv.write_text(
            "ticker,shares,cost_basis,purchase_date\nNVDA,10,100,2023-06-15\n",
            encoding="utf-8",
        )

        assert read_portfolio(csv)[0].purchase_date == date(2023, 6, 15)

    def test_none_survives_a_full_write_read_cycle(self, tmp_path):
        from src.portfolio.models import Holding
        from src.portfolio.reader import read_portfolio
        from src.portfolio.transaction_reader import export_holdings_to_csv

        out = tmp_path / "out.csv"
        export_holdings_to_csv(
            [Holding(ticker="MRVL", shares=Decimal("71.85"), cost_basis=Decimal("246.36"))],
            str(out),
        )

        assert read_portfolio(out)[0].purchase_date is None


class TestTaxTreatmentOfUnknown:
    """Unknown must be reported as unknown, never silently favourable."""

    def test_unknown_period_is_reported_as_unknown(self):
        from src.alerts.tax_aware_stops import _classify_period

        assert _classify_period(None, date(2026, 8, 18)) == "unknown"

    def test_year_2000_would_have_been_long_term(self):
        """Pins why the old sentinel was dangerous, not merely inelegant."""
        from src.alerts.tax_aware_stops import _classify_period

        assert _classify_period(date(2000, 1, 1), date(2026, 8, 18)) == "long_term"


class TestDatabaseAcceptsUnknown:
    def test_holding_with_no_date_persists(self, tmp_path, monkeypatch):
        from src.db.models import Portfolio

        col = Portfolio.__table__.c.purchase_date
        assert col.nullable is True, (
            "purchase_date NOT NULL forces the pipeline to invent a date to save at all"
        )


class TestPurchaseHistoryStillWins:
    """Known lot dates from purchase_history must still override unknown."""

    def test_override_applies_over_none(self):
        from src.pipeline.main_pipeline import _apply_purchase_history_to_holdings
        from src.portfolio.models import Holding

        h = Holding(ticker="NVDA", shares=Decimal("1000"), cost_basis=Decimal("143.35"))
        assert h.purchase_date is None

        _apply_purchase_history_to_holdings(
            [h],
            {"NVDA": [
                {"shares": 1000, "cost_basis": 143.35, "purchase_date": "2025-06-16"},
                {"shares": 4000, "cost_basis": 44.50, "purchase_date": "2023-06-15"},
            ]},
        )

        assert h.purchase_date == date(2023, 6, 15), "earliest lot date wins"
