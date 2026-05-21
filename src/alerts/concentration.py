"""Portfolio concentration analysis — per-position weight and stop-loss overlay.

Positions with outsized weight in a portfolio warrant tighter stops regardless
of their tax math, because a large concentrated position amplifies drawdown
risk at the total-portfolio level.

Concentration factors (applied as a multiplier to the ATR threshold):
  >10% of portfolio → 0.70  (30% tighter)
  >5%  of portfolio → 0.85  (15% tighter)
  ≤5%              → 1.00  (no adjustment)
"""
from __future__ import annotations

from decimal import Decimal

from src.portfolio.models import Holding


def compute_concentration(
    holdings: list[Holding],
    prices: dict[str, Decimal],
) -> dict[str, tuple[Decimal, Decimal]]:
    """Compute per-ticker portfolio weight and concentration stop-loss factor.

    Args:
        holdings: List of portfolio holdings.
        prices:   Current prices per ticker.

    Returns:
        Dict mapping ticker → (pct_of_portfolio, concentration_factor).
        pct_of_portfolio is expressed as a percentage (e.g. 12.5 = 12.5%).
        concentration_factor is a Decimal multiplier (0.70, 0.85, or 1.00).
    """
    # Total portfolio value — use cost_basis as fallback when price missing
    total_value = sum(
        h.shares * prices.get(h.ticker, h.cost_basis)
        for h in holdings
    )

    if total_value <= 0:
        return {h.ticker: (Decimal("0"), Decimal("1")) for h in holdings}

    result: dict[str, tuple[Decimal, Decimal]] = {}
    for h in holdings:
        price = prices.get(h.ticker, h.cost_basis)
        position_value = h.shares * price
        pct = (position_value / total_value) * Decimal("100")

        if pct > Decimal("10"):
            factor = Decimal("0.70")
        elif pct > Decimal("5"):
            factor = Decimal("0.85")
        else:
            factor = Decimal("1.00")

        result[h.ticker] = (pct.quantize(Decimal("0.01")), factor)

    return result
