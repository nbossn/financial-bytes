"""Tests for the tax-aware stop-loss engine — Phase 1.

Covers:
  - effective_tax_rate() helper (all brackets + NIIT)
  - After-tax breakeven math
  - ST → LT conversion penalty curve
  - HARVEST classification (loss positions)
  - Wash-sale gate
  - Action classification for known positions (FBTC, NKE, CEG)
  - compute_tax_aware_stops() batch sorting
"""
from __future__ import annotations

import json
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.portfolio.models import Holding, PortfolioSnapshot
from src.portfolio.tax_calculator import effective_tax_rate, NIIT_RATE
from src.alerts.tax_aware_stops import (
    TaxAwareStop,
    compute_tax_aware_stop,
    compute_tax_aware_stops,
    _compute_st_penalty,
    _days_held_and_to_ltcg,
    ST_LTCG_BUFFER_DAYS,
)
from src.alerts.wash_sale import (
    is_wash_sale_blocked,
    record_sell,
    WASH_SALE_DAYS,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _holding(ticker: str, shares: float, cost_basis: float, purchase_date: str | None = None) -> Holding:
    pd = date.fromisoformat(purchase_date) if purchase_date else None
    return Holding(
        ticker=ticker,
        shares=Decimal(str(shares)),
        cost_basis=Decimal(str(cost_basis)),
        purchase_date=pd,
    )


TODAY = date(2026, 5, 11)


# ── effective_tax_rate() ──────────────────────────────────────────────────────

class TestEffectiveTaxRate:
    def test_high_bracket_short_term(self):
        low, high = effective_tax_rate("short_term", bracket="high", niit=False)
        assert low  == Decimal("0.32")
        assert high == Decimal("0.37")

    def test_high_bracket_long_term(self):
        low, high = effective_tax_rate("long_term", bracket="high", niit=False)
        assert low  == Decimal("0.20")
        assert high == Decimal("0.20")

    def test_high_bracket_long_term_with_niit(self):
        low, high = effective_tax_rate("long_term", bracket="high", niit=True)
        assert low  == Decimal("0.20") + NIIT_RATE
        assert high == Decimal("0.20") + NIIT_RATE

    def test_mid_bracket_long_term(self):
        low, high = effective_tax_rate("long_term", bracket="mid", niit=False)
        assert low  == Decimal("0.15")
        assert high == Decimal("0.15")

    def test_low_bracket_long_term_zero(self):
        low, high = effective_tax_rate("long_term", bracket="low", niit=False)
        assert low  == Decimal("0.00")
        assert high == Decimal("0.15")

    def test_unknown_period_returns_full_range(self):
        low, high = effective_tax_rate("unknown", bracket="high", niit=False)
        assert low  == Decimal("0.20")
        assert high == Decimal("0.37")

    def test_invalid_bracket_falls_back_to_high(self):
        low, high = effective_tax_rate("long_term", bracket="billionaire", niit=False)
        assert high == Decimal("0.20")  # high bracket long-term

    def test_trust_bracket_always_top_rate(self):
        """Trust short-term rate is always 37% (compressed brackets)."""
        low, high = effective_tax_rate("short_term", bracket="trust", niit=False)
        assert low  == Decimal("0.37")
        assert high == Decimal("0.37")

    def test_trust_bracket_lt_with_niit(self):
        """Trust long-term + NIIT = 20% + 3.8% = 23.8%."""
        low, high = effective_tax_rate("long_term", bracket="trust", niit=True)
        assert low  == Decimal("0.20") + NIIT_RATE
        assert high == Decimal("0.20") + NIIT_RATE

    def test_niit_only_applies_to_lt_not_st(self):
        low_niit, high_niit = effective_tax_rate("short_term", bracket="high", niit=True)
        low_no,   high_no   = effective_tax_rate("short_term", bracket="high", niit=False)
        # NIIT does not apply to short-term ordinary income
        assert low_niit  == low_no
        assert high_niit == high_no


# ── _compute_st_penalty() ─────────────────────────────────────────────────────

class TestStPenalty:
    def test_penalty_at_zero_days_is_max(self):
        penalty = _compute_st_penalty(0)
        assert penalty == Decimal("1.5")

    def test_penalty_at_buffer_days_is_one(self):
        penalty = _compute_st_penalty(ST_LTCG_BUFFER_DAYS)
        assert penalty == Decimal("1")

    def test_penalty_above_buffer_is_one(self):
        penalty = _compute_st_penalty(ST_LTCG_BUFFER_DAYS + 100)
        assert penalty == Decimal("1")

    def test_penalty_at_45_days_is_midpoint(self):
        # 45 days = half of 90 → fraction = 0.5 → penalty = 1 + 0.5 × 0.5 = 1.25
        penalty = _compute_st_penalty(45)
        assert Decimal("1.24") <= penalty <= Decimal("1.26")

    def test_penalty_none_days_returns_one(self):
        assert _compute_st_penalty(None) == Decimal("1")


# ── _days_held_and_to_ltcg() ─────────────────────────────────────────────────

class TestDaysHeldAndToLtcg:
    def test_long_term_position(self):
        purchase = date(2024, 1, 1)   # >365 days before TODAY
        held, to_ltcg = _days_held_and_to_ltcg(purchase, TODAY)
        assert held is not None and held > 365
        assert to_ltcg is None  # already long-term

    def test_short_term_position(self):
        purchase = date(2025, 12, 1)  # ~160 days before TODAY
        held, to_ltcg = _days_held_and_to_ltcg(purchase, TODAY)
        assert held is not None and held < 365
        assert to_ltcg is not None and 0 < to_ltcg < 365

    def test_unknown_purchase_date(self):
        held, to_ltcg = _days_held_and_to_ltcg(None, TODAY)
        assert held is None
        assert to_ltcg is None


# ── Wash-sale ledger ──────────────────────────────────────────────────────────

class TestWashSale:
    def test_no_ledger_returns_not_blocked(self, tmp_path):
        ledger = tmp_path / "ws.json"
        blocked, days = is_wash_sale_blocked("FBTC", check_date=TODAY, ledger_path=ledger)
        assert not blocked
        assert days is None

    def test_recent_sell_blocks_harvest(self, tmp_path):
        ledger = tmp_path / "ws.json"
        sell_date = date(2026, 5, 1)   # 10 days before TODAY
        record_sell("FBTC", sell_date=sell_date, ledger_path=ledger)
        blocked, days = is_wash_sale_blocked("FBTC", check_date=TODAY, ledger_path=ledger)
        assert blocked
        assert days == WASH_SALE_DAYS - 10

    def test_old_sell_does_not_block(self, tmp_path):
        ledger = tmp_path / "ws.json"
        sell_date = date(2026, 4, 1)   # 40 days before TODAY
        record_sell("FBTC", sell_date=sell_date, ledger_path=ledger)
        blocked, days = is_wash_sale_blocked("FBTC", check_date=TODAY, ledger_path=ledger)
        assert not blocked
        assert days is None

    def test_different_ticker_not_blocked(self, tmp_path):
        ledger = tmp_path / "ws.json"
        record_sell("NKE", sell_date=date(2026, 5, 5), ledger_path=ledger)
        blocked, _ = is_wash_sale_blocked("FBTC", check_date=TODAY, ledger_path=ledger)
        assert not blocked

    def test_boundary_exactly_30_days_still_blocked(self, tmp_path):
        ledger = tmp_path / "ws.json"
        sell_date = date(2026, 4, 11)  # exactly 30 days before TODAY (2026-05-11)
        record_sell("NKE", sell_date=sell_date, ledger_path=ledger)
        blocked, days = is_wash_sale_blocked("NKE", check_date=TODAY, ledger_path=ledger)
        assert blocked
        assert days == 0

    def test_boundary_31_days_not_blocked(self, tmp_path):
        ledger = tmp_path / "ws.json"
        sell_date = date(2026, 4, 10)  # 31 days before TODAY
        record_sell("NKE", sell_date=sell_date, ledger_path=ledger)
        blocked, _ = is_wash_sale_blocked("NKE", check_date=TODAY, ledger_path=ledger)
        assert not blocked


# ── compute_tax_aware_stop() — specific known positions ──────────────────────

class TestKnownPositions:
    """Smoke-test the engine against positions from the HANDOFF."""

    def test_fbtc_harvest_no_wash_sale(self, tmp_path):
        """FBTC: cost basis $86.72, current ~$69.72 — clear harvest candidate."""
        ledger = tmp_path / "ws.json"
        h = _holding("FBTC", shares=100, cost_basis=86.72)
        stop = compute_tax_aware_stop(
            holding=h,
            current_price=Decimal("69.72"),
            bracket="high",
            niit=True,
            as_of=TODAY,
            ledger_path=ledger,
        )
        assert stop.action == "HARVEST"
        assert stop.harvest_candidate is True
        assert stop.wash_sale_blocked is False
        assert stop.harvest_opportunity_dollars > 0
        # Tax value ≈ $1700 × 0.238 ≈ $404 at high+NIIT
        assert stop.harvest_opportunity_dollars > Decimal("300")

    def test_fbtc_harvest_blocked_by_wash_sale(self, tmp_path):
        """FBTC with recent sell → WATCH, not HARVEST."""
        ledger = tmp_path / "ws.json"
        record_sell("FBTC", sell_date=date(2026, 5, 5), ledger_path=ledger)
        h = _holding("FBTC", shares=100, cost_basis=86.72)
        stop = compute_tax_aware_stop(
            holding=h,
            current_price=Decimal("69.72"),
            bracket="high",
            niit=True,
            as_of=TODAY,
            ledger_path=ledger,
        )
        assert stop.action == "WATCH"
        assert stop.wash_sale_blocked is True

    def test_nke_harvest(self, tmp_path):
        """NKE: cost basis $59.77, current $44.41 — harvest candidate."""
        ledger = tmp_path / "ws.json"
        h = _holding("NKE", shares=50, cost_basis=59.77)
        stop = compute_tax_aware_stop(
            holding=h,
            current_price=Decimal("44.41"),
            bracket="high",
            niit=True,
            as_of=TODAY,
            ledger_path=ledger,
        )
        assert stop.action == "HARVEST"
        assert stop.harvest_candidate is True
        assert stop.unrealized_gain_dollars < 0

    def test_ceg_hold_with_tax_floor(self, tmp_path):
        """CEG: cost basis $270.10, current ~$300 — profitable HOLD with tax floor."""
        ledger = tmp_path / "ws.json"
        h = _holding("CEG", shares=20, cost_basis=270.10, purchase_date="2025-03-01")
        stop = compute_tax_aware_stop(
            holding=h,
            current_price=Decimal("300.00"),
            bracket="high",
            niit=True,
            as_of=TODAY,
            ledger_path=ledger,
        )
        # CEG is ~11% gain — should be HOLD
        assert stop.action == "HOLD"
        assert stop.harvest_candidate is False
        assert stop.unrealized_gain_pct > 0
        # Tax floor should be below current price
        assert stop.final_threshold_price < stop.current_price
        # After-tax gain per share > 0
        assert stop.after_tax_gain_per_share > 0

    def test_breakeven_math_correctness(self, tmp_path):
        """Verify breakeven formula: -(after_tax_gain / current_price) × 100."""
        ledger = tmp_path / "ws.json"
        # $100 stock, $50 basis, 100% gain, high bracket LT (20% + 3.8% NIIT = 23.8%)
        h = _holding("TEST", shares=1, cost_basis=50.0, purchase_date="2024-01-01")
        stop = compute_tax_aware_stop(
            holding=h,
            current_price=Decimal("100"),
            bracket="high",
            niit=True,
            as_of=TODAY,
            ledger_path=ledger,
        )
        # gain_per_share = 50, rate = 0.238, after_tax_gain = 50 × (1 - 0.238) = 38.1
        # breakeven_decline_pct = -(38.1 / 100) × 100 = -38.1%
        assert Decimal("-39") <= stop.breakeven_decline_pct <= Decimal("-37")
        # threshold_price = current_price + current_price × (final_threshold_pct / 100)
        expected_price = float(stop.current_price) * (1 + float(stop.final_threshold_pct) / 100)
        assert abs(float(stop.final_threshold_price) - expected_price) < 0.10

    def test_st_near_ltcg_gets_hold_not_exit(self, tmp_path):
        """Position with 45 days to LTCG conversion should be HOLD with widened threshold."""
        ledger = tmp_path / "ws.json"
        # Purchase 320 days ago → 45 days remaining to 365-day LTCG conversion
        from datetime import timedelta
        purchase = TODAY - timedelta(days=320)
        h = _holding("HOLD_ME", shares=10, cost_basis=80.0, purchase_date=purchase.isoformat())
        stop = compute_tax_aware_stop(
            holding=h,
            current_price=Decimal("90"),  # +12.5% gain
            bracket="high",
            niit=True,
            as_of=TODAY,
            ledger_path=ledger,
        )
        # 45 days to LTCG → HOLD (don't sell 45 days before tax drops from 37% to 23.8%)
        assert stop.action == "HOLD"
        # ST penalty should be > 1.0 (threshold widened)
        assert stop.st_penalty_factor > Decimal("1")


# ── compute_tax_aware_stops() — batch + sorting ───────────────────────────────

class TestBatchCompute:
    def test_harvest_sorted_first(self, tmp_path):
        """Loss positions should appear before gain positions in output."""
        ledger = tmp_path / "ws.json"
        snapshot = PortfolioSnapshot(
            holdings=[
                _holding("GAIN", 10, 100, "2024-01-01"),  # gain position
                _holding("LOSS", 10, 200),                # loss position (unknown date)
            ],
            prices={"GAIN": Decimal("150"), "LOSS": Decimal("150")},
        )
        stops = compute_tax_aware_stops(snapshot, bracket="high", niit=True, as_of=TODAY, ledger_path=ledger)
        assert len(stops) == 2
        # HARVEST (LOSS) should come before HOLD (GAIN)
        assert stops[0].ticker == "LOSS"
        assert stops[0].action == "HARVEST"
        assert stops[1].ticker == "GAIN"

    def test_money_market_skipped(self, tmp_path):
        """Positions with cost_basis ≤ $1.01 and >1000 shares should be skipped."""
        ledger = tmp_path / "ws.json"
        snapshot = PortfolioSnapshot(
            holdings=[
                _holding("SPAXX", 10000, 1.00),  # money market
                _holding("NVDA", 5, 500, "2024-01-01"),
            ],
            prices={"SPAXX": Decimal("1.00"), "NVDA": Decimal("700")},
        )
        stops = compute_tax_aware_stops(snapshot, bracket="high", niit=True, as_of=TODAY, ledger_path=ledger)
        tickers = [s.ticker for s in stops]
        assert "SPAXX" not in tickers
        assert "NVDA" in tickers

    def test_missing_price_skipped(self, tmp_path):
        """Holdings without a price entry are silently skipped."""
        ledger = tmp_path / "ws.json"
        snapshot = PortfolioSnapshot(
            holdings=[
                _holding("ABC", 10, 100, "2024-01-01"),
                _holding("XYZ", 10, 100, "2024-01-01"),
            ],
            prices={"ABC": Decimal("120")},  # XYZ has no price
        )
        stops = compute_tax_aware_stops(snapshot, bracket="high", niit=True, as_of=TODAY, ledger_path=ledger)
        assert len(stops) == 1
        assert stops[0].ticker == "ABC"

    def test_empty_portfolio(self, tmp_path):
        ledger = tmp_path / "ws.json"
        snapshot = PortfolioSnapshot(holdings=[], prices={})
        stops = compute_tax_aware_stops(snapshot, ledger_path=ledger)
        assert stops == []

    def test_multiple_harvest_sorted_by_opportunity(self, tmp_path):
        """When multiple HARVEST positions exist, larger opportunity should appear first."""
        ledger = tmp_path / "ws.json"
        snapshot = PortfolioSnapshot(
            holdings=[
                _holding("SMALL_LOSS", 10, 120),   # $200 total loss
                _holding("BIG_LOSS",   50, 120),   # $1000 total loss
            ],
            prices={
                "SMALL_LOSS": Decimal("100"),
                "BIG_LOSS":   Decimal("100"),
            },
        )
        stops = compute_tax_aware_stops(snapshot, bracket="high", niit=True, as_of=TODAY, ledger_path=ledger)
        assert stops[0].ticker == "BIG_LOSS"
        assert stops[1].ticker == "SMALL_LOSS"
