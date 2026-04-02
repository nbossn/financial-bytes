"""Yahoo Finance historical price fetcher + quantitative metrics computation.

Uses the public Yahoo Finance v8 chart API (no auth required).
Computes: beta, alpha (Jensen's), Sharpe, Sortino, max drawdown, correlation, R-squared.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import httpx
from loguru import logger

from src.api.models import QuantMetrics

_YF_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}
_RISK_FREE_RATE = 0.045  # approximate current US 10Y yield


def _fetch_prices(ticker: str, period: str = "1y") -> list[float]:
    """Return list of daily adjusted close prices from Yahoo Finance."""
    url = _YF_CHART_URL.format(ticker=ticker)
    params = {"range": period, "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true"}
    try:
        with httpx.Client(timeout=20, headers=_HEADERS) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        result = data["chart"]["result"]
        if not result:
            return []
        indicators = result[0].get("indicators", {})
        adj_close = indicators.get("adjclose", [{}])[0].get("adjclose", [])
        if not adj_close:
            adj_close = indicators.get("quote", [{}])[0].get("close", [])
        return [float(p) for p in adj_close if p is not None]
    except Exception as e:
        logger.warning(f"[yahoo_data] Price fetch failed for {ticker}: {e}")
        return []


def _daily_returns(prices: list[float]) -> list[float]:
    if len(prices) < 2:
        return []
    return [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices))]


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    variance = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(variance)


def _covariance(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    mx, my = _mean(xs[:n]), _mean(ys[:n])
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / (n - 1)


def _max_drawdown(prices: list[float]) -> float:
    if not prices:
        return 0.0
    peak = prices[0]
    max_dd = 0.0
    for p in prices[1:]:
        if p > peak:
            peak = p
        dd = (peak - p) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd * 100  # as percentage


def compute_quant_metrics(ticker: str, benchmark: str = "SPY", period: str = "1y") -> QuantMetrics:
    """Fetch price history and compute quantitative metrics vs benchmark."""
    logger.info(f"[quant] Computing metrics for {ticker} vs {benchmark} ({period})")

    ticker_prices = _fetch_prices(ticker, period)
    bench_prices = _fetch_prices(benchmark, period)

    base = QuantMetrics(ticker=ticker, benchmark=benchmark)

    if len(ticker_prices) < 30:
        base.error = f"Insufficient price data: only {len(ticker_prices)} points"
        logger.warning(f"[quant] {ticker}: {base.error}")
        return base

    # Align lengths
    n = min(len(ticker_prices), len(bench_prices))
    ticker_prices = ticker_prices[-n:]
    bench_prices = bench_prices[-n:]

    t_returns = _daily_returns(ticker_prices)
    b_returns = _daily_returns(bench_prices)
    n_ret = min(len(t_returns), len(b_returns))
    t_returns = t_returns[:n_ret]
    b_returns = b_returns[:n_ret]

    trading_days = 252

    # ── Annualized return & volatility ──────────────────────────
    mean_daily = _mean(t_returns)
    std_daily = _std(t_returns)

    ann_return = ((1 + mean_daily) ** trading_days - 1) * 100
    ann_vol = std_daily * math.sqrt(trading_days) * 100

    # ── Beta & Alpha ─────────────────────────────────────────────
    cov = _covariance(t_returns, b_returns)
    bench_var = _std(b_returns) ** 2
    beta = cov / bench_var if bench_var > 0 else None

    bench_ann_return = (((1 + _mean(b_returns)) ** trading_days) - 1) * 100
    rf_daily = (1 + _risk_free_rate()) ** (1 / trading_days) - 1
    alpha = None
    if beta is not None:
        # Jensen's alpha (annualized)
        alpha = ann_return - (_risk_free_rate() * 100 + beta * (bench_ann_return - _risk_free_rate() * 100))

    # ── R-squared & correlation ───────────────────────────────────
    std_t = _std(t_returns)
    std_b = _std(b_returns)
    correlation = (cov / (std_t * std_b)) if std_t > 0 and std_b > 0 else None
    r_squared = (correlation ** 2) if correlation is not None else None

    # ── Sharpe ───────────────────────────────────────────────────
    rf_ann = _risk_free_rate() * 100
    sharpe = (ann_return - rf_ann) / ann_vol if ann_vol > 0 else None

    # ── Sortino ──────────────────────────────────────────────────
    rf_daily_rate = _risk_free_rate() / trading_days
    downside = [r - rf_daily_rate for r in t_returns if r < rf_daily_rate]
    downside_std = _std(downside) * math.sqrt(trading_days) * 100 if len(downside) > 1 else 0
    sortino = (ann_return - rf_ann) / downside_std if downside_std > 0 else None

    # ── Max drawdown & current drawdown ──────────────────────────
    max_dd = _max_drawdown(ticker_prices)
    peak = max(ticker_prices)
    current_dd = (peak - ticker_prices[-1]) / peak * 100 if peak > 0 else 0.0

    # ── Momentum ──────────────────────────────────────────────────
    def _period_return(days: int) -> float | None:
        if len(ticker_prices) < days + 1:
            return None
        start = ticker_prices[-(days + 1)]
        end = ticker_prices[-1]
        return (end - start) / start * 100 if start > 0 else None

    # ── RSI(14) from price returns ────────────────────────────────
    rsi_val = _compute_rsi(ticker_prices, period=14)

    base.period_days = n
    base.annualized_return = round(ann_return, 2)
    base.annualized_volatility = round(ann_vol, 2)
    base.beta = round(beta, 3) if beta is not None else None
    base.alpha_annualized = round(alpha, 2) if alpha is not None else None
    base.r_squared = round(r_squared, 3) if r_squared is not None else None
    base.correlation = round(correlation, 3) if correlation is not None else None
    base.sharpe_ratio = round(sharpe, 3) if sharpe is not None else None
    base.sortino_ratio = round(sortino, 3) if sortino is not None else None
    base.max_drawdown = round(max_dd, 2)
    base.current_drawdown = round(current_dd, 2)
    base.rsi_14 = round(rsi_val, 2) if rsi_val is not None else None
    base.momentum_1m = _period_return(21)
    base.momentum_3m = _period_return(63)
    base.momentum_6m = _period_return(126)
    base.data_end = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    logger.info(
        f"[quant] {ticker}: beta={base.beta}, alpha={base.alpha_annualized}%, "
        f"sharpe={base.sharpe_ratio}, sortino={base.sortino_ratio}, "
        f"ann_ret={base.annualized_return}%, max_dd={base.max_drawdown}%"
    )
    return base


def _risk_free_rate() -> float:
    return _RISK_FREE_RATE


def _compute_rsi(prices: list[float], period: int = 14) -> float | None:
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = _mean(gains[:period])
    avg_loss = _mean(losses[:period])

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
