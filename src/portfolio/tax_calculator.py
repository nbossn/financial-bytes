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


def compute_tax_summary(snapshot: PortfolioSnapshot) -> PortfolioTaxSummary:
    """Compute per-position capital gains tax estimates for a PortfolioSnapshot."""
    as_of = snapshot.as_of or date.today()
    lots: list[TaxLot] = []

    for holding in snapshot.holdings:
        price = snapshot.prices.get(holding.ticker, holding.cost_basis)
        gain = holding.unrealized_pnl(price)
        period = _classify_period(holding.purchase_date, as_of)

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

        lots.append(TaxLot(
            ticker=holding.ticker,
            shares=holding.shares,
            cost_basis=holding.cost_basis,
            purchase_date=holding.purchase_date,
            current_price=price,
            unrealized_gain=gain,
            holding_period=period,
            estimated_tax_low=tax_low,
            estimated_tax_high=tax_high,
        ))

    # Gains first (largest to smallest), then losses (smallest to largest)
    lots.sort(key=lambda l: l.unrealized_gain, reverse=True)
    return PortfolioTaxSummary(lots=lots)
