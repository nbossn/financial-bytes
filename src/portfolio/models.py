from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class Holding:
    ticker: str
    shares: Decimal
    cost_basis: Decimal
    purchase_date: date | None = None

    @property
    def total_cost(self) -> Decimal:
        return self.shares * self.cost_basis

    def current_value(self, current_price: Decimal) -> Decimal:
        return self.shares * current_price

    def unrealized_pnl(self, current_price: Decimal) -> Decimal:
        return self.current_value(current_price) - self.total_cost

    def unrealized_pnl_pct(self, current_price: Decimal) -> Decimal:
        if self.cost_basis == 0:
            return Decimal(0)
        return ((current_price - self.cost_basis) / self.cost_basis) * 100


@dataclass
class PortfolioSnapshot:
    holdings: list[Holding]
    prices: dict[str, Decimal] = field(default_factory=dict)  # ticker -> current price
    as_of: date | None = None
    # Optional per-lot overrides keyed by ticker: list of {"shares", "cost_basis", "purchase_date"} dicts
    # When present, tax_summary uses these instead of the aggregated Holding for that ticker.
    lot_overrides: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def total_cost(self) -> Decimal:
        return sum(h.total_cost for h in self.holdings)

    @property
    def total_value(self) -> Decimal:
        return sum(h.current_value(self.prices.get(h.ticker, h.cost_basis)) for h in self.holdings)

    @property
    def total_pnl(self) -> Decimal:
        return self.total_value - self.total_cost

    @property
    def total_pnl_pct(self) -> Decimal:
        if self.total_cost == 0:
            return Decimal(0)
        return (self.total_pnl / self.total_cost) * 100

    @property
    def tax_summary(self):
        from src.portfolio.tax_calculator import compute_tax_summary
        return compute_tax_summary(self)
