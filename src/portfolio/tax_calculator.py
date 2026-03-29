"""Capital gains tax estimator for portfolio holdings."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from src.portfolio.models import PortfolioSnapshot

HoldingPeriod = Literal["short_term", "long_term", "unknown"]

# US capital gains tax rate ranges (2025/2026 brackets)
SHORT_TERM_LOW = Decimal("0.22")   # ordinary income — 22% bracket
SHORT_TERM_HIGH = Decimal("0.37")  # ordinary income — top bracket
LONG_TERM_LOW = Decimal("0.15")    # long-term LTCG standard rate
LONG_TERM_HIGH = Decimal("0.20")   # long-term LTCG high-earner rate


@dataclass
class TaxLot:
    ticker: str
    shares: Decimal
    cost_basis: Decimal        # per-share
    purchase_date: date | None
    current_price: Decimal
    unrealized_gain: Decimal   # total dollars (shares × (price − cost_basis))
    holding_period: HoldingPeriod
    estimated_tax_low: Decimal
    estimated_tax_high: Decimal

    @property
    def is_harvesting_candidate(self) -> bool:
        return self.unrealized_gain < 0

    @property
    def holding_period_label(self) -> str:
        labels = {
            "short_term": "Short-Term (<1yr)",
            "long_term": "Long-Term (≥1yr)",
            "unknown": "Unknown",
        }
        return labels[self.holding_period]

    @property
    def tax_rate_label(self) -> str:
        rates = {
            "short_term": "22%–37% (ordinary income)",
            "long_term": "15%–20% (LTCG)",
            "unknown": "15%–37% (varies)",
        }
        return rates[self.holding_period]


@dataclass
class PortfolioTaxSummary:
    lots: list[TaxLot] = field(default_factory=list)

    @property
    def total_unrealized_gain(self) -> Decimal:
        return sum((l.unrealized_gain for l in self.lots if l.unrealized_gain > 0), Decimal(0))

    @property
    def total_unrealized_loss(self) -> Decimal:
        return sum((l.unrealized_gain for l in self.lots if l.unrealized_gain < 0), Decimal(0))

    @property
    def estimated_tax_low(self) -> Decimal:
        return sum((l.estimated_tax_low for l in self.lots), Decimal(0))

    @property
    def estimated_tax_high(self) -> Decimal:
        return sum((l.estimated_tax_high for l in self.lots), Decimal(0))

    @property
    def harvesting_candidates(self) -> list[TaxLot]:
        return [l for l in self.lots if l.is_harvesting_candidate]

    @property
    def total_harvestable_loss(self) -> Decimal:
        return sum((l.unrealized_gain for l in self.harvesting_candidates), Decimal(0))

    @property
    def has_data(self) -> bool:
        return bool(self.lots)


def _classify_period(purchase_date: date | None, as_of: date) -> HoldingPeriod:
    """Classify holding period; Fidelity imports (purchase_date == today) → unknown."""
    if purchase_date is None or purchase_date >= as_of:
        return "unknown"
    return "long_term" if (as_of - purchase_date).days >= 365 else "short_term"


def _compute_tax_for_lot(
    ticker: str,
    shares: Decimal,
    cost_basis: Decimal,
    purchase_date: date | None,
    current_price: Decimal,
    as_of: date,
) -> TaxLot:
    """Compute a single TaxLot entry."""
    gain = shares * (current_price - cost_basis)
    period = _classify_period(purchase_date, as_of)

    if gain > 0:
        if period == "short_term":
            tax_low = gain * SHORT_TERM_LOW
            tax_high = gain * SHORT_TERM_HIGH
        elif period == "long_term":
            tax_low = gain * LONG_TERM_LOW
            tax_high = gain * LONG_TERM_HIGH
        else:  # unknown — show full range
            tax_low = gain * LONG_TERM_LOW
            tax_high = gain * SHORT_TERM_HIGH
    else:
        tax_low = Decimal(0)
        tax_high = Decimal(0)

    return TaxLot(
        ticker=ticker,
        shares=shares,
        cost_basis=cost_basis,
        purchase_date=purchase_date,
        current_price=current_price,
        unrealized_gain=gain,
        holding_period=period,
        estimated_tax_low=tax_low,
        estimated_tax_high=tax_high,
    )


def compute_tax_summary(snapshot: PortfolioSnapshot) -> PortfolioTaxSummary:
    """Compute per-position capital gains tax estimates for a PortfolioSnapshot.

    When snapshot.lot_overrides contains entries for a ticker, those lots are used
    for accurate short/long-term classification. Otherwise falls back to the
    aggregated Holding (which has purchase_date = today from Fidelity imports).
    """
    as_of = snapshot.as_of or date.today()
    lots: list[TaxLot] = []

    for holding in snapshot.holdings:
        # Skip money market / cash equivalents — no capital gains to report
        if holding.cost_basis <= Decimal("1.01") and holding.shares > Decimal("1000"):
            from loguru import logger
            logger.debug(f"Tax: skipping {holding.ticker} (money market / cash equivalent)")
            continue

        price = snapshot.prices.get(holding.ticker, holding.cost_basis)
        override_lots = snapshot.lot_overrides.get(holding.ticker)

        if override_lots:
            # Use per-lot data for accurate tax classification
            allocated_shares = Decimal(0)
            for lot_data in override_lots:
                lot_shares_raw = lot_data.get("shares")
                if lot_shares_raw is None:
                    # Sentinel: allocate all unallocated shares to this lot
                    lot_shares = holding.shares - allocated_shares
                else:
                    lot_shares = Decimal(str(lot_shares_raw))

                if lot_shares <= 0:
                    continue

                lot_cost = Decimal(str(lot_data["cost_basis"]))
                lot_date_str = lot_data.get("purchase_date")
                lot_date = date.fromisoformat(lot_date_str) if lot_date_str else None

                lots.append(_compute_tax_for_lot(
                    ticker=holding.ticker,
                    shares=lot_shares,
                    cost_basis=lot_cost,
                    purchase_date=lot_date,
                    current_price=price,
                    as_of=as_of,
                ))
                allocated_shares += lot_shares
        else:
            # Fallback: single lot from holding
            lots.append(_compute_tax_for_lot(
                ticker=holding.ticker,
                shares=holding.shares,
                cost_basis=holding.cost_basis,
                purchase_date=holding.purchase_date,
                current_price=price,
                as_of=as_of,
            ))

    # Gains first (largest to smallest), then losses
    lots.sort(key=lambda l: l.unrealized_gain, reverse=True)
    return PortfolioTaxSummary(lots=lots)
