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
    pc_ratio: float | None = None
    pfcf_ratio: float | None = None
    ev_ebitda: float | None = None
    ev_sales: float | None = None
    enterprise_value_text: str | None = None
    # Earnings
    eps_ttm: float | None = None
    eps_next_year: float | None = None
    eps_next_quarter: float | None = None
    eps_this_year: float | None = None
    eps_past_5y: float | None = None
    eps_next_5y: float | None = None
    eps_qoq: float | None = None
    eps_yoy_ttm: float | None = None
    # Growth
    sales_past_5y: float | None = None
    sales_qoq: float | None = None
    sales_yoy_ttm: float | None = None
    # Profitability
    profit_margin: float | None = None
    oper_margin: float | None = None
    gross_margin: float | None = None
    roa: float | None = None
    roe: float | None = None
    roi: float | None = None
    roic: float | None = None
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
    employees: float | None = None
    ipo_date: str | None = None
    earnings_date: str | None = None
    prev_close: float | None = None
    current_price_raw: float | None = None
    price_change_pct: float | None = None
    volume_raw: str | None = None
    analyst_recom: float | None = None  # 1=Strong Buy .. 5=Strong Sell
    # Ownership / float
    shares_outstanding_text: str | None = None
    shares_float_text: str | None = None
    short_float: float | None = None
    short_ratio: float | None = None
    short_interest_text: str | None = None
    option_short: str | None = None
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


class InsiderTrade(BaseModel):
    name: str
    relationship: str
    date: str
    transaction: str  # Buy, Sale, Option Exercise, etc.
    cost: float | None = None      # price per share
    shares: float | None = None
    value_usd: float | None = None
    shares_total: float | None = None


class FinvizAnalystRating(BaseModel):
    date: str
    action: str   # Initiated, Upgrade, Downgrade, Reiterated, Resumed
    analyst: str
    rating_change: str
    price_target: float | None = None


class QuantMetrics(BaseModel):
    """Computed quantitative metrics for a ticker over the look-back period."""
    ticker: str
    benchmark: str = "SPY"
    period_days: int = 252
    # Returns
    annualized_return: float | None = None       # %
    annualized_volatility: float | None = None   # %
    # Risk-adjusted
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    # Market relationship
    beta: float | None = None
    alpha_annualized: float | None = None        # Jensen's alpha %
    r_squared: float | None = None               # 0-1
    correlation: float | None = None             # vs benchmark
    # Drawdown
    max_drawdown: float | None = None            # %
    current_drawdown: float | None = None        # % from all-time high in window
    # Momentum
    rsi_14: float | None = None
    momentum_1m: float | None = None             # 1-month return %
    momentum_3m: float | None = None             # 3-month return %
    momentum_6m: float | None = None             # 6-month return %
    # Notes
    data_start: str | None = None
    data_end: str | None = None
    error: str | None = None                     # set if computation failed


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
    finviz_analyst_ratings: list[FinvizAnalystRating] = Field(default_factory=list)
    insider_trades: list[InsiderTrade] = Field(default_factory=list)
    technicals: TechnicalIndicators | None = None
    fundamentals: FinvizFundamentals | None = None
    quant_metrics: QuantMetrics | None = None
    sec_filings: list[dict] = Field(default_factory=list)
    consensus_rating: str | None = None
    consensus_price_target: Decimal | None = None
