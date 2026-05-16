"""ADX Regime Gate — identifies trending vs. choppy market conditions.

Wilder's Average Directional Index (ADX) measures trend *strength*, not direction.
Used here as a pre-filter on the stop-tightening pipeline:

  ADX < 20: sideways/choppy — 50-DMA momentum signals unreliable,
            suppress stop-tightening to prevent false stop exits during dips.
  ADX 20–25: weak trend — cautious; momentum signals used but not amplified.
  ADX > 25: trend confirmed — all signals valid, CRS amplifier enabled.

Reference: J. Welles Wilder, *New Concepts in Technical Trading Systems* (1978).

Usage:
    from src.alerts.trend_regime import compute_regime_gates

    gates = compute_regime_gates(["NVDA", "MSFT", "CEG"])
    gate = gates["NVDA"]
    print(gate.regime)            # "trending" | "weak" | "choppy" | "unknown"
    print(gate.suppress_momentum) # True if choppy → don't tighten stop on 50-DMA signal
    print(gate.adx_display)       # "28.4" or "—"
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import yfinance as yf
from loguru import logger

warnings.filterwarnings("ignore", category=FutureWarning)

# ── ADX thresholds (Wilder's original parameters) ────────────────────────────

ADX_CHOPPY_THRESHOLD: float = 20.0   # below → sideways, suppress momentum signals
ADX_TREND_THRESHOLD: float = 25.0    # above → confirmed trend, all signals valid
ADX_PERIOD: int = 14                  # Wilder smoothing period
ADX_LOOKBACK: str = "90d"            # needs ≥3× ADX_PERIOD days of history


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class RegimeGate:
    """ADX regime assessment for a single ticker."""
    ticker: str
    adx: Optional[float]          # ADX value, None if unavailable
    regime: str                   # "trending" | "weak" | "choppy" | "unknown"
    suppress_momentum: bool       # True → override momentum_factor to 1.0

    @property
    def adx_display(self) -> str:
        return f"{self.adx:.1f}" if self.adx is not None else "—"


# ── ADX computation ───────────────────────────────────────────────────────────

def _compute_adx(ticker: str) -> Optional[float]:
    """Return Wilder's ADX(14) for ticker, or None on failure."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            hist = yf.Ticker(ticker).history(period=ADX_LOOKBACK, auto_adjust=True)

        if hist is None or len(hist) < ADX_PERIOD * 3:
            logger.debug(
                f"trend_regime: insufficient history for {ticker} "
                f"({len(hist) if hist is not None else 0} bars)"
            )
            return None

        high = hist["High"]
        low = hist["Low"]
        close = hist["Close"]
        prev_close = close.shift(1)
        prev_high = high.shift(1)
        prev_low = low.shift(1)

        # True Range
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)

        # Raw directional movement
        up_move = high - prev_high
        down_move = prev_low - low

        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

        # Wilder exponential smoothing (alpha = 1/period)
        alpha = 1.0 / ADX_PERIOD
        atr14 = tr.ewm(alpha=alpha, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr14.replace(0, float("nan"))
        minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr14.replace(0, float("nan"))

        di_sum = (plus_di + minus_di).replace(0, float("nan"))
        dx_series = 100 * (plus_di - minus_di).abs() / di_sum
        adx = float(dx_series.ewm(alpha=alpha, adjust=False).mean().iloc[-1])

        return None if pd.isna(adx) else adx

    except Exception as e:
        logger.debug(f"trend_regime: ADX failed for {ticker}: {e}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def compute_regime_gate(ticker: str) -> RegimeGate:
    """Compute the ADX regime gate for a single ticker."""
    ticker = ticker.upper().strip()
    adx = _compute_adx(ticker)

    if adx is None:
        return RegimeGate(ticker=ticker, adx=None, regime="unknown", suppress_momentum=False)

    if adx < ADX_CHOPPY_THRESHOLD:
        logger.info(
            f"trend_regime: {ticker} ADX={adx:.1f} < {ADX_CHOPPY_THRESHOLD:.0f} — "
            "choppy market, momentum signals suppressed"
        )
        return RegimeGate(ticker=ticker, adx=adx, regime="choppy", suppress_momentum=True)

    if adx < ADX_TREND_THRESHOLD:
        logger.debug(f"trend_regime: {ticker} ADX={adx:.1f} — weak trend (20–25 range)")
        return RegimeGate(ticker=ticker, adx=adx, regime="weak", suppress_momentum=False)

    logger.debug(f"trend_regime: {ticker} ADX={adx:.1f} — confirmed trend")
    return RegimeGate(ticker=ticker, adx=adx, regime="trending", suppress_momentum=False)


def compute_regime_gates(tickers: list[str]) -> dict[str, RegimeGate]:
    """Compute ADX regime gates for a list of tickers.

    Args:
        tickers: Stock symbols to evaluate.

    Returns:
        Dict mapping ticker → RegimeGate.
    """
    return {ticker: compute_regime_gate(ticker) for ticker in tickers}
