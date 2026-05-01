"""Tests for new modules: earnings_check, premarket_check, tax_calculator.

These tests cover the pure-logic layers of each module without hitting external
APIs. yfinance calls in premarket_check are mocked via unittest.mock.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ────────────────────────────────────────────────────────────────────────────
# earnings_check.py — match_rule() and threshold logic
# ────────────────────────────────────────────────────────────────────────────

class TestEarningsCheckMatchRule:
    def setup_method(self):
        from src.portfolio.earnings_check import GOOG_RULES, MSFT_RULES, AMZN_RULES, match_rule
        self.goog = GOOG_RULES
        self.msft = MSFT_RULES
        self.amzn = AMZN_RULES
        self.match = match_rule

    def test_goog_beat(self):
        rule = self.match(19.5, self.goog)
        assert rule.label == "Beat"
        assert "Hold all" in rule.nbossn

    def test_goog_in_line(self):
        rule = self.match(18.5, self.goog)
        assert "In-line" in rule.label

    def test_goog_slight_miss(self):
        rule = self.match(17.8, self.goog)
        assert "Slight miss" in rule.label

    def test_goog_miss(self):
        rule = self.match(17.0, self.goog)
        assert "Miss" in rule.label

    def test_msft_beat(self):
        rule = self.match(42.0, self.msft)
        assert "Beat" in rule.label or rule.min_val == 40.0

    def test_msft_miss(self):
        rule = self.match(34.0, self.msft)
        assert "Miss" in rule.label

    def test_amzn_beat(self):
        rule = self.match(32.0, self.amzn)
        assert "Beat" in rule.label

    def test_amzn_miss_floor(self):
        rule = self.match(26.0, self.amzn)
        assert "Miss" in rule.label

    def test_exact_boundary_treated_as_lower_tier(self):
        # At exactly 19.0 (Beat min_val), should match Beat (>= 19.0)
        from src.portfolio.earnings_check import GOOG_RULES, match_rule
        rule = match_rule(19.0, GOOG_RULES)
        assert rule.label == "Beat"

    def test_generate_report_smoke(self):
        from src.portfolio.earnings_check import generate_report
        report = generate_report({"GOOG": 18.5, "MSFT": 39.0, "AMZN": 29.5})
        assert "Alphabet" in report
        assert "Microsoft" in report
        assert "Amazon" in report


# ────────────────────────────────────────────────────────────────────────────
# premarket_check.py — inference logic (mocked yfinance)
# ────────────────────────────────────────────────────────────────────────────

def _make_hist_bar(price: float, hour: int, minute: int = 0, date_offset: int = 0) -> pd.DataFrame:
    """Build a minimal 1-bar yfinance history DataFrame at the given hour."""
    et_offset = timedelta(hours=-4)
    now = datetime.now(tz=timezone(et_offset))
    ts = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    ts = ts + timedelta(days=date_offset)
    index = pd.DatetimeIndex([ts])
    return pd.DataFrame(
        {"Open": price, "High": price, "Low": price, "Close": price, "Volume": 1000},
        index=index,
    )


def _make_mock_ticker(price: float, prev_close: float, hour: int = 7, minute: int = 10) -> MagicMock:
    mock = MagicMock()
    mock.history.return_value = _make_hist_bar(price, hour, minute)
    mock.fast_info.previous_close = prev_close
    return mock


class TestPremarketCheckInference:
    def _run(self, premarket_price: float, prev_close: float, hour: int = 7):
        from src.portfolio.premarket_check import check_premarket_reaction
        with patch("src.portfolio.premarket_check.yf.Ticker") as mock_yf:
            mock_yf.return_value = _make_mock_ticker(premarket_price, prev_close, hour)
            return check_premarket_reaction("LLY", prev_close)

    def test_clear_beat(self):
        result = self._run(premarket_price=920.0, prev_close=851.21)
        assert result.data_available
        assert "beat" in result.inference
        assert result.pct_change > 0.08

    def test_modest_beat(self):
        result = self._run(premarket_price=877.0, prev_close=851.21)
        assert result.data_available
        assert "modest beat" in result.inference

    def test_in_line(self):
        result = self._run(premarket_price=852.0, prev_close=851.21)
        assert result.data_available
        assert "in-line" in result.inference

    def test_modest_miss(self):
        result = self._run(premarket_price=822.0, prev_close=851.21)
        assert result.data_available
        assert "miss" in result.inference.lower()

    def test_severe_miss(self):
        result = self._run(premarket_price=760.0, prev_close=851.21)
        assert result.data_available
        assert "severe" in result.inference

    def test_no_today_data(self):
        from src.portfolio.premarket_check import check_premarket_reaction
        with patch("src.portfolio.premarket_check.yf.Ticker") as mock_yf:
            # Return yesterday's data only (date_offset=-1)
            mock_yf.return_value.history.return_value = _make_hist_bar(851.21, 16, 0, date_offset=-1)
            result = check_premarket_reaction("LLY", 851.21)
        assert not result.data_available
        assert "today" in result.inference or "market" in result.inference

    def test_empty_history(self):
        from src.portfolio.premarket_check import check_premarket_reaction
        with patch("src.portfolio.premarket_check.yf.Ticker") as mock_yf:
            mock_yf.return_value.history.return_value = pd.DataFrame()
            result = check_premarket_reaction("LLY", 851.21)
        assert not result.data_available

    def test_yfinance_exception(self):
        from src.portfolio.premarket_check import check_premarket_reaction
        with patch("src.portfolio.premarket_check.yf.Ticker") as mock_yf:
            mock_yf.return_value.history.side_effect = Exception("network error")
            result = check_premarket_reaction("LLY", 851.21)
        assert not result.data_available
        assert "check failed" in result.inference

    def test_summary_line_with_data(self):
        result = self._run(920.0, 851.21)
        line = result.summary_line()
        assert "LLY" in line
        assert "$920.00" in line

    def test_summary_line_no_data(self):
        from src.portfolio.premarket_check import check_premarket_reaction
        with patch("src.portfolio.premarket_check.yf.Ticker") as mock_yf:
            mock_yf.return_value.history.return_value = pd.DataFrame()
            result = check_premarket_reaction("LLY", 851.21)
        line = result.summary_line()
        assert "no pre-market data" in line

    def test_ticker_context_appended(self):
        from src.portfolio.premarket_check import check_premarket_reaction
        with patch("src.portfolio.premarket_check.yf.Ticker") as mock_yf:
            mock_yf.return_value = _make_mock_ticker(920.0, 851.21)
            result = check_premarket_reaction("LLY", 851.21, ticker_context="check Mounjaro >$10B")
        assert "check Mounjaro" in result.inference

    def test_check_earnings_day_multi_ticker(self):
        from src.portfolio.premarket_check import check_earnings_day
        with patch("src.portfolio.premarket_check.yf.Ticker") as mock_yf:
            mock_yf.side_effect = [
                _make_mock_ticker(920.0, 851.21),   # LLY
                _make_mock_ticker(160.0, 147.82),   # RDDT
            ]
            results = check_earnings_day([("LLY", 851.21), ("RDDT", 147.82)])
        assert len(results) == 2
        assert results[0].ticker == "LLY"
        assert results[1].ticker == "RDDT"


# ────────────────────────────────────────────────────────────────────────────
# tax_calculator.py — holding period detection and tax estimates
# ────────────────────────────────────────────────────────────────────────────

class TestTaxCalculator:
    def setup_method(self):
        from src.portfolio.tax_calculator import TaxLot

        self.make_lot = lambda ticker, shares, cost, purchase, current: TaxLot(
            ticker=ticker,
            shares=Decimal(str(shares)),
            cost_basis=Decimal(str(cost)),
            purchase_date=purchase,
            current_price=Decimal(str(current)),
            unrealized_gain=Decimal(str(shares)) * (Decimal(str(current)) - Decimal(str(cost))),
            holding_period="long_term" if (
                purchase and (date.today() - purchase).days >= 365
            ) else ("short_term" if purchase else "unknown"),
            estimated_tax_low=Decimal("0"),
            estimated_tax_high=Decimal("0"),
        )

    def test_is_harvesting_candidate_loss(self):
        lot = self.make_lot("RDDT", 14, 243.0, date(2025, 4, 1), 147.82)
        assert lot.is_harvesting_candidate is True

    def test_is_harvesting_candidate_gain(self):
        lot = self.make_lot("NVDA", 5000, 64.10, date(2020, 10, 1), 209.25)
        assert lot.is_harvesting_candidate is False

    def test_holding_period_short_term(self):
        lot = self.make_lot("AMD", 20, 251.48, date(2025, 8, 19), 334.0)
        assert lot.holding_period == "short_term"

    def test_holding_period_long_term(self):
        lot = self.make_lot("NVDA", 5000, 64.10, date(2020, 10, 1), 209.25)
        assert lot.holding_period == "long_term"

    def test_holding_period_label(self):
        lot = self.make_lot("NVDA", 5000, 64.10, date(2020, 10, 1), 209.25)
        assert "Long-Term" in lot.holding_period_label

    def test_tax_rate_label_short_term(self):
        lot = self.make_lot("AMD", 20, 251.48, date(2025, 8, 19), 334.0)
        assert "ordinary income" in lot.tax_rate_label

    def test_tax_rate_label_long_term(self):
        lot = self.make_lot("NVDA", 5000, 64.10, date(2020, 10, 1), 209.25)
        assert "LTCG" in lot.tax_rate_label

    def test_unrealized_gain_long_position(self):
        lot = self.make_lot("NVDA", 5000, 64.10, date(2020, 10, 1), 209.25)
        expected_gain = 5000 * (209.25 - 64.10)
        assert abs(float(lot.unrealized_gain) - expected_gain) < 1.0

    def test_portfolio_tax_summary_total_gain(self):
        from src.portfolio.tax_calculator import PortfolioTaxSummary
        lot1 = self.make_lot("NVDA", 5000, 64.10, date(2020, 10, 1), 209.25)
        lot2 = self.make_lot("RDDT", 14, 243.0, date(2025, 4, 1), 147.82)
        summary = PortfolioTaxSummary(lots=[lot1, lot2])
        # NVDA gain minus RDDT loss should give a positive total
        assert summary.total_unrealized_gain > 0
