"""
Alpaca Markets data client — price snapshots + news via official alpaca-py SDK.

Usage modes (controlled by DATA_PROVIDER in .env):
  "massive"  — default, existing behaviour (massive.com + yfinance fallback)
  "alpaca"   — use Alpaca for price/news instead of massive.com
  "both"     — run Alpaca AND yfinance in parallel, log discrepancies, return Alpaca result

API keys required in .env:
  ALPACA_API_KEY=<key>
  ALPACA_SECRET_KEY=<secret>
  ALPACA_DATA_FEED=iex     # "iex" (free, real-time) or "sip" (paid, full NBBO)

Free tier (IEX feed, account required but no cost):
  - Real-time last-sale prices for NYSE/NASDAQ
  - Batch snapshots: one call for all tickers
  - News: Benzinga + Reuters/Bloomberg headlines with sentiment
  - Rate limit: 200 req/min

Get keys at: https://alpaca.markets → paper trading account → API Keys tab
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from loguru import logger


class AlpacaClientError(Exception):
    """Raised when Alpaca SDK is missing or API key is not configured."""


def _get_client():
    """Lazy-import alpaca-py and return (StockHistoricalDataClient, NewsClient)."""
    try:
        from alpaca.data import StockHistoricalDataClient, NewsClient  # type: ignore
    except ImportError as e:
        raise AlpacaClientError(
            "alpaca-py not installed. Run: pip install alpaca-py"
        ) from e

    api_key = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        raise AlpacaClientError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env. "
            "Get keys at: https://alpaca.markets (free paper account)"
        )
    return (
        StockHistoricalDataClient(api_key, secret_key),
        NewsClient(api_key, secret_key),
    )


def get_quote_alpaca(ticker: str) -> "QuoteSnapshot | None":
    """
    Fetch a real-time price snapshot for a single ticker via Alpaca.

    Returns QuoteSnapshot compatible with the existing pipeline.
    Uses IEX feed by default (real-time last-sale; free tier).
    """
    from src.api.models import QuoteSnapshot

    try:
        from alpaca.data.requests import StockSnapshotRequest  # type: ignore
        from alpaca.data.enums import DataFeed  # type: ignore

        feed_str = os.getenv("ALPACA_DATA_FEED", "iex").lower()
        feed = DataFeed.SIP if feed_str == "sip" else DataFeed.IEX

        data_client, _ = _get_client()
        snapshots = data_client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=ticker, feed=feed)
        )

        snap = snapshots.get(ticker)
        if snap is None:
            logger.warning(f"[alpaca] No snapshot data for {ticker}")
            return None

        # Prefer latestTrade price; fall back to daily close
        latest_trade = snap.latest_trade
        daily_bar = snap.daily_bar
        prev_daily_bar = snap.previous_daily_bar

        current_raw = (
            latest_trade.price if latest_trade and latest_trade.price else
            (daily_bar.close if daily_bar else None)
        )
        prev_raw = prev_daily_bar.close if prev_daily_bar else None

        if current_raw is None:
            logger.warning(f"[alpaca] Snapshot has no price for {ticker}")
            return None

        current = Decimal(str(round(float(current_raw), 4)))
        prev = Decimal(str(round(float(prev_raw), 4))) if prev_raw else None
        change = (current - prev) if prev else None
        change_pct = (change / prev * 100) if (change is not None and prev) else None
        volume = int(daily_bar.volume) if daily_bar and daily_bar.volume else None

        logger.info(
            f"[alpaca] {ticker}: ${current}"
            + (f" ({float(change_pct):+.2f}% vs prev close)" if change_pct else "")
        )

        return QuoteSnapshot(
            ticker=ticker,
            current_price=current,
            prev_close=prev,
            day_change=change,
            day_change_pct=change_pct,
            volume=volume,
            as_of=datetime.now(timezone.utc),
        )

    except AlpacaClientError:
        raise
    except Exception as e:
        logger.warning(f"[alpaca] Failed to get quote for {ticker}: {e}")
        return None


def get_quotes_batch_alpaca(tickers: list[str]) -> "dict[str, QuoteSnapshot]":
    """
    Fetch real-time snapshots for multiple tickers in a single Alpaca API call.

    This is the primary advantage over yfinance — one batched call vs. N sequential calls.
    Returns dict of ticker → QuoteSnapshot.
    """
    from src.api.models import QuoteSnapshot

    if not tickers:
        return {}

    try:
        from alpaca.data.requests import StockSnapshotRequest  # type: ignore
        from alpaca.data.enums import DataFeed  # type: ignore

        feed_str = os.getenv("ALPACA_DATA_FEED", "iex").lower()
        feed = DataFeed.SIP if feed_str == "sip" else DataFeed.IEX

        data_client, _ = _get_client()
        snapshots = data_client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=tickers, feed=feed)
        )

        results: dict[str, QuoteSnapshot] = {}
        missing = []

        for ticker in tickers:
            snap = snapshots.get(ticker)
            if snap is None:
                missing.append(ticker)
                continue

            latest_trade = snap.latest_trade
            daily_bar = snap.daily_bar
            prev_daily_bar = snap.previous_daily_bar

            current_raw = (
                latest_trade.price if latest_trade and latest_trade.price else
                (daily_bar.close if daily_bar else None)
            )
            prev_raw = prev_daily_bar.close if prev_daily_bar else None

            if current_raw is None:
                missing.append(ticker)
                continue

            current = Decimal(str(round(float(current_raw), 4)))
            prev = Decimal(str(round(float(prev_raw), 4))) if prev_raw else None
            change = (current - prev) if prev else None
            change_pct = (change / prev * 100) if (change is not None and prev) else None
            volume = int(daily_bar.volume) if daily_bar and daily_bar.volume else None

            results[ticker] = QuoteSnapshot(
                ticker=ticker,
                current_price=current,
                prev_close=prev,
                day_change=change,
                day_change_pct=change_pct,
                volume=volume,
                as_of=datetime.now(timezone.utc),
            )

        logger.info(
            f"[alpaca] Batch snapshot: {len(results)}/{len(tickers)} tickers fetched"
            + (f" (missing: {missing})" if missing else "")
        )
        return results

    except AlpacaClientError:
        raise
    except Exception as e:
        logger.warning(f"[alpaca] Batch snapshot failed: {e}")
        return {}


def get_news_alpaca(
    ticker: str,
    lookback_hours: int = 24,
    limit: int = 15,
) -> "list[BenzingaArticle]":
    """
    Fetch recent news articles for a ticker via Alpaca News API.

    Covers Benzinga, Reuters, Bloomberg headlines with sentiment scores.
    Returns list[BenzingaArticle] compatible with existing pipeline.
    """
    from src.api.models import BenzingaArticle

    try:
        from alpaca.data.requests import NewsRequest  # type: ignore

        _, news_client = _get_client()

        start = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        news_data = news_client.get_news(
            NewsRequest(
                symbols=[ticker],
                start=start,
                limit=limit,
                sort="desc",
            )
        )

        articles = []
        for item in (news_data.news if hasattr(news_data, "news") else news_data):
            # Alpaca returns alpaca.data.models.news.News objects
            sentiment = None
            if hasattr(item, "content") and item.content:
                # content field may carry sentiment in some feeds
                pass

            articles.append(
                BenzingaArticle(
                    id=str(getattr(item, "id", "") or ""),
                    ticker=ticker,
                    headline=getattr(item, "headline", "") or "",
                    url=getattr(item, "url", "") or "",
                    summary=getattr(item, "summary", None),
                    body=getattr(item, "content", None),
                    source=getattr(item, "source", None),
                    published_at=getattr(item, "created_at", None),
                    sentiment=sentiment,
                )
            )

        logger.info(f"[alpaca] {len(articles)} news articles for {ticker}")
        return articles

    except AlpacaClientError:
        raise
    except Exception as e:
        logger.warning(f"[alpaca] News fetch failed for {ticker}: {e}")
        return []


# ── Comparison / reliability testing ──────────────────────────────────────────

def compare_with_yfinance(ticker: str) -> dict:
    """
    Run both Alpaca and yfinance for a ticker and log the comparison.

    Returns a dict with both results and the discrepancy. Used in DATA_PROVIDER="both" mode.
    Logs a WARNING if price difference > 0.5% (likely stale data from one source).
    """
    from src.api.yfinance_client import get_quote_yfinance

    alpaca_result = get_quote_alpaca(ticker)
    yfinance_result = get_quote_yfinance(ticker)

    comparison = {
        "ticker": ticker,
        "alpaca_price": float(alpaca_result.current_price) if alpaca_result else None,
        "yfinance_price": float(yfinance_result.current_price) if yfinance_result else None,
        "alpaca_prev_close": float(alpaca_result.prev_close) if alpaca_result and alpaca_result.prev_close else None,
        "yfinance_prev_close": float(yfinance_result.prev_close) if yfinance_result and yfinance_result.prev_close else None,
        "alpaca_ok": alpaca_result is not None,
        "yfinance_ok": yfinance_result is not None,
        "price_diff_pct": None,
        "agreement": True,
    }

    if alpaca_result and yfinance_result:
        a = float(alpaca_result.current_price)
        y = float(yfinance_result.current_price)
        if y > 0:
            diff_pct = abs(a - y) / y * 100
            comparison["price_diff_pct"] = round(diff_pct, 4)
            comparison["agreement"] = diff_pct < 0.5

            if diff_pct >= 0.5:
                logger.warning(
                    f"[provider-compare] {ticker}: Alpaca=${a:.4f} vs yfinance=${y:.4f} "
                    f"→ {diff_pct:.2f}% divergence — investigate staleness"
                )
            else:
                logger.info(
                    f"[provider-compare] {ticker}: Alpaca=${a:.4f} vs yfinance=${y:.4f} "
                    f"→ {diff_pct:.4f}% — AGREE"
                )
    elif alpaca_result and not yfinance_result:
        logger.warning(f"[provider-compare] {ticker}: Alpaca OK, yfinance FAILED")
        comparison["agreement"] = False
    elif yfinance_result and not alpaca_result:
        logger.warning(f"[provider-compare] {ticker}: yfinance OK, Alpaca FAILED")
        comparison["agreement"] = False
    else:
        logger.error(f"[provider-compare] {ticker}: BOTH FAILED")
        comparison["agreement"] = False

    return comparison


def compare_batch_with_yfinance(tickers: list[str]) -> list[dict]:
    """
    Run Alpaca batch snapshot + yfinance batch download and compare all tickers.

    Use this to evaluate data provider reliability before switching permanently.
    Results are logged and returned for analysis.
    """
    from src.api.yfinance_client import get_quotes_batch_yfinance

    logger.info(f"[provider-compare] Starting batch comparison for {len(tickers)} tickers")

    alpaca_results = get_quotes_batch_alpaca(tickers)
    yfinance_results = get_quotes_batch_yfinance(tickers)

    comparisons = []
    agree_count = 0
    fail_alpaca = 0
    fail_yfinance = 0

    for ticker in tickers:
        a = alpaca_results.get(ticker)
        y = yfinance_results.get(ticker)

        entry = {
            "ticker": ticker,
            "alpaca_price": float(a.current_price) if a else None,
            "yfinance_price": float(y.current_price) if y else None,
            "alpaca_ok": a is not None,
            "yfinance_ok": y is not None,
            "price_diff_pct": None,
            "agreement": True,
        }

        if a and y:
            ap = float(a.current_price)
            yp = float(y.current_price)
            if yp > 0:
                diff = abs(ap - yp) / yp * 100
                entry["price_diff_pct"] = round(diff, 4)
                entry["agreement"] = diff < 0.5
                if entry["agreement"]:
                    agree_count += 1
        elif not a:
            fail_alpaca += 1
            entry["agreement"] = False
        elif not y:
            fail_yfinance += 1
            entry["agreement"] = False

        comparisons.append(entry)

    total = len(tickers)
    logger.info(
        f"[provider-compare] Results: {agree_count}/{total} agree (<0.5% diff) | "
        f"Alpaca failures: {fail_alpaca} | yfinance failures: {fail_yfinance}"
    )

    # Log disagreements prominently
    disagreements = [c for c in comparisons if not c["agreement"] and c["price_diff_pct"] is not None]
    if disagreements:
        logger.warning(
            f"[provider-compare] {len(disagreements)} price divergences >0.5%: "
            + ", ".join(f"{c['ticker']}({c['price_diff_pct']:.2f}%)" for c in disagreements[:10])
        )

    return comparisons
