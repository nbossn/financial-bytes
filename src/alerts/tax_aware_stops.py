"""Tax-aware stop-loss engine — Phase 1: tax floor + wash-sale.

Computes per-position exit thresholds derived from first principles:
  - After-tax breakeven floor (when does selling stop making financial sense?)
  - Short-term → long-term conversion penalty (don't fire 30 days before LTCG)
  - Harvest priority short-circuit (loss positions → HARVEST if wash-sale clear)

Phase 2 will layer in ATR volatility, concentration, and momentum overlays.

Usage:
    from src.alerts.tax_aware_stops import compute_tax_aware_stops, format_tax_aware_table
    from src.portfolio.models import PortfolioSnapshot

    stops = compute_tax_aware_stops(snapshot, bracket="high", niit=True)
    print(format_tax_aware_table(stops))
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from loguru import logger

from src.portfolio.models import PortfolioSnapshot, Holding
from src.portfolio.tax_calculator import (
    HoldingPeriod,
    effective_tax_rate,
    _classify_period,
)
from src.alerts.wash_sale import is_wash_sale_blocked, wash_sale_status_note

# ── Constants ────────────────────────────────────────────────────────────────

# Within this many days of LTCG conversion, widen the threshold to avoid
# firing just before the tax rate drops dramatically.
ST_LTCG_BUFFER_DAYS: int = 90

# Maximum ST → LT conversion penalty multiplier (applied linearly from 0 at
# 90 days down to 1.5× at 0 days remaining).
ST_PENALTY_MAX: Decimal = Decimal("1.5")

# Minimum unrealized gain (%) before we compute a meaningful breakeven floor.
# Below this, the position is essentially break-even — action = WATCH.
MIN_GAIN_PCT_FOR_FLOOR: Decimal = Decimal("2")  # 2%

TaxAction = Literal["HOLD", "WATCH", "HARVEST", "EXIT_TAX_DRAG_LOW", "EXIT_LTCG"]


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class TaxAwareStop:
    """Per-position tax-aware stop-loss result (Phase 1: tax floor only)."""

    ticker: str
    shares: Decimal
    cost_basis: Decimal           # per-share
    current_price: Decimal

    # Gain/loss
    unrealized_gain_dollars: Decimal   # total position gain/loss in $
    unrealized_gain_pct: Decimal       # as %

    # Tax classification
    holding_period: HoldingPeriod
    days_held: int | None              # None if purchase_date unknown
    days_to_ltcg: int | None           # None if already long-term or unknown

    # Effective rates for this position
    tax_rate_low: Decimal
    tax_rate_high: Decimal

    # Core math
    after_tax_gain_per_share: Decimal  # gain × (1 − high_rate); 0 if loss
    breakeven_decline_pct: Decimal     # -(after_tax_gain / current_price) × 100; 0 if loss
    st_penalty_factor: Decimal         # 1.0 → 1.5 for short-term near LTCG conversion

    # Final threshold (Phase 1: tax floor only)
    final_threshold_pct: Decimal       # negative %, e.g. -38.5 means -38.5% from current
    final_threshold_price: Decimal

    # Harvest
    harvest_candidate: bool
    harvest_opportunity_dollars: Decimal   # tax value of the harvestable loss
    wash_sale_blocked: bool
    wash_sale_days_remaining: int | None

    # Decision
    action: TaxAction
    rationale: list[str] = field(default_factory=list)

    @property
    def position_value(self) -> Decimal:
        return self.shares * self.current_price

    @property
    def tax_rate_label(self) -> str:
        if self.holding_period == "long_term":
            return f"{float(self.tax_rate_high)*100:.1f}% LTCG"
        if self.holding_period == "short_term":
            return f"{float(self.tax_rate_high)*100:.0f}% ST"
        return f"{float(self.tax_rate_low)*100:.0f}%–{float(self.tax_rate_high)*100:.0f}%"


# ── Core computation ─────────────────────────────────────────────────────────

def _days_held_and_to_ltcg(
    purchase_date: date | None, as_of: date
) -> tuple[int | None, int | None]:
    """Return (days_held, days_to_ltcg). days_to_ltcg is None if already LTCG."""
    if purchase_date is None or purchase_date >= as_of:
        return None, None
    held = (as_of - purchase_date).days
    if held >= 365:
        return held, None  # already long-term
    return held, 365 - held


def _compute_st_penalty(days_to_ltcg: int | None) -> Decimal:
    """Widen threshold linearly from 1.0× (≥90 days) to 1.5× (0 days to LTCG)."""
    if days_to_ltcg is None:
        return Decimal("1")
    if days_to_ltcg >= ST_LTCG_BUFFER_DAYS:
        return Decimal("1")
    # Linear: 1.0 at 90 days, 1.5 at 0 days
    fraction = Decimal(str((ST_LTCG_BUFFER_DAYS - days_to_ltcg) / ST_LTCG_BUFFER_DAYS))
    return Decimal("1") + fraction * (ST_PENALTY_MAX - Decimal("1"))


def compute_tax_aware_stop(
    holding: Holding,
    current_price: Decimal,
    bracket: str = "high",
    niit: bool = True,
    as_of: date | None = None,
    ledger_path=None,
) -> TaxAwareStop:
    """Compute a single TaxAwareStop for one holding.

    Args:
        holding:       The position (ticker, shares, cost_basis, purchase_date).
        current_price: Live price for this ticker.
        bracket:       Tax bracket: "high" | "mid" | "low"
        niit:          Apply 3.8% NIIT to long-term gains.
        as_of:         Reference date (defaults to today).
        ledger_path:   Override for wash-sale ledger path.

    Returns:
        TaxAwareStop with action, threshold, and rationale.
    """
    as_of_date = as_of or date.today()
    ticker = holding.ticker

    # ── Basic P&L ────────────────────────────────────────────────────────────
    gain_per_share = current_price - holding.cost_basis
    total_gain = holding.shares * gain_per_share
    gain_pct = (gain_per_share / holding.cost_basis * 100) if holding.cost_basis else Decimal(0)

    # ── Tax classification ───────────────────────────────────────────────────
    period = _classify_period(holding.purchase_date, as_of_date)
    days_held, days_to_ltcg = _days_held_and_to_ltcg(holding.purchase_date, as_of_date)
    rate_low, rate_high = effective_tax_rate(period, bracket=bracket, niit=niit)

    # ── Wash-sale check ──────────────────────────────────────────────────────
    wash_blocked, wash_days_left = is_wash_sale_blocked(ticker, check_date=as_of_date, ledger_path=ledger_path)

    rationale: list[str] = []

    # ── HARVEST short-circuit ────────────────────────────────────────────────
    if total_gain < 0:
        harvest_opp = abs(total_gain) * rate_high  # tax value of the loss
        if wash_blocked:
            action: TaxAction = "WATCH"
            rationale.append(f"Loss position — wash-sale blocked ({wash_days_left}d remaining).")
            rationale.append(f"Harvestable loss: ${float(abs(total_gain)):,.0f} "
                             f"(~${float(harvest_opp):,.0f} tax value at {float(rate_high)*100:.0f}% rate).")
            rationale.append("Mark calendar: eligible to harvest after wash-sale window clears.")
        else:
            action = "HARVEST"
            rationale.append(f"Loss position: ${float(total_gain):,.0f} unrealized loss.")
            rationale.append(f"Harvest opportunity: ~${float(harvest_opp):,.0f} in tax savings "
                             f"at {float(rate_high)*100:.0f}% rate.")
            rationale.append("Wash-sale clear. Sell to crystallize loss, then wait 31 days before repurchasing.")

        return TaxAwareStop(
            ticker=ticker,
            shares=holding.shares,
            cost_basis=holding.cost_basis,
            current_price=current_price,
            unrealized_gain_dollars=total_gain,
            unrealized_gain_pct=gain_pct,
            holding_period=period,
            days_held=days_held,
            days_to_ltcg=days_to_ltcg,
            tax_rate_low=rate_low,
            tax_rate_high=rate_high,
            after_tax_gain_per_share=Decimal(0),
            breakeven_decline_pct=Decimal(0),
            st_penalty_factor=Decimal("1"),
            final_threshold_pct=Decimal(0),
            final_threshold_price=holding.cost_basis,
            harvest_candidate=True,
            harvest_opportunity_dollars=harvest_opp,
            wash_sale_blocked=wash_blocked,
            wash_sale_days_remaining=wash_days_left,
            action=action,
            rationale=rationale,
        )

    # ── Gain position: compute tax floor ────────────────────────────────────
    after_tax_gain_ps = gain_per_share * (Decimal("1") - rate_high)
    # How far can price fall before after-tax gains = 0?
    # threshold_price = current_price - after_tax_gain_ps
    # breakeven_pct   = -(after_tax_gain_ps / current_price) × 100
    if current_price > 0:
        breakeven_decline_pct = -(after_tax_gain_ps / current_price) * 100
    else:
        breakeven_decline_pct = Decimal(0)

    # ST → LT penalty: widen threshold if close to conversion
    st_penalty = _compute_st_penalty(days_to_ltcg)
    final_threshold_pct = breakeven_decline_pct * st_penalty
    final_threshold_price = current_price * (1 + final_threshold_pct / 100)

    # ── Action classification ────────────────────────────────────────────────
    if period == "short_term" and days_to_ltcg is not None and days_to_ltcg <= 60:
        action = "HOLD"
        rationale.append(
            f"Short-term position: {days_to_ltcg}d until LTCG conversion. "
            f"Do not sell — waiting saves "
            f"~{float((rate_high - rate_low) * total_gain):,.0f} in tax."
        )
    elif gain_pct < MIN_GAIN_PCT_FOR_FLOOR:
        action = "WATCH"
        rationale.append(
            f"Minimal gain ({float(gain_pct):.1f}%). Tax floor is near current price — "
            "monitor for further movement."
        )
    elif period == "long_term":
        action = "HOLD"
        rationale.append(
            f"Long-term position (+{float(gain_pct):.1f}%). "
            f"Tax floor: price can fall {float(breakeven_decline_pct):.1f}% "
            f"(to ${float(final_threshold_price):.2f}) before after-tax gains erode. "
            f"Tax rate: {float(rate_high)*100:.1f}% LTCG."
        )
    else:
        action = "HOLD"
        period_note = "unknown holding period" if period == "unknown" else f"{period.replace('_', '-')} position"
        rationale.append(
            f"{period_note.capitalize()} (+{float(gain_pct):.1f}%). "
            f"Tax floor: ${float(final_threshold_price):.2f} "
            f"({float(final_threshold_pct):.1f}% from current, {float(rate_high)*100:.0f}% rate)."
        )

    if st_penalty > 1:
        rationale.append(
            f"ST→LT penalty ({float(st_penalty):.2f}×): threshold widened — "
            f"{days_to_ltcg}d until LTCG conversion."
        )

    return TaxAwareStop(
        ticker=ticker,
        shares=holding.shares,
        cost_basis=holding.cost_basis,
        current_price=current_price,
        unrealized_gain_dollars=total_gain,
        unrealized_gain_pct=gain_pct,
        holding_period=period,
        days_held=days_held,
        days_to_ltcg=days_to_ltcg,
        tax_rate_low=rate_low,
        tax_rate_high=rate_high,
        after_tax_gain_per_share=after_tax_gain_ps,
        breakeven_decline_pct=breakeven_decline_pct,
        st_penalty_factor=st_penalty,
        final_threshold_pct=final_threshold_pct,
        final_threshold_price=final_threshold_price,
        harvest_candidate=False,
        harvest_opportunity_dollars=Decimal(0),
        wash_sale_blocked=False,
        wash_sale_days_remaining=None,
        action=action,
        rationale=rationale,
    )


def compute_tax_aware_stops(
    snapshot: PortfolioSnapshot,
    bracket: str = "high",
    niit: bool = True,
    as_of: date | None = None,
    ledger_path=None,
) -> list[TaxAwareStop]:
    """Compute tax-aware stops for all holdings in a snapshot.

    Args:
        snapshot:    Portfolio snapshot with holdings and current prices.
        bracket:     Tax bracket: "high" | "mid" | "low"
        niit:        Apply 3.8% NIIT to long-term gains.
        as_of:       Reference date for holding period classification.
        ledger_path: Override for wash-sale ledger.

    Returns:
        List of TaxAwareStop, sorted: HARVEST → WATCH → HOLD.
    """
    stops: list[TaxAwareStop] = []
    skipped = 0

    for holding in snapshot.holdings:
        # Skip money market / cash equivalents
        if holding.cost_basis <= Decimal("1.01") and holding.shares > Decimal("1000"):
            skipped += 1
            continue

        price = snapshot.prices.get(holding.ticker)
        if price is None:
            logger.debug(f"tax_aware_stops: no price for {holding.ticker} — skipping")
            skipped += 1
            continue

        try:
            stop = compute_tax_aware_stop(
                holding=holding,
                current_price=price,
                bracket=bracket,
                niit=niit,
                as_of=as_of,
                ledger_path=ledger_path,
            )
            stops.append(stop)
        except Exception as e:
            logger.warning(f"tax_aware_stops: failed for {holding.ticker}: {e}")
            skipped += 1

    if skipped:
        logger.debug(f"tax_aware_stops: skipped {skipped} positions (no price or cash equivalent)")

    # Sort: HARVEST first, then WATCH, then HOLD
    _order = {"HARVEST": 0, "EXIT_TAX_DRAG_LOW": 1, "EXIT_LTCG": 2, "WATCH": 3, "HOLD": 4}
    stops.sort(key=lambda s: (_order.get(s.action, 5), -abs(float(s.unrealized_gain_dollars))))

    logger.info(
        f"tax_aware_stops: computed {len(stops)} positions — "
        f"{sum(1 for s in stops if s.action == 'HARVEST')} HARVEST, "
        f"{sum(1 for s in stops if s.action == 'WATCH')} WATCH, "
        f"{sum(1 for s in stops if s.action in ('EXIT_TAX_DRAG_LOW', 'EXIT_LTCG'))} EXIT, "
        f"{sum(1 for s in stops if s.action == 'HOLD')} HOLD"
    )
    return stops


# ── Formatting ───────────────────────────────────────────────────────────────

def format_tax_aware_table(stops: list[TaxAwareStop], max_rows: int = 50) -> str:
    """Format a CLI table of tax-aware stop recommendations.

    Shows HARVEST and WATCH/EXIT positions in full; truncates HOLD list to
    max_rows (since HOLD positions need no immediate action).
    """
    if not stops:
        return "No positions to display."

    harvest = [s for s in stops if s.action == "HARVEST"]
    exits   = [s for s in stops if s.action in ("EXIT_TAX_DRAG_LOW", "EXIT_LTCG")]
    watches = [s for s in stops if s.action == "WATCH"]
    holds   = [s for s in stops if s.action == "HOLD"]

    lines = ["", "═" * 105, "  TAX-AWARE STOP RECOMMENDATIONS", "═" * 105]

    def _row(s: TaxAwareStop) -> str:
        gain_sign = "+" if s.unrealized_gain_pct >= 0 else ""
        period_label = {
            "long_term":  "LT",
            "short_term": f"ST/{s.days_to_ltcg}d" if s.days_to_ltcg is not None else "ST",
            "unknown":    "??",
        }.get(s.holding_period, "??")

        thresh = (
            f"{float(s.final_threshold_pct):.1f}% → ${float(s.final_threshold_price):.2f}"
            if s.action not in ("HARVEST",)
            else "HARVEST NOW"
        )

        harvest_note = (
            f"  save ~${float(s.harvest_opportunity_dollars):,.0f}"
            if s.action == "HARVEST"
            else ""
        )

        action_display = {
            "HARVEST": "🟢 HARVEST",
            "EXIT_TAX_DRAG_LOW": "🔴 EXIT",
            "EXIT_LTCG": "🟡 EXIT-LT",
            "WATCH": "👁  WATCH",
            "HOLD": "   HOLD",
        }.get(s.action, s.action)

        return (
            f"  {s.ticker:<8} {period_label:<7} "
            f"{gain_sign}{float(s.unrealized_gain_pct):>6.1f}%  "
            f"{s.tax_rate_label:<14} "
            f"{thresh:<32} "
            f"{action_display}{harvest_note}"
        )

    header = (
        f"  {'TICKER':<8} {'PERIOD':<7} {'GAIN%':>7}  "
        f"{'TAX RATE':<14} {'FLOOR / THRESHOLD':<32} ACTION"
    )

    if harvest:
        lines += ["", f"  HARVEST OPPORTUNITIES ({len(harvest)})", "  " + "─" * 95, header]
        for s in harvest:
            lines.append(_row(s))
            if s.wash_sale_blocked:
                lines.append(f"    ⚠️  {wash_sale_status_note(s.ticker)}")

    if exits:
        lines += ["", f"  EXIT SIGNALS ({len(exits)})", "  " + "─" * 95, header]
        for s in exits:
            lines.append(_row(s))

    if watches:
        lines += ["", f"  WATCH LIST ({len(watches)})", "  " + "─" * 95, header]
        for s in watches:
            lines.append(_row(s))

    if holds:
        shown = holds[:max_rows]
        lines += ["", f"  HOLD ({len(holds)} positions{', showing top ' + str(max_rows) if len(holds) > max_rows else ''})", "  " + "─" * 95, header]
        for s in shown:
            lines.append(_row(s))
        if len(holds) > max_rows:
            lines.append(f"  … {len(holds) - max_rows} more HOLD positions (all within tax floors)")

    # Summary
    total_harvest_value = sum(s.harvest_opportunity_dollars for s in harvest if not s.wash_sale_blocked)
    blocked_harvest = [s for s in harvest if s.wash_sale_blocked]
    lines += [
        "",
        "═" * 105,
        f"  Summary: {len(harvest)} HARVEST  "
        f"| {len(exits)} EXIT  "
        f"| {len(watches)} WATCH  "
        f"| {len(holds)} HOLD",
    ]
    if total_harvest_value > 0:
        lines.append(f"  Immediate harvest opportunity: ~${float(total_harvest_value):,.0f} in tax savings")
    if blocked_harvest:
        lines.append(
            f"  Wash-sale blocked: {', '.join(s.ticker for s in blocked_harvest)} "
            f"(harvest eligible after window clears)"
        )
    lines.append("  Note: thresholds are tax-floor estimates (Phase 1). ATR/momentum overlays in Phase 2.")
    lines.append("═" * 105)

    return "\n".join(lines)


def format_tax_aware_discord(stops: list[TaxAwareStop], portfolio_name: str) -> str:
    """Format a concise Discord message for HARVEST and EXIT positions."""
    harvest = [s for s in stops if s.action == "HARVEST" and not s.wash_sale_blocked]
    blocked = [s for s in stops if s.action == "HARVEST" and s.wash_sale_blocked]
    exits   = [s for s in stops if s.action in ("EXIT_TAX_DRAG_LOW", "EXIT_LTCG")]

    if not harvest and not exits:
        return (
            f"💼 **Tax-Aware Stops — {portfolio_name}**\n"
            f"No immediate actions. "
            f"{sum(1 for s in stops if s.action == 'WATCH')} positions on watch list."
        )

    lines = [f"💼 **Tax-Aware Stops — {portfolio_name}**\n"]

    if harvest:
        total = sum(s.harvest_opportunity_dollars for s in harvest)
        lines.append(f"**🟢 HARVEST ({len(harvest)} positions, ~${float(total):,.0f} tax savings)**")
        for s in harvest:
            lines.append(
                f"  `{s.ticker}`: {float(s.unrealized_gain_pct):.1f}% loss, "
                f"harvest ~${float(s.harvest_opportunity_dollars):,.0f} "
                f"({s.tax_rate_label})"
            )

    if blocked:
        lines.append(f"\n**⚠️ WASH-SALE BLOCKED ({len(blocked)})**")
        for s in blocked:
            lines.append(f"  `{s.ticker}`: {s.wash_sale_days_remaining}d remaining")

    if exits:
        lines.append(f"\n**🔴 EXIT SIGNALS ({len(exits)})**")
        for s in exits:
            lines.append(
                f"  `{s.ticker}`: at ${float(s.current_price):.2f}, "
                f"tax floor ${float(s.final_threshold_price):.2f} "
                f"({float(s.final_threshold_pct):.1f}%)"
            )

    lines.append("\n_No auto-action taken. Review before executing._")
    return "\n".join(lines)
