"""Tax-aware stop-loss engine — Phases 1–3.

Computes per-position exit thresholds derived from first principles:

  Phase 1 — Tax floor + wash-sale
    - After-tax breakeven floor (when does selling stop making financial sense?)
    - Short-term → long-term conversion penalty (don't fire 30 days before LTCG)
    - Harvest priority short-circuit (loss positions → HARVEST if wash-sale clear)

  Phase 2 — ATR volatility + concentration overlays
    - ATR-based volatility floor (from existing dynamic_stops engine)
    - Concentration factor: tighter stops for outsized positions (>5% / >10% of portfolio)
    - Final threshold = max(tax_floor, atr_floor × concentration_factor)

  Phase 3 — Momentum signal
    - 50-DMA filter: tighten by 15% when price below 50-DMA for 3+ consecutive days
    - Final threshold = max(tax_floor, atr_floor × concentration_factor × momentum_factor)

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
    """Per-position tax-aware stop-loss result (Phases 1–3)."""

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

    # Phase 1: tax floor math
    after_tax_gain_per_share: Decimal  # gain × (1 − high_rate); 0 if loss
    breakeven_decline_pct: Decimal     # -(after_tax_gain / current_price) × 100; 0 if loss
    st_penalty_factor: Decimal         # 1.0 → 1.5 for short-term near LTCG conversion
    tax_floor_pct: Decimal             # breakeven_decline_pct × st_penalty_factor

    # Phase 2: ATR + concentration (None = not computed / unavailable)
    atr_pct: float | None              # ATR14 as fraction of price (e.g. 0.025 = 2.5%)
    atr_threshold_pct: Decimal | None  # raw ATR-based threshold (negative %)
    pct_of_portfolio: Decimal          # this position as % of total portfolio value
    concentration_factor: Decimal      # 0.70 / 0.85 / 1.00 based on pct_of_portfolio

    # Phase 3: momentum
    momentum_flag: bool                # True if below 50-DMA for 3+ consecutive days
    momentum_factor: Decimal           # 0.85 if flag, 1.00 otherwise

    # Final threshold (composite of all active phases)
    final_threshold_pct: Decimal       # negative %, e.g. -38.5 means -38.5% from current
    final_threshold_price: Decimal
    phases_applied: list[int]          # e.g. [1, 2, 3]

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

    @property
    def vol_floor_pct(self) -> Decimal | None:
        """ATR floor after concentration + momentum adjustments. None if ATR unavailable."""
        if self.atr_threshold_pct is None:
            return None
        return self.atr_threshold_pct * self.concentration_factor * self.momentum_factor


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
    # Phase 2 inputs (pass pre-computed to avoid per-position yfinance calls)
    atr_pct: float | None = None,
    atr_threshold_pct: Decimal | None = None,
    pct_of_portfolio: Decimal = Decimal("0"),
    concentration_factor: Decimal = Decimal("1"),
    # Phase 3 inputs
    momentum_flag: bool = False,
    momentum_factor: Decimal = Decimal("1"),
) -> TaxAwareStop:
    """Compute a single TaxAwareStop for one holding.

    Phase 2+3 inputs (atr_threshold_pct, concentration_factor, momentum_factor)
    are pre-computed by the batch runner and passed in here. When not provided,
    the engine falls back to Phase 1 (tax floor only).

    Args:
        holding:              The position (ticker, shares, cost_basis, purchase_date).
        current_price:        Live price for this ticker.
        bracket:              Tax bracket: "high" | "mid" | "low" | "trust"
        niit:                 Apply 3.8% NIIT to long-term gains.
        as_of:                Reference date (defaults to today).
        ledger_path:          Override for wash-sale ledger path.
        atr_pct:              ATR14 as fraction of price (Phase 2).
        atr_threshold_pct:    ATR-based threshold, negative Decimal (Phase 2).
        pct_of_portfolio:     Position weight as % of total portfolio (Phase 2).
        concentration_factor: Stop tightening factor from concentration (Phase 2).
        momentum_flag:        True if below 50-DMA for 3+ consecutive days (Phase 3).
        momentum_factor:      Stop tightening factor from momentum (Phase 3).

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

    # ── Determine which phases are active ───────────────────────────────────
    phases: list[int] = [1]
    if atr_threshold_pct is not None:
        phases.append(2)
    if momentum_flag or momentum_factor != Decimal("1"):
        if 2 not in phases:
            phases.append(2)
        phases.append(3)

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
            tax_floor_pct=Decimal(0),
            atr_pct=atr_pct,
            atr_threshold_pct=atr_threshold_pct,
            pct_of_portfolio=pct_of_portfolio,
            concentration_factor=concentration_factor,
            momentum_flag=momentum_flag,
            momentum_factor=momentum_factor,
            final_threshold_pct=Decimal(0),
            final_threshold_price=holding.cost_basis,
            phases_applied=phases,
            harvest_candidate=True,
            harvest_opportunity_dollars=harvest_opp,
            wash_sale_blocked=wash_blocked,
            wash_sale_days_remaining=wash_days_left,
            action=action,
            rationale=rationale,
        )

    # ── Gain position: Phase 1 tax floor ────────────────────────────────────
    after_tax_gain_ps = gain_per_share * (Decimal("1") - rate_high)
    if current_price > 0:
        breakeven_decline_pct = -(after_tax_gain_ps / current_price) * 100
    else:
        breakeven_decline_pct = Decimal(0)

    st_penalty = _compute_st_penalty(days_to_ltcg)
    tax_floor_pct = breakeven_decline_pct * st_penalty

    # ── Phase 2+3: volatility floor ──────────────────────────────────────────
    vol_floor_pct: Decimal | None = None
    if atr_threshold_pct is not None:
        # Both are negative — max() picks the less-negative (tighter / fires sooner)
        vol_floor_pct = atr_threshold_pct * concentration_factor * momentum_factor

    # ── Compose final threshold ──────────────────────────────────────────────
    if vol_floor_pct is not None:
        # max() of two negatives = less negative = tighter stop
        final_threshold_pct = max(tax_floor_pct, vol_floor_pct)
    else:
        final_threshold_pct = tax_floor_pct

    final_threshold_price = current_price * (1 + final_threshold_pct / 100)

    # ── Action classification ────────────────────────────────────────────────
    if period == "short_term" and days_to_ltcg is not None and days_to_ltcg <= 60:
        action = "HOLD"
        rationale.append(
            f"Short-term position: {days_to_ltcg}d until LTCG conversion. "
            f"Do not sell — waiting saves "
            f"~${float((rate_high - rate_low) * total_gain):,.0f} in tax."
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
            f"Tax floor: {float(tax_floor_pct):.1f}% (${float(current_price * (1 + tax_floor_pct/100)):.2f}). "
            f"Tax rate: {float(rate_high)*100:.1f}%."
        )
    else:
        action = "HOLD"
        period_note = "Unknown holding period" if period == "unknown" else f"{period.replace('_', '-').capitalize()} position"
        rationale.append(
            f"{period_note} (+{float(gain_pct):.1f}%). "
            f"Tax floor: ${float(current_price * (1 + tax_floor_pct/100)):.2f} "
            f"({float(tax_floor_pct):.1f}%, {float(rate_high)*100:.0f}% rate)."
        )

    if st_penalty > 1:
        rationale.append(
            f"ST→LT penalty ({float(st_penalty):.2f}×): threshold widened — "
            f"{days_to_ltcg}d until LTCG conversion."
        )

    if vol_floor_pct is not None and vol_floor_pct > tax_floor_pct:
        rationale.append(
            f"Volatility floor ({float(vol_floor_pct):.1f}%) tighter than tax floor "
            f"({float(tax_floor_pct):.1f}%) — ATR governs."
        )
        if concentration_factor < Decimal("1"):
            rationale.append(
                f"Concentration overlay ({float(pct_of_portfolio):.1f}% of portfolio → "
                f"{float(concentration_factor):.2f}× tighter)."
            )
        if momentum_flag:
            rationale.append("Momentum flag: price below 50-DMA for 3+ consecutive days → 15% tighter.")
    elif vol_floor_pct is not None:
        rationale.append(
            f"Tax floor ({float(tax_floor_pct):.1f}%) governs (wider than ATR floor "
            f"{float(vol_floor_pct):.1f}%)."
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
        tax_floor_pct=tax_floor_pct,
        atr_pct=atr_pct,
        atr_threshold_pct=atr_threshold_pct,
        pct_of_portfolio=pct_of_portfolio,
        concentration_factor=concentration_factor,
        momentum_flag=momentum_flag,
        momentum_factor=momentum_factor,
        final_threshold_pct=final_threshold_pct,
        final_threshold_price=final_threshold_price,
        phases_applied=phases,
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
    run_phase2: bool = True,
    run_phase3: bool = True,
) -> list[TaxAwareStop]:
    """Compute tax-aware stops for all holdings in a snapshot.

    Runs all available phases by default. Phase 2 (ATR + concentration) and
    Phase 3 (momentum) require yfinance network calls — set run_phase2=False
    to skip for speed (falls back to Phase 1 tax floor only).

    Args:
        snapshot:    Portfolio snapshot with holdings and current prices.
        bracket:     Tax bracket: "high" | "mid" | "low" | "trust"
        niit:        Apply 3.8% NIIT to long-term gains.
        as_of:       Reference date for holding period classification.
        ledger_path: Override for wash-sale ledger.
        run_phase2:  Compute ATR + concentration overlays (default True).
        run_phase3:  Compute momentum signals (default True, requires run_phase2).

    Returns:
        List of TaxAwareStop, sorted: HARVEST → EXIT → WATCH → HOLD.
    """
    from src.alerts.concentration import compute_concentration

    stops: list[TaxAwareStop] = []
    skipped = 0

    # Filter to equity holdings only (skip money market / cash equivalents)
    equity_holdings = [
        h for h in snapshot.holdings
        if not (h.cost_basis <= Decimal("1.01") and h.shares > Decimal("1000"))
    ]
    skipped += len(snapshot.holdings) - len(equity_holdings)

    if not equity_holdings:
        logger.info("tax_aware_stops: no equity holdings found")
        return []

    # ── Phase 2 prep: concentration + ATR ───────────────────────────────────
    concentration_map: dict[str, tuple[Decimal, Decimal]] = {}
    atr_map:           dict[str, tuple[float | None, Decimal | None]] = {}
    momentum_map:      dict[str, tuple[bool, Decimal]] = {}

    if run_phase2:
        logger.info("tax_aware_stops: computing concentration weights…")
        concentration_map = compute_concentration(equity_holdings, snapshot.prices)

        tickers_with_price = [
            h.ticker for h in equity_holdings if h.ticker in snapshot.prices
        ]
        logger.info(f"tax_aware_stops: fetching ATR for {len(tickers_with_price)} tickers…")
        from src.alerts.dynamic_stops import compute_dynamic_stop
        for ticker in tickers_with_price:
            try:
                ds = compute_dynamic_stop(ticker)
                # dynamic_pct is a fraction (e.g. -0.115 = -11.5%).
                # Convert to percentage units to match tax_floor_pct convention.
                atr_thresh_pct = (
                    Decimal(str(round(ds.dynamic_pct * 100, 4)))
                    if ds.dynamic_pct else None
                )
                atr_map[ticker] = (ds.atr_pct, atr_thresh_pct)
            except Exception as e:
                logger.debug(f"ATR failed for {ticker}: {e}")
                atr_map[ticker] = (None, None)

    # ── Phase 3 prep: momentum ───────────────────────────────────────────────
    if run_phase2 and run_phase3:
        logger.info("tax_aware_stops: computing momentum flags…")
        from src.alerts.momentum import compute_momentum_flags
        momentum_map = compute_momentum_flags(tickers_with_price)

    # ── Per-position computation ─────────────────────────────────────────────
    for holding in equity_holdings:
        price = snapshot.prices.get(holding.ticker)
        if price is None:
            logger.debug(f"tax_aware_stops: no price for {holding.ticker} — skipping")
            skipped += 1
            continue

        # Unpack Phase 2+3 data (fall back to Phase 1 defaults if not available)
        pct_of_port, conc_factor = concentration_map.get(
            holding.ticker, (Decimal("0"), Decimal("1"))
        )
        atr_pct_val, atr_thresh = atr_map.get(holding.ticker, (None, None))
        mom_flag, mom_factor = momentum_map.get(holding.ticker, (False, Decimal("1")))

        try:
            stop = compute_tax_aware_stop(
                holding=holding,
                current_price=price,
                bracket=bracket,
                niit=niit,
                as_of=as_of,
                ledger_path=ledger_path,
                atr_pct=atr_pct_val,
                atr_threshold_pct=atr_thresh,
                pct_of_portfolio=pct_of_port,
                concentration_factor=conc_factor,
                momentum_flag=mom_flag,
                momentum_factor=mom_factor,
            )
            stops.append(stop)
        except Exception as e:
            logger.warning(f"tax_aware_stops: failed for {holding.ticker}: {e}")
            skipped += 1

    if skipped:
        logger.debug(f"tax_aware_stops: skipped {skipped} positions")

    # Sort: HARVEST → EXIT → WATCH → HOLD; within same action by gain magnitude
    _order = {"HARVEST": 0, "EXIT_TAX_DRAG_LOW": 1, "EXIT_LTCG": 2, "WATCH": 3, "HOLD": 4}
    stops.sort(key=lambda s: (_order.get(s.action, 5), -abs(float(s.unrealized_gain_dollars))))

    # Mark Phase 3 on all positions when it was computed (even if no flags triggered)
    if run_phase2 and run_phase3:
        for s in stops:
            if 2 in s.phases_applied and 3 not in s.phases_applied:
                s.phases_applied.append(3)

    phases_str = ",".join(str(p) for p in sorted({p for s in stops for p in s.phases_applied}))
    logger.info(
        f"tax_aware_stops: {len(stops)} positions (phases {phases_str}) — "
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

        if s.action == "HARVEST":
            thresh = "HARVEST NOW"
        else:
            thresh = f"{float(s.final_threshold_pct):.1f}% → ${float(s.final_threshold_price):.2f}"

        # Show both tax floor and vol floor when Phase 2+ was run
        floor_detail = ""
        if s.atr_threshold_pct is not None:
            vf = s.vol_floor_pct
            floor_detail = (
                f"  [tax:{float(s.tax_floor_pct):.1f}%"
                f" atr:{float(s.atr_threshold_pct):.1f}%"
                + (f" conc:{float(s.concentration_factor):.2f}×" if s.concentration_factor < Decimal("1") else "")
                + (f" 📉mom" if s.momentum_flag else "")
                + f" → final:{float(s.final_threshold_pct):.1f}%]"
            )

        harvest_note = (
            f"  save ~${float(s.harvest_opportunity_dollars):,.0f}"
            if s.action == "HARVEST"
            else ""
        )

        conc_note = f" {float(s.pct_of_portfolio):.1f}%port" if s.pct_of_portfolio > 0 else ""

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
            f"{action_display}{harvest_note}{conc_note}{floor_detail}"
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
    phases_used = sorted({p for s in stops for p in s.phases_applied})
    phase_label = f"Phases applied: {', '.join(str(p) for p in phases_used)}"
    lines.append(f"  {phase_label}")
    if max(phases_used) < 3:
        lines.append("  Note: run with --phase 3 (default) for full ATR + momentum overlays.")
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
