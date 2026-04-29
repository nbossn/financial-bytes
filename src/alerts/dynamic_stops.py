"""Dynamic volatility-based stop-loss calculation.

Computes per-position stop-loss thresholds based on actual price volatility
rather than flat percentages. Two calculation methods:

  ATR-based (primary):
    threshold = -(atr_multiplier × ATR14 / price)
    ATR14 = 14-day Average True Range, measures natural daily swing range.
    Default multiplier = 5.0: gives ~10% for GOOG (2% ATR), ~30% for COIN (6% ATR).

  Beta-scaled (fallback when ATR unavailable):
    threshold = base_threshold × beta
    Scales a base threshold proportionally to market sensitivity.

Earnings buffer:
    Within N days of earnings, volatility is elevated and pre-earnings positioning
    is expected — widen the threshold by earnings_widen_factor to avoid false alerts
    during a period of legitimate, anticipated price movement.

Three check modes (selected by caller):
  static   — use portfolio.csv stop_loss_pct as-is (existing behavior)
  dynamic  — use computed ATR/beta threshold, ignore static value
  hybrid   — use the tighter (less negative) of static or dynamic;
             never fires later than static, but may fire earlier if volatility
             suggests the move is already meaningful

Usage:
    from src.alerts.dynamic_stops import compute_dynamic_stop, suggest_all_stops

    result = compute_dynamic_stop("GOOG", static_stop=-0.25)
    print(result.recommended_pct)       # e.g. -0.102
    print(result.hybrid_pct)            # e.g. -0.102 (tighter of static/-0.25 and dynamic/-0.102)
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd
import yfinance as yf
from loguru import logger

# Suppress yfinance/pandas FutureWarnings in output
warnings.filterwarnings("ignore", category=FutureWarning)


# ---------------------------------------------------------------------------
# Configuration defaults (all overridable per call)
# ---------------------------------------------------------------------------

DEFAULT_ATR_MULTIPLIER: float = 5.0    # 5× ATR = meaningful move, not daily noise
DEFAULT_BASE_THRESHOLD: float = -0.15  # fallback if ATR unavailable and no beta
EARNINGS_BUFFER_DAYS: int = 7          # widen threshold within N days of earnings
EARNINGS_WIDEN_FACTOR: float = 1.5     # multiply threshold by this during earnings window
MIN_THRESHOLD: float = -0.50           # never suggest a stop wider than -50%
MAX_THRESHOLD: float = -0.04           # never suggest a stop tighter than -4%
ATR_LOOKBACK_DAYS: int = 60            # days of history for ATR calculation
ATR_PERIOD: int = 14                   # ATR smoothing period
BETA_LOOKBACK: str = "252d"            # 1 year for beta calculation


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class DynamicStop:
    ticker: str
    current_price: float

    # Method used for primary calculation
    method: str                      # "atr" | "beta_scaled" | "static_fallback"

    # Raw inputs
    atr_pct: Optional[float]         # ATR14 as % of price (e.g. 0.025 = 2.5%)
    beta: Optional[float]            # computed beta vs SPY
    earnings_days: Optional[int]     # days to next earnings (None = unknown)

    # Thresholds (all negative decimals, e.g. -0.15 = -15%)
    dynamic_pct: float               # ATR/beta-based threshold
    static_pct: Optional[float]      # static value from portfolio.csv (may be None)
    hybrid_pct: float                # tighter of dynamic and static
    earnings_buffered: bool          # True if threshold was widened for earnings window

    @property
    def recommended_pct(self) -> float:
        """The recommended threshold (dynamic). Negative decimal."""
        return self.dynamic_pct

    @property
    def dynamic_pct_display(self) -> str:
        return f"{self.dynamic_pct * 100:.1f}%"

    @property
    def static_pct_display(self) -> str:
        if self.static_pct is None:
            return "—"
        return f"{self.static_pct * 100:.1f}%"

    @property
    def hybrid_pct_display(self) -> str:
        return f"{self.hybrid_pct * 100:.1f}%"

    @property
    def threshold_price_dynamic(self) -> float:
        return self.current_price * (1 + self.dynamic_pct)

    @property
    def threshold_price_hybrid(self) -> float:
        return self.current_price * (1 + self.hybrid_pct)

    def summary_line(self) -> str:
        earnings_note = ""
        if self.earnings_buffered:
            earnings_note = f" [widened for earnings in {self.earnings_days}d]"
        static_note = f"static={self.static_pct_display}" if self.static_pct else "no static"
        return (
            f"{self.ticker:<8} ${self.current_price:>8.2f}  "
            f"dynamic={self.dynamic_pct_display} ({self.method}, "
            f"ATR={self.atr_pct*100:.1f}%" if self.atr_pct else f"{self.ticker:<8} "
            f"dynamic={self.dynamic_pct_display} ({self.method})  "
            f"{static_note}  hybrid={self.hybrid_pct_display}{earnings_note}"
        )


# ---------------------------------------------------------------------------
# ATR computation
# ---------------------------------------------------------------------------

def _compute_atr_pct(ticker: str) -> Optional[float]:
    """Return ATR14 as a fraction of current price, or None on failure."""
    try:
        hist = yf.Ticker(ticker).history(period=f"{ATR_LOOKBACK_DAYS}d", auto_adjust=True)
        if hist is None or len(hist) < ATR_PERIOD + 2:
            return None

        high = hist["High"]
        low = hist["Low"]
        close = hist["Close"]
        prev_close = close.shift(1)

        true_range = pd.concat(
            [
                (high - low),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        atr14 = true_range.rolling(ATR_PERIOD).mean().iloc[-1]
        current_price = float(close.iloc[-1])

        if current_price <= 0 or pd.isna(atr14):
            return None

        return float(atr14 / current_price)

    except Exception as e:
        logger.debug(f"ATR calculation failed for {ticker}: {e}")
        return None


# ---------------------------------------------------------------------------
# Beta computation
# ---------------------------------------------------------------------------

def _compute_beta(ticker: str) -> Optional[float]:
    """Return beta vs SPY over the past year, or None on failure."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            data = yf.download(
                [ticker, "SPY"],
                period=BETA_LOOKBACK,
                auto_adjust=True,
                progress=False,
            )

        closes = data["Close"] if "Close" in data else data
        returns = closes.pct_change().dropna()

        if ticker not in returns.columns or "SPY" not in returns.columns:
            return None
        if len(returns) < 60:
            return None

        aligned = returns[[ticker, "SPY"]].dropna()
        cov_matrix = aligned.cov()
        beta = float(cov_matrix.loc[ticker, "SPY"] / cov_matrix.loc["SPY", "SPY"])

        # Sanity-check: beta outside [-1, 10] is probably a data error
        if not (-1.0 <= beta <= 10.0):
            return None

        return beta

    except Exception as e:
        logger.debug(f"Beta calculation failed for {ticker}: {e}")
        return None


# ---------------------------------------------------------------------------
# Earnings proximity
# ---------------------------------------------------------------------------

def _days_to_earnings(ticker: str) -> Optional[int]:
    """Return days until next earnings, or None if unknown / in the past."""
    try:
        cal = yf.Ticker(ticker).calendar
        if not cal or "Earnings Date" not in cal:
            return None

        earnings_dates = cal["Earnings Date"]
        if not earnings_dates:
            return None

        today = date.today()
        upcoming = []
        for ed in (earnings_dates if isinstance(earnings_dates, list) else [earnings_dates]):
            if hasattr(ed, "date"):
                ed = ed.date()
            if isinstance(ed, date) and ed >= today:
                upcoming.append((ed - today).days)

        return min(upcoming) if upcoming else None

    except Exception as e:
        logger.debug(f"Earnings lookup failed for {ticker}: {e}")
        return None


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_dynamic_stop(
    ticker: str,
    static_stop: Optional[float] = None,
    atr_multiplier: float = DEFAULT_ATR_MULTIPLIER,
    base_threshold: float = DEFAULT_BASE_THRESHOLD,
    earnings_buffer_days: int = EARNINGS_BUFFER_DAYS,
    earnings_widen_factor: float = EARNINGS_WIDEN_FACTOR,
) -> DynamicStop:
    """
    Compute a volatility-based stop-loss threshold for a single ticker.

    Args:
        ticker:               Stock symbol.
        static_stop:          Existing static threshold from portfolio.csv (negative decimal).
                              Used to compute hybrid_pct. May be None.
        atr_multiplier:       Multiplier applied to ATR14/price. Default 5.0.
        base_threshold:       Fallback threshold if ATR unavailable and beta = 1.0.
        earnings_buffer_days: Widen threshold within this many days of earnings.
        earnings_widen_factor:Multiply dynamic threshold by this near earnings.

    Returns:
        DynamicStop with all computed values.
    """
    ticker = ticker.upper().strip()

    # Fetch current price for reference
    try:
        hist = yf.Ticker(ticker).history(period="5d", auto_adjust=True)
        current_price = float(hist["Close"].iloc[-1]) if hist is not None and not hist.empty else 0.0
    except Exception:
        current_price = 0.0

    # ── Method 1: ATR-based ──────────────────────────────────────────────────
    atr_pct = _compute_atr_pct(ticker)
    beta = None
    method = "static_fallback"
    raw_dynamic = base_threshold

    if atr_pct is not None and atr_pct > 0:
        raw_dynamic = -(atr_multiplier * atr_pct)
        method = "atr"
    else:
        # ── Method 2: Beta-scaled ────────────────────────────────────────────
        beta = _compute_beta(ticker)
        if beta is not None:
            raw_dynamic = base_threshold * max(beta, 0.3)
            method = "beta_scaled"
        # else: fallback to base_threshold, method = "static_fallback"

    # ── Earnings buffer ──────────────────────────────────────────────────────
    earnings_days = _days_to_earnings(ticker)
    earnings_buffered = False

    if earnings_days is not None and earnings_days <= earnings_buffer_days:
        raw_dynamic *= earnings_widen_factor
        earnings_buffered = True
        logger.info(
            f"{ticker}: earnings in {earnings_days}d — widening threshold "
            f"by {earnings_widen_factor}× to {raw_dynamic*100:.1f}%"
        )

    # ── Clamp to reasonable bounds ───────────────────────────────────────────
    dynamic_pct = max(MIN_THRESHOLD, min(MAX_THRESHOLD, raw_dynamic))

    # ── Hybrid: tighter of dynamic and static ────────────────────────────────
    # Both are negative. max() of two negatives = less negative = tighter (fires sooner).
    # e.g. max(-0.10, -0.25) = -0.10 → fires at -10%, which is more protective.
    if static_stop is not None:
        hybrid_pct = max(dynamic_pct, static_stop)
    else:
        hybrid_pct = dynamic_pct

    return DynamicStop(
        ticker=ticker,
        current_price=current_price,
        method=method,
        atr_pct=atr_pct,
        beta=beta,
        earnings_days=earnings_days,
        dynamic_pct=dynamic_pct,
        static_pct=static_stop,
        hybrid_pct=hybrid_pct,
        earnings_buffered=earnings_buffered,
    )


# ---------------------------------------------------------------------------
# Batch computation
# ---------------------------------------------------------------------------

def suggest_all_stops(
    positions: list[dict],
    atr_multiplier: float = DEFAULT_ATR_MULTIPLIER,
) -> list[DynamicStop]:
    """
    Compute dynamic stops for a list of portfolio positions.

    Args:
        positions: List of dicts with keys: ticker, stop_loss_pct (optional).
        atr_multiplier: Passed to compute_dynamic_stop.

    Returns:
        List of DynamicStop objects, one per position.
    """
    results = []
    for pos in positions:
        ticker = pos.get("ticker", "").upper()
        if not ticker:
            continue
        static = pos.get("stop_loss_pct")
        if static is not None:
            try:
                static = float(static)
            except (ValueError, TypeError):
                static = None

        logger.info(f"Computing dynamic stop for {ticker}...")
        try:
            result = compute_dynamic_stop(ticker, static_stop=static, atr_multiplier=atr_multiplier)
            results.append(result)
        except Exception as e:
            logger.error(f"Failed to compute stop for {ticker}: {e}")

    return results


# ---------------------------------------------------------------------------
# Formatted output
# ---------------------------------------------------------------------------

def format_suggestions_table(stops: list[DynamicStop]) -> str:
    """Format a markdown table of stop-loss suggestions."""
    lines = [
        "## Dynamic Stop-Loss Suggestions",
        "",
        f"| Ticker | Price | ATR% | Method | Dynamic | Static | Hybrid | Earnings | Notes |",
        f"|--------|-------|------|--------|---------|--------|--------|----------|-------|",
    ]

    for s in sorted(stops, key=lambda x: x.ticker):
        atr_display = f"{s.atr_pct*100:.1f}%" if s.atr_pct else "—"
        earnings_display = f"{s.earnings_days}d" if s.earnings_days is not None else "—"
        notes = "⚠️ near earnings" if s.earnings_buffered else ""
        if s.dynamic_pct != s.hybrid_pct:
            notes += " 📌 hybrid differs"

        lines.append(
            f"| {s.ticker} | ${s.current_price:.2f} | {atr_display} | {s.method} "
            f"| {s.dynamic_pct_display} | {s.static_pct_display} | {s.hybrid_pct_display} "
            f"| {earnings_display} | {notes.strip()} |"
        )

    lines += [
        "",
        f"*ATR multiplier: {DEFAULT_ATR_MULTIPLIER}×. "
        f"Hybrid = tighter of dynamic and static.*",
        f"*Earnings buffer: widen {EARNINGS_WIDEN_FACTOR}× within {EARNINGS_BUFFER_DAYS} days.*",
    ]

    return "\n".join(lines)
