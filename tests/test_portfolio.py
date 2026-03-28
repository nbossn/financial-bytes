"""Tests for portfolio reader and models."""
from __future__ import annotations

import csv
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from src.portfolio.models import Holding, PortfolioSnapshot
from src.portfolio.reader import PortfolioReadError, read_portfolio


def _write_csv(rows: list[dict], path: Path) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


class TestHolding:
    def test_total_cost(self):
        h = Holding(ticker="MSFT", shares=Decimal("100"), cost_basis=Decimal("555.23"),
                    purchase_date=None)
        assert h.total_cost == Decimal("55523.00")

    def test_current_value(self):
        h = Holding(ticker="MSFT", shares=Decimal("100"), cost_basis=Decimal("555.23"),
                    purchase_date=None)
        assert h.current_value(Decimal("600.00")) == Decimal("60000.00")

    def test_unrealized_pnl_profit(self):
        h = Holding(ticker="MSFT", shares=Decimal("100"), cost_basis=Decimal("500.00"),
                    purchase_date=None)
        pnl = h.unrealized_pnl(Decimal("600.00"))
        assert pnl == Decimal("10000.00")

    def test_unrealized_pnl_loss(self):
        h = Holding(ticker="MSFT", shares=Decimal("100"), cost_basis=Decimal("600.00"),
                    purchase_date=None)
        pnl = h.unrealized_pnl(Decimal("500.00"))
        assert pnl == Decimal("-10000.00")

    def test_unrealized_pnl_pct(self):
        h = Holding(ticker="MSFT", shares=Decimal("100"), cost_basis=Decimal("500.00"),
                    purchase_date=None)
        pct = h.unrealized_pnl_pct(Decimal("600.00"))
        assert abs(float(pct) - 20.0) < 0.01


class TestPortfolioSnapshot:
    def test_total_cost(self, sample_holdings):
        snap = PortfolioSnapshot(holdings=sample_holdings)
        expected = Decimal("100") * Decimal("555.23") + Decimal("200") * Decimal("206.45")
        assert snap.total_cost == expected

    def test_total_value_no_prices(self, sample_holdings):
        """Without market prices dict, value falls back to cost basis per holding."""
        snap = PortfolioSnapshot(holdings=sample_holdings)
        assert snap.total_value == snap.total_cost

    def test_total_pnl_at_cost(self, sample_holdings):
        snap = PortfolioSnapshot(holdings=sample_holdings)
        assert snap.total_pnl == Decimal("0")

    def test_holdings_count(self, sample_holdings):
        snap = PortfolioSnapshot(holdings=sample_holdings)
        assert len(snap.holdings) == 2


class TestPortfolioReader:
    def test_read_valid_csv(self, tmp_path):
        csv_file = tmp_path / "portfolio.csv"
        _write_csv([
            {"ticker": "MSFT", "shares": "100", "cost_basis": "555.23", "purchase_date": "2025-08-15"},
            {"ticker": "NVDA", "shares": "200", "cost_basis": "206.45", "purchase_date": "2025-11-05"},
        ], csv_file)
        holdings = read_portfolio(str(csv_file))
        assert len(holdings) == 2
        assert holdings[0].ticker == "MSFT"
        assert holdings[1].ticker == "NVDA"
        assert holdings[0].shares == Decimal("100")

    def test_missing_column_raises(self, tmp_path):
        csv_file = tmp_path / "bad.csv"
        _write_csv([{"ticker": "MSFT", "shares": "100"}], csv_file)
        with pytest.raises(PortfolioReadError, match="cost_basis"):
            read_portfolio(str(csv_file))

    def test_file_not_found(self):
        with pytest.raises(PortfolioReadError):
            read_portfolio("/nonexistent/portfolio.csv")

    def test_tickers_uppercased(self, tmp_path):
        csv_file = tmp_path / "portfolio.csv"
        _write_csv([
            {"ticker": "msft", "shares": "100", "cost_basis": "555.23", "purchase_date": "2025-08-15"},
        ], csv_file)
        holdings = read_portfolio(str(csv_file))
        assert holdings[0].ticker == "MSFT"
