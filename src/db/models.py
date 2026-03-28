from datetime import date, datetime
from decimal import Decimal

from datetime import timezone

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import JSON as JSONB

_now = lambda: datetime.now(timezone.utc).replace(tzinfo=None)  # noqa: E731
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Portfolio(Base):
    __tablename__ = "portfolio"

    id = Column(Integer, primary_key=True)
    portfolio_name = Column(String(100), nullable=False, default="default", index=True)
    ticker = Column(String(10), nullable=False)
    shares = Column(Numeric(15, 4), nullable=False)
    cost_basis = Column(Numeric(15, 4), nullable=False)
    purchase_date = Column(Date, nullable=False)
    created_at = Column(DateTime, default=_now)


class Article(Base):
    __tablename__ = "articles"

    id = Column(Integer, primary_key=True)
    ticker = Column(String(10), nullable=False, index=True)
    headline = Column(Text, nullable=False)
    url = Column(Text, unique=True, nullable=False)
    source = Column(String(100))
    body = Column(Text)
    snippet = Column(Text)
    published_at = Column(DateTime, index=True)
    scraped_at = Column(DateTime, default=_now)


class ApiSignal(Base):
    __tablename__ = "api_signals"

    id = Column(Integer, primary_key=True)
    ticker = Column(String(10), nullable=False, index=True)
    signal_date = Column(Date, nullable=False)
    current_price = Column(Numeric(15, 4))
    day_change_pct = Column(Numeric(8, 4))
    rsi = Column(Numeric(8, 4))
    macd = Column(Numeric(12, 6))
    analyst_rating = Column(String(20))
    price_target = Column(Numeric(15, 4))
    benzinga_sentiment = Column(Numeric(4, 3))
    raw_data = Column(JSONB)
    created_at = Column(DateTime, default=_now)

    __table_args__ = (UniqueConstraint("ticker", "signal_date", name="uq_signal_ticker_date"),)


class Summary(Base):
    __tablename__ = "summaries"

    id = Column(Integer, primary_key=True)
    portfolio_name = Column(String(100), nullable=False, default="default", index=True)
    ticker = Column(String(10), nullable=False)
    report_date = Column(Date, nullable=False)
    summary = Column(Text, nullable=False)
    sentiment = Column(Numeric(4, 3))
    recommendation = Column(String(10))
    confidence = Column(Numeric(4, 3))
    key_catalysts = Column(JSONB)
    key_risks = Column(JSONB)
    analyst_consensus = Column(String(20))
    price_target = Column(Numeric(15, 4))
    technical_signal = Column(String(200))
    article_count = Column(Integer)
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint("ticker", "report_date", "portfolio_name", name="uq_summary_ticker_date_portfolio"),
    )


class Recommendation(Base):
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True)
    portfolio_name = Column(String(100), nullable=False, default="default", index=True)
    report_date = Column(Date, nullable=False)
    market_theme = Column(String(300))
    five_min_summary = Column(Text)
    portfolio_summary = Column(Text)
    global_market_context = Column(Text)
    action_items = Column(JSONB)
    top_opportunities = Column(JSONB)
    top_risks = Column(JSONB)
    overall_sentiment = Column(Numeric(4, 3))
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint("report_date", "portfolio_name", name="uq_recommendation_date_portfolio"),
    )


class Newsletter(Base):
    __tablename__ = "newsletters"

    id = Column(Integer, primary_key=True)
    portfolio_name = Column(String(100), nullable=False, default="default", index=True)
    report_date = Column(Date, nullable=False)
    html_content = Column(Text)
    markdown_content = Column(Text)
    file_path = Column(String(500))
    email_sent = Column(Boolean, default=False)
    email_sent_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint("report_date", "portfolio_name", name="uq_newsletter_date_portfolio"),
    )


class ScrapeLog(Base):
    __tablename__ = "scrape_logs"

    id = Column(Integer, primary_key=True)
    ticker = Column(String(10))
    source = Column(String(100))
    articles_found = Column(Integer)
    success = Column(Boolean)
    error_message = Column(Text)
    duration_ms = Column(Integer)
    scraped_at = Column(DateTime, default=_now)
