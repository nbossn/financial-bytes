"""Endpoint wrappers for massive.com API."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from src.api.massive_client import MassiveClient, MassiveAPIError
from src.api.models import (
    AnalystRating,
    BenzingaArticle,
    QuoteSnapshot,
    TechnicalIndicators,
    TickerSignals,
)
from src.config import settings


def _retry_decorator():
    return retry(
        retry=retry_if_exception_type(MassiveAPIError),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )


class MassiveEndpoints:
    def __init__(self, client: MassiveClient):
        self.client = client

    @_retry_decorator()
    def get_quote(self, ticker: str) -> QuoteSnapshot | None:
        """Get current quote / snapshot for a ticker."""
        try:
            data = self.client.get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")
            result = data.get("ticker", {})
            day = result.get("day", {})
            prev = result.get("prevDay", {})

            current = Decimal(str(result.get("lastTrade", {}).get("p") or day.get("c") or 0))
            prev_close = Decimal(str(prev.get("c", 0) or 0))
            change = current - prev_close if prev_close else Decimal(0)
            change_pct = (change / prev_close * 100) if prev_close else Decimal(0)

            return QuoteSnapshot(
                ticker=ticker,
                current_price=current,
                prev_close=prev_close,
                day_change=change,
                day_change_pct=change_pct,
                volume=day.get("v"),
                as_of=datetime.now(timezone.utc),
            )
        except (MassiveAPIError, Exception) as e:
            logger.warning(f"Could not get quote for {ticker}: {e}")
            return None

    @_retry_decorator()
    def get_news(self, ticker: str, lookback_hours: int | None = None) -> list[BenzingaArticle]:
        """Fetch Benzinga news articles for a ticker."""
        hours = lookback_hours or settings.article_lookback_hours
        published_after = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        try:
            data = self.client.get(
                "/v2/reference/news",
                params={
                    "ticker": ticker,
                    "published_utc.gte": published_after,
                    "limit": 50,
                    "sort": "published_utc",
                    "order": "desc",
                },
            )
            articles = []
            for item in data.get("results", []):
                tickers = item.get("tickers", [])
                articles.append(
                    BenzingaArticle(
                        id=str(item.get("id", "")),
                        ticker=ticker,
                        headline=item.get("title", ""),
                        url=item.get("article_url", ""),
                        summary=item.get("description"),
                        body=item.get("description"),  # full body if available
                        source=item.get("publisher", {}).get("name"),
                        published_at=_parse_dt(item.get("published_utc")),
                        sentiment=item.get("insights", [{}])[0].get("sentiment_score")
                        if item.get("insights") else None,
                    )
                )
            logger.info(f"massive.com: {len(articles)} news articles for {ticker}")
            return articles
        except MassiveAPIError:
            logger.warning(f"Could not fetch news for {ticker}")
            return []

    @_retry_decorator()
    def get_analyst_ratings(self, ticker: str) -> list[AnalystRating]:
        """Fetch analyst ratings and price targets for a ticker."""
        try:
            data = self.client.get(
                "/v2/reference/analysts",
                params={"ticker": ticker, "limit": 10, "sort": "date", "order": "desc"},
            )
            ratings = []
            for item in data.get("results", []):
                ratings.append(
                    AnalystRating(
                        ticker=ticker,
                        analyst_firm=item.get("analyst"),
                        rating=item.get("current_rating"),
                        price_target=Decimal(str(item["price_target"])) if item.get("price_target") else None,
                        previous_rating=item.get("previous_rating"),
                        previous_price_target=Decimal(str(item["previous_price_target"]))
                        if item.get("previous_price_target") else None,
                        rating_date=_parse_dt(item.get("date")),
                    )
                )
            logger.info(f"massive.com: {len(ratings)} analyst ratings for {ticker}")
            return ratings
        except MassiveAPIError:
            logger.warning(f"Could not fetch analyst ratings for {ticker}")
            return []

    @_retry_decorator()
    def get_technicals(self, ticker: str) -> TechnicalIndicators | None:
        """Fetch RSI, MACD, EMA technical indicators for a ticker."""
        indicators = TechnicalIndicators(ticker=ticker)
        try:
            rsi_data = self.client.get(
                f"/v1/indicators/rsi/{ticker}",
                params={"adjusted": "true", "window": 14, "series_type": "close", "limit": 1},
            )
            rsi_results = rsi_data.get("results", {}).get("values", [])
            if rsi_results:
                indicators.rsi = rsi_results[0].get("value")

            macd_data = self.client.get(
                f"/v1/indicators/macd/{ticker}",
                params={"adjusted": "true", "short_window": 12, "long_window": 26,
                        "signal_window": 9, "series_type": "close", "limit": 1},
            )
            macd_results = macd_data.get("results", {}).get("values", [])
            if macd_results:
                indicators.macd = macd_results[0].get("value")
                indicators.macd_signal = macd_results[0].get("signal")

            # Derive signal summary
            if indicators.rsi is not None:
                if indicators.rsi > 70:
                    indicators.signal_summary = "Overbought (RSI > 70)"
                elif indicators.rsi < 30:
                    indicators.signal_summary = "Oversold (RSI < 30)"
                else:
                    indicators.signal_summary = f"Neutral (RSI {indicators.rsi:.1f})"

            logger.info(f"massive.com: technicals fetched for {ticker} — RSI={indicators.rsi}")
            return indicators
        except MassiveAPIError:
            logger.warning(f"Could not fetch technicals for {ticker}")
            return None

    def get_ticker_signals(self, ticker: str, lookback_hours: int | None = None) -> TickerSignals:
        """Fetch all signals for a ticker in one call."""
        logger.info(f"Fetching massive.com signals for {ticker}")
        quote = self.get_quote(ticker)
        news = self.get_news(ticker, lookback_hours)
        ratings = self.get_analyst_ratings(ticker)
        technicals = self.get_technicals(ticker)

        # Compute consensus
        consensus_rating = None
        consensus_target = None
        if ratings:
            valid_targets = [r.price_target for r in ratings if r.price_target]
            if valid_targets:
                consensus_target = sum(valid_targets) / len(valid_targets)
            buy_count = sum(1 for r in ratings if r.rating and "buy" in r.rating.lower())
            sell_count = sum(1 for r in ratings if r.rating and "sell" in r.rating.lower())
            if buy_count > sell_count:
                consensus_rating = "Buy"
            elif sell_count > buy_count:
                consensus_rating = "Sell"
            else:
                consensus_rating = "Hold"

        return TickerSignals(
            ticker=ticker,
            quote=quote,
            news=news,
            analyst_ratings=ratings,
            technicals=technicals,
            consensus_rating=consensus_rating,
            consensus_price_target=consensus_target,
        )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
