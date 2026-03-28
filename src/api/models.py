from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class BenzingaArticle(BaseModel):
    id: str
    ticker: str
    headline: str
    url: str
    summary: str | None = None
    body: str | None = None
    source: str | None = None
    published_at: datetime | None = None
    sentiment: float | None = None  # -1.0 to 1.0


class AnalystRating(BaseModel):
    ticker: str
    analyst_firm: str | None = None
    rating: str | None = None          # Buy, Hold, Sell, Overweight, etc.
    price_target: Decimal | None = None
    previous_rating: str | None = None
    previous_price_target: Decimal | None = None
    rating_date: datetime | None = None


class TechnicalIndicators(BaseModel):
    ticker: str
    rsi: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    ema_20: float | None = None
    ema_50: float | None = None
    signal_summary: str | None = None  # derived: "Overbought", "Neutral", "Oversold"


class QuoteSnapshot(BaseModel):
    ticker: str
    current_price: Decimal
    prev_close: Decimal | None = None
    day_change: Decimal | None = None
    day_change_pct: Decimal | None = None
    volume: int | None = None
    market_cap: Decimal | None = None
    as_of: datetime | None = None


class TickerSignals(BaseModel):
    """Aggregated signals for one ticker from massive.com."""
    ticker: str
    quote: QuoteSnapshot | None = None
    news: list[BenzingaArticle] = Field(default_factory=list)
    analyst_ratings: list[AnalystRating] = Field(default_factory=list)
    technicals: TechnicalIndicators | None = None
    consensus_rating: str | None = None
    consensus_price_target: Decimal | None = None
