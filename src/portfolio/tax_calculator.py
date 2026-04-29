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


def generate_tax_note(ticker: str, lots: list[TaxLot], recommendation: str) -> str | None:
    """Generate a plain-English tax implication note for a ticker.

    Combines lot structure (short/long-term, gains/losses) with the analyst
    recommendation (BUY/HOLD/SELL) to produce actionable 3-5 sentence guidance.
    Returns None when there is nothing meaningful to say (money market, no data).
    """
    if not lots:
        return None

    rec = recommendation.upper()

    lt_gains = [l for l in lots if l.holding_period == "long_term" and l.unrealized_gain > 0]
    st_gains = [l for l in lots if l.holding_period == "short_term" and l.unrealized_gain > 0]
    losses   = [l for l in lots if l.unrealized_gain < 0]

    total_lt_gain  = sum(l.unrealized_gain for l in lt_gains)
    total_st_gain  = sum(l.unrealized_gain for l in st_gains)
    total_loss     = sum(l.unrealized_gain for l in losses)

    lt_tax_low  = sum(l.estimated_tax_low  for l in lt_gains)
    lt_tax_high = sum(l.estimated_tax_high for l in lt_gains)
    st_tax_low  = sum(l.estimated_tax_low  for l in st_gains)
    st_tax_high = sum(l.estimated_tax_high for l in st_gains)

    # Savings from waiting for ST → LTCG conversion
    st_as_ltcg_low  = total_st_gain * LONG_TERM_LOW
    st_as_ltcg_high = total_st_gain * LONG_TERM_HIGH
    st_savings_low  = st_tax_low  - st_as_ltcg_high  # conservative
    st_savings_high = st_tax_high - st_as_ltcg_low   # aggressive

    parts: list[str] = []

    # ── Pure loss position ──────────────────────────────────────────────────
    if losses and not lt_gains and not st_gains:
        harvest_amt = abs(float(total_loss))
        lt_loss = [l for l in losses if l.holding_period == "long_term"]
        st_loss = [l for l in losses if l.holding_period == "short_term"]
        parts.append(
            f"Tax-loss harvesting available — ${harvest_amt:,.0f} in harvestable "
            f"loss{'es' if len(losses) > 1 else ''} across {len(losses)} lot{'s' if len(losses) > 1 else ''}."
        )
        if st_loss and lt_loss:
            parts.append(
                "Short-term losses offset ordinary income first (22%–37%); "
                "long-term losses offset LTCG gains."
            )
        if rec == "SELL":
            parts.append(
                f"Selling crystallizes the loss and offsets gains elsewhere. "
                f"IRS wash-sale rule: wait 31 days before repurchasing {ticker}."
            )
        else:
            parts.append(
                "Holding preserves the loss for a future decision. "
                "Consider selling if the investment thesis has broken."
            )
        return " ".join(parts)

    # ── Position with no actionable data ───────────────────────────────────
    if not lt_gains and not st_gains:
        return None

    # ── Short-term exposure ─────────────────────────────────────────────────
    if st_gains:
        n_st = len(st_gains)
        parts.append(
            f"Short-term exposure: ${float(total_st_gain):,.0f} across {n_st} "
            f"lot{'s' if n_st > 1 else ''} (22%–37% ordinary income). "
            f"Do not sell before LTCG conversion — holding saves "
            f"~${float(st_savings_low):,.0f}–${float(st_savings_high):,.0f}."
        )

    # ── Long-term gains ─────────────────────────────────────────────────────
    if lt_gains:
        n_lt = len(lt_gains)
        lot_label = f"{n_lt} long-term lot{'s' if n_lt > 1 else ''}"
        parts.append(
            f"Long-term gains: ${float(total_lt_gain):,.0f} across {lot_label} "
            f"(15%–20%, ~${float(lt_tax_low):,.0f}–${float(lt_tax_high):,.0f} tax on full exit)."
            if n_lt > 1 or st_gains
            else
            f"Single long-term lot: +${float(total_lt_gain):,.0f} LTCG "
            f"(15%–20%, ~${float(lt_tax_low):,.0f}–${float(lt_tax_high):,.0f} tax on full exit)."
        )

    # ── Harvestable losses alongside gains ──────────────────────────────────
    if losses:
        parts.append(
            f"${abs(float(total_loss)):,.0f} in harvestable losses available — "
            "selling loss positions offsets gains above."
        )

    # ── Recommendation-specific action guidance ─────────────────────────────
    all_tax_low  = lt_tax_low  + st_tax_low
    all_tax_high = lt_tax_high + st_tax_high

    if rec == "HOLD":
        if st_gains:
            parts.append(
                "HOLD requires no tax action — maintain positions until "
                "short-term lots convert to LTCG."
            )
        elif len(lots) > 1:
            parts.append(
                "HOLD requires no tax action. If a partial trim is later needed, "
                "use specific lot identification and sell highest-basis lots first "
                "to minimize gain realized."
            )
        else:
            parts.append("HOLD requires no tax action today.")

    elif rec == "SELL":
        if st_gains and lt_gains:
            parts.append(
                f"If exiting: sell LTCG lot{'s' if len(lt_gains) > 1 else ''} first "
                f"(15%–20% rate); avoid selling short-term lots (22%–37%) unless thesis is broken. "
                f"Full exit estimated tax: ${float(all_tax_low):,.0f}–${float(all_tax_high):,.0f}."
            )
        elif st_gains:
            parts.append(
                f"Selling now crystallizes short-term gain at 22%–37% "
                f"(${float(st_tax_low):,.0f}–${float(st_tax_high):,.0f} tax). "
                f"If possible, delay until LTCG conversion to save "
                f"~${float(st_savings_low):,.0f}–${float(st_savings_high):,.0f}."
            )
        else:
            parts.append(
                f"Exiting at LTCG rates (15%–20%) — estimated "
                f"${float(lt_tax_low):,.0f}–${float(lt_tax_high):,.0f} tax. "
                "Tax-efficient exit."
            )

    elif rec == "BUY":
        if st_gains:
            parts.append(
                "Adding shares starts a new short-term lot alongside existing short-term exposure. "
                "Use specific lot identification when selling — do not commingle new and existing lots."
            )
        else:
            parts.append(
                "Adding shares starts a new short-term lot alongside existing LTCG position. "
                "Track the new purchase date separately — new shares need 12 months before "
                "they become tax-efficient to exit."
            )

    return " ".join(parts) if parts else None
