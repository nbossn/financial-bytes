"""Pre-market earnings inference from price movement.

When a company reports earnings pre-market, the stock price reaction in the
pre-market session (before 9:30 AM ET) signals the market's read on the print
before the press release is indexed by search engines.

This module fetches the pre-market price change and maps it to a qualitative
inference: beat / in-line / miss / severe miss.

Inference accuracy: ~70-80% directional. Strong signals (>8% or <-8%) are more
reliable. The 0-5% range is ambiguous — check the actual numbers when they land.

Usage:
    from src.portfolio.premarket_check import check_premarket_reaction

    result = check_premarket_reaction("LLY", prev_close=851.21)
    print(result.inference)         # "likely beat — check Mounjaro+Zepbound >$10B"
    print(result.premarket_price)   # 920.50
    print(result.pct_change)        # 0.0815

CLI:
    financial-bytes premarket-check --ticker LLY --prev-close 851.21
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import yfinance as yf
from loguru import logger


ET_OFFSET = timedelta(hours=-4)  # EDT (UTC-4); update to -5 in winter (EST)


@dataclass
class PremarketResult:
    ticker: str
    prev_close: float
    premarket_price: Optional[float]
    pct_change: Optional[float]
    inference: str
    detail: str
    data_available: bool
    checked_at: str  # ISO timestamp

    def summary_line(self) -> str:
        if not self.data_available:
            return f"{self.ticker}: no pre-market data yet — check again after 7 AM ET"
        pct_str = f"{self.pct_change:+.1%}" if self.pct_change is not None else "N/A"
        return (
            f"{self.ticker}: ${self.premarket_price:.2f} ({pct_str} vs. ${self.prev_close:.2f} close) "
            f"→ {self.inference}"
        )


def check_premarket_reaction(
    ticker: str,
    prev_close: float,
    beats_threshold: float = 0.08,
    modest_beat_threshold: float = 0.03,
    modest_miss_threshold: float = -0.03,
    miss_threshold: float = -0.08,
    ticker_context: Optional[str] = None,
) -> PremarketResult:
    """
    Fetch pre-market price for a ticker and infer earnings quality from the move.

    Args:
        ticker: Stock ticker symbol
        prev_close: Previous regular-session close price
        beats_threshold: % change above which to infer a clear beat (default 8%)
        modest_beat_threshold: % change for modest beat (default 3%)
        modest_miss_threshold: % change for modest miss (default -3%)
        miss_threshold: % change below which to infer a miss (default -8%)
        ticker_context: Optional extra text appended to the inference (e.g., "check Mounjaro >$10B")

    Returns:
        PremarketResult with inference and raw data
    """
    now_et = datetime.now(tz=timezone(ET_OFFSET))
    checked_at = now_et.isoformat()

    t = yf.Ticker(ticker)

    try:
        # Fetch today's data with pre/post market bars
        hist = t.history(period="1d", interval="5m", prepost=True)

        if hist.empty:
            return PremarketResult(
                ticker=ticker,
                prev_close=prev_close,
                premarket_price=None,
                pct_change=None,
                inference="no data — market may not have opened",
                detail="yfinance returned no intraday data",
                data_available=False,
                checked_at=checked_at,
            )

        # Filter to pre-market bars (before 9:30 AM ET = 13:30 UTC)
        # yfinance timestamps are in the local timezone of the exchange (ET for NYSE)
        today_date = now_et.date()
        today_bars = hist[hist.index.date == today_date]

        if today_bars.empty:
            return PremarketResult(
                ticker=ticker,
                prev_close=prev_close,
                premarket_price=None,
                pct_change=None,
                inference="no today data — market session hasn't started",
                detail=f"Last data point: {hist.index[-1]} (yesterday's session)",
                data_available=False,
                checked_at=checked_at,
            )

        # Pre-market bars: before 9:30 AM
        premarket_bars = today_bars[today_bars.index.hour < 9]
        if premarket_bars.empty:
            # Maybe market is open already — use regular-session bars
            regular_bars = today_bars[today_bars.index.hour >= 9]
            if regular_bars.empty:
                return PremarketResult(
                    ticker=ticker,
                    prev_close=prev_close,
                    premarket_price=None,
                    pct_change=None,
                    inference="market not yet open",
                    detail="No pre-market or regular session bars found for today",
                    data_available=False,
                    checked_at=checked_at,
                )
            current_price = float(regular_bars["Close"].iloc[-1])
            price_source = "regular session open"
        else:
            current_price = float(premarket_bars["Close"].iloc[-1])
            price_source = f"pre-market ({premarket_bars.index[-1].strftime('%H:%M ET')})"

        pct_change = (current_price - prev_close) / prev_close

        # Map pct_change to inference
        context_suffix = f" — {ticker_context}" if ticker_context else ""
        if pct_change >= beats_threshold:
            inference = f"likely beat{context_suffix}"
            detail = f"Strong pre-market move (+{pct_change:.1%}) is above the {beats_threshold:.0%} beat threshold."
        elif pct_change >= modest_beat_threshold:
            inference = f"modest beat or in-line — verify numbers{context_suffix}"
            detail = f"Moderate pre-market move (+{pct_change:.1%}). Could be beat or in-line; check actual figures."
        elif pct_change >= modest_miss_threshold:
            inference = f"in-line — results roughly as expected{context_suffix}"
            detail = f"Flat pre-market move ({pct_change:+.1%}). Market doesn't see a major surprise."
        elif pct_change >= miss_threshold:
            inference = f"likely miss — review guidance language{context_suffix}"
            detail = f"Negative pre-market move ({pct_change:+.1%}). Suggests results below expectations or guidance concern."
        else:
            inference = f"severe miss signal — thesis may be impaired{context_suffix}"
            detail = f"Sharp pre-market decline ({pct_change:+.1%}) suggests significant miss or guidance cut."

        logger.info(
            f"{ticker} pre-market check: ${current_price:.2f} ({pct_change:+.1%} vs. ${prev_close:.2f}) "
            f"via {price_source} → {inference}"
        )

        return PremarketResult(
            ticker=ticker,
            prev_close=prev_close,
            premarket_price=current_price,
            pct_change=pct_change,
            inference=inference,
            detail=detail,
            data_available=True,
            checked_at=checked_at,
        )

    except Exception as e:
        logger.warning(f"Pre-market check failed for {ticker}: {e}")
        return PremarketResult(
            ticker=ticker,
            prev_close=prev_close,
            premarket_price=None,
            pct_change=None,
            inference="check failed — run again",
            detail=str(e),
            data_available=False,
            checked_at=checked_at,
        )


# ── Earnings-day configurations ──────────────────────────────────────────────
# Map tickers to their earnings-day context for richer inference output.
# These are thesis-specific strings that appear after the directional call.

EARNINGS_CONTEXT: dict[str, str] = {
    "LLY": "check Mounjaro+Zepbound vs. $9-10B threshold",
    "RDDT": "check ad revenue growth vs. 35% hold threshold",
    "AAPL": "check Services vs. $30.4B and iPhone vs. $56.5B",
    "NVDA": "check Data Center revenue vs. $73-75B guidance",
    "AMD": "check Data Center + AI accelerator mix",
    "GOOG": "check Google Cloud vs. $19B",
    "GOOGL": "check Google Cloud vs. $19B",
    "MSFT": "check Azure growth vs. 39%",
    "AMZN": "check AWS revenue vs. $29B",
    "META": "check ad revenue growth and Reality Labs losses",
}


def check_earnings_day(tickers: list[tuple[str, float]]) -> list[PremarketResult]:
    """
    Run pre-market checks for multiple tickers on an earnings day.

    Args:
        tickers: list of (ticker_symbol, prev_close_price) tuples

    Returns:
        List of PremarketResult, one per ticker
    """
    results = []
    for ticker, prev_close in tickers:
        context = EARNINGS_CONTEXT.get(ticker.upper())
        result = check_premarket_reaction(ticker, prev_close, ticker_context=context)
        results.append(result)
    return results
