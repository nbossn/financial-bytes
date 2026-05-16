"""Momentum signals for stop-loss tightening.

Detects price momentum deterioration using a 50-day moving average filter.
When a position is below its 50-DMA for 3+ consecutive trading days, we
tighten the stop by 15% — indicating the position has broken short-term
trend support and the down-move may continue.

Momentum factors:
  Below 50-DMA ≥3 consecutive days  → 0.85  (15% tighter stop)
  Above 50-DMA  or insufficient data → 1.00  (no adjustment)

Usage:
    from src.alerts.momentum import compute_momentum_flags

    flags = compute_momentum_flags(["NVDA", "MSFT", "CEG"])
    flag, factor = flags["NVDA"]   # (False, Decimal("1.0"))
"""
from __future__ import annotations

import warnings
from decimal import Decimal
from typing import Optional

import yfinance as yf
from loguru import logger

warnings.filterwarnings("ignore", category=FutureWarning)

# Number of consecutive days below 50-DMA to trigger the momentum flag
MOMENTUM_DAYS: int = 3
# Lookback for DMA computation (needs ≥50 trading days of history)
MOMENTUM_LOOKBACK: str = "90d"
# Tightening factor applied when momentum flag is active
MOMENTUM_TIGHTEN_FACTOR: Decimal = Decimal("0.85")


def _compute_single_momentum(ticker: str) -> tuple[bool, Decimal]:
    """Return (momentum_flag, factor) for a single ticker.

    momentum_flag is True when the closing price has been below the 50-day
    simple moving average for MOMENTUM_DAYS or more consecutive trading days.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            hist = yf.Ticker(ticker).history(period=MOMENTUM_LOOKBACK, auto_adjust=True)

        if hist is None or len(hist) < 52:
            logger.debug(f"momentum: insufficient history for {ticker} ({len(hist) if hist is not None else 0} rows)")
            return False, Decimal("1")

        closes = hist["Close"]
        dma50 = closes.rolling(50).mean()

        # Check last MOMENTUM_DAYS closes
        recent_closes = closes.iloc[-MOMENTUM_DAYS:]
        recent_dma    = dma50.iloc[-MOMENTUM_DAYS:]

        below_flags = [
            float(c) < float(d)
            for c, d in zip(recent_closes, recent_dma)
            if not (float('nan') == float(d))
        ]

        consecutive_below = len(below_flags) == MOMENTUM_DAYS and all(below_flags)

        if consecutive_below:
            current = float(closes.iloc[-1])
            dma_val = float(dma50.iloc[-1])
            pct_below = (dma_val - current) / dma_val * 100
            logger.info(
                f"momentum: {ticker} below 50-DMA for {MOMENTUM_DAYS}+ days "
                f"(current=${current:.2f}, DMA=${dma_val:.2f}, {pct_below:.1f}% below) "
                "→ tightening stop 15%"
            )
            return True, MOMENTUM_TIGHTEN_FACTOR

        return False, Decimal("1")

    except Exception as e:
        logger.debug(f"momentum: failed for {ticker}: {e}")
        return False, Decimal("1")


def compute_momentum_flags(
    tickers: list[str],
) -> dict[str, tuple[bool, Decimal]]:
    """Compute momentum flags for a list of tickers.

    Args:
        tickers: Stock symbols to evaluate.

    Returns:
        Dict mapping ticker → (momentum_flag, factor).
        momentum_flag: True if price is below 50-DMA for 3+ consecutive days.
        factor: Decimal multiplier — 0.85 if flag active, 1.0 otherwise.
    """
    result: dict[str, tuple[bool, Decimal]] = {}
    for ticker in tickers:
        flag, factor = _compute_single_momentum(ticker)
        result[ticker] = (flag, factor)
    return result


# ── 52-Week High/Low Proximity (George & Hwang 2004) ──────────────────────────
#
# George & Hwang (Journal of Finance, 2004) found that proximity to the 52-week
# high is a stronger predictor of future returns than raw momentum:
#   - Near 52W high: analysts/investors anchor to the high → systematic
#     underreaction to positive news. Price tends to continue up but faces
#     psychological resistance. Tighten stop to protect gains.
#   - Near 52W low: overreaction downward due to anchoring. Upward bias
#     documented over 6-month horizon. Widen stop slightly to allow recovery.

# Within this fraction of the 52W high → tighten stop (resistance zone)
W52_HIGH_THRESHOLD: float = 0.05
# Within this fraction above the 52W low → loosen slightly (underreaction zone)
W52_LOW_THRESHOLD: float = 0.10
# Tightening factor near 52W high
W52_HIGH_FACTOR: Decimal = Decimal("0.85")
# Loosening factor near 52W low (wider stop to avoid shaking out a recovery)
W52_LOW_FACTOR: Decimal = Decimal("1.15")


def _compute_single_52w_proximity(
    ticker: str,
) -> tuple[str, Optional[float], Decimal]:
    """Return (zone, pct_from_high, factor) for 52-week high/low proximity.

    zone: "near_high" | "near_low" | "neutral"
    pct_from_high: distance below 52W high as a fraction (0 = at the high).
    factor: Decimal stop multiplier — 0.85 / 1.15 / 1.00.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            hist = yf.Ticker(ticker).history(period="252d", auto_adjust=True)

        if hist is None or len(hist) < 50:
            logger.debug(
                f"momentum: insufficient 52W history for {ticker} "
                f"({len(hist) if hist is not None else 0} bars)"
            )
            return "neutral", None, Decimal("1")

        closes = hist["Close"]
        current = float(closes.iloc[-1])
        high_52w = float(closes.max())
        low_52w = float(closes.min())

        if current <= 0 or high_52w <= 0:
            return "neutral", None, Decimal("1")

        pct_from_high = (high_52w - current) / high_52w    # 0 = at 52W high
        pct_above_low = (current - low_52w) / low_52w      # 0 = at 52W low

        if pct_from_high <= W52_HIGH_THRESHOLD:
            logger.info(
                f"momentum: {ticker} near 52W high "
                f"(${current:.2f} vs ${high_52w:.2f}, {pct_from_high*100:.1f}% below) "
                "→ tightening stop 15%"
            )
            return "near_high", pct_from_high, W52_HIGH_FACTOR

        if pct_above_low <= W52_LOW_THRESHOLD:
            logger.info(
                f"momentum: {ticker} near 52W low "
                f"(${current:.2f} vs ${low_52w:.2f}, {pct_above_low*100:.1f}% above) "
                "→ loosening stop (George & Hwang underreaction zone)"
            )
            return "near_low", pct_from_high, W52_LOW_FACTOR

        return "neutral", pct_from_high, Decimal("1")

    except Exception as e:
        logger.debug(f"momentum: 52W proximity failed for {ticker}: {e}")
        return "neutral", None, Decimal("1")


def compute_52w_flags(
    tickers: list[str],
) -> dict[str, tuple[str, Optional[float], Decimal]]:
    """Compute 52-week high/low proximity for a list of tickers.

    Args:
        tickers: Stock symbols to evaluate.

    Returns:
        Dict mapping ticker → (zone, pct_from_high, factor).
        zone: "near_high" | "near_low" | "neutral"
        pct_from_high: fraction below 52W high (0 = at the high), or None.
        factor: Decimal stop multiplier — 0.85 near high, 1.15 near low, 1.0 neutral.
    """
    result: dict[str, tuple[str, Optional[float], Decimal]] = {}
    for ticker in tickers:
        zone, prox, factor = _compute_single_52w_proximity(ticker)
        result[ticker] = (zone, prox, factor)
    return result
