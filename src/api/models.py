from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class FinvizFundamentals(BaseModel):
    """Full snapshot table data from Finviz — valuation, profitability, ownership, etc."""
    # Valuation
    pe_ratio: float | None = None
    forward_pe: float | None = None
    peg_ratio: float | None = None
    ps_ratio: float | None = None
    pb_ratio: float | None = None
    pfcf_ratio: float | None = None
    # Earnings
    eps_ttm: float | None = None
    eps_next_year: float | None = None
    eps_next_quarter: float | None = None
    eps_this_year: float | None = None
    eps_past_5y: float | None = None
    eps_next_5y: float | None = None
    eps_qoq: float | None = None
    # Growth
    sales_past_5y: float | None = None
    sales_qoq: float | None = None
    # Profitability
    profit_margin: float | None = None
    oper_margin: float | None = None
    gross_margin: float | None = None
    roa: float | None = None
    roe: float | None = None
    roi: float | None = None
    # Financial strength
    current_ratio: float | None = None
    quick_ratio: float | None = None
    lt_debt_eq: float | None = None
    debt_eq: float | None = None
    # Market data
    market_cap_text: str | None = None
    income_text: str | None = None
    sales_text: str | None = None
    book_per_share: float | None = None
    cash_per_share: float | None = None
    dividend: float | None = None
    dividend_pct: float | None = None
    target_price: float | None = None
    # Ownership / float
    shares_outstanding_text: str | None = None
    shares_float_text: str | None = None
    short_float: float | None = None
    short_ratio: float | None = None
    insider_own: float | None = None
    inst_own: float | None = None
    insider_trans: float | None = None
    inst_trans: float | None = None
    avg_volume_text: str | None = None
    rel_volume: float | None = None
    # 52-week & performance
    high_52w: float | None = None
    low_52w: float | None = None
    range_52w: str | None = None
    perf_week: float | None = None
    perf_month: float | None = None
    perf_quarter: float | None = None
    perf_half_year: float | None = None
    perf_year: float | None = None
    perf_ytd: float | None = None
    volatility_week: float | None = None
    volatility_month: float | None = None
    atr: float | None = None


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
    sma_20: float | None = None
    sma_50: float | None = None
    sma_200: float | None = None
    beta: float | None = None
    chart_daily_url: str | None = None
    chart_weekly_url: str | None = None
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
    """Aggregated signals for one ticker from massive.com + Finviz."""
    ticker: str
    quote: QuoteSnapshot | None = None
    news: list[BenzingaArticle] = Field(default_factory=list)
    analyst_ratings: list[AnalystRating] = Field(default_factory=list)
    technicals: TechnicalIndicators | None = None
    fundamentals: FinvizFundamentals | None = None
    sec_filings: list[dict] = Field(default_factory=list)
    consensus_rating: str | None = None
    consensus_price_target: Decimal | None = None
