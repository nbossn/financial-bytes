"""Shared pytest fixtures for financial-bytes test suite."""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest

# ── Minimal env vars so Settings doesn't error on import ─────────
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-anthropic")
os.environ.setdefault("MASSIVE_API_KEY", "test-key-massive")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("EMAIL_RECIPIENT", "test@example.com")
os.environ.setdefault("EMAIL_FROM", "financial-bytes@example.com")
os.environ.setdefault("SMTP_USER", "test@example.com")
os.environ.setdefault("SMTP_PASS", "test-password")


@pytest.fixture
def sample_holdings():
    from src.portfolio.models import Holding
    return [
        Holding(ticker="MSFT", shares=Decimal("100"), cost_basis=Decimal("555.23"),
                purchase_date=date(2025, 8, 15)),
        Holding(ticker="NVDA", shares=Decimal("200"), cost_basis=Decimal("206.45"),
                purchase_date=date(2025, 11, 5)),
    ]


@pytest.fixture
def sample_snapshot(sample_holdings):
    from src.portfolio.models import PortfolioSnapshot
    return PortfolioSnapshot(holdings=sample_holdings)


@pytest.fixture
def sample_articles():
    from src.scrapers.base_scraper import ScrapedArticle
    return [
        ScrapedArticle(
            ticker="MSFT",
            headline="Microsoft Azure revenue surges 33% on AI demand",
            url="https://example.com/1",
            source="Reuters",
            body="Microsoft's cloud computing platform Azure grew 33% in the latest quarter...",
            snippet=None,
            published_at=None,
        ),
        ScrapedArticle(
            ticker="MSFT",
            headline="Copilot reaches 10 million enterprise users",
            url="https://example.com/2",
            source="CNBC",
            body="Microsoft's AI Copilot assistant has surpassed 10 million enterprise users...",
            snippet=None,
            published_at=None,
        ),
    ]


@pytest.fixture
def sample_analyst_report():
    from src.agents.analyst_agent import AnalystReport
    return AnalystReport(
        ticker="MSFT",
        report_date=date(2026, 3, 27),
        article_count=2,
        summary="Microsoft continues to show strong momentum in AI and cloud.",
        sentiment=0.72,
        sentiment_label="Bullish",
        recommendation="BUY",
        confidence=0.81,
        recommendation_context="Azure growth + Copilot adoption support a BUY thesis.",
        key_catalysts=["Azure AI", "Copilot enterprise"],
        key_risks=["Regulatory risk"],
        analyst_consensus="Strong Buy",
        price_target=450.0,
        technical_signal="RSI oversold, MACD bullish crossover.",
    )


@pytest.fixture
def sample_director_report(sample_analyst_report):
    from src.agents.director_agent import DirectorReport, StockSignal
    return DirectorReport(
        report_date=date(2026, 3, 27),
        market_theme="AI infrastructure dominates as tech rallies.",
        five_min_summary="Markets are healthy. MSFT is a buy on current weakness.",
        portfolio_summary="Portfolio up 5.2%.",
        global_market_context="S&P futures +0.3%. Asia mixed.",
        top_opportunities=[
            StockSignal(ticker="MSFT", signal="BUY", rationale="Cheap vs. fair value.",
                        short_term="$380 target", long_term="$450+ on AI"),
        ],
        top_risks=[
            StockSignal(ticker="NVDA", risk="Export restrictions", severity="Medium",
                        mitigation="Diversify with MSFT."),
        ],
        action_items=["Monitor NVDA earnings", "Add MSFT on dips"],
        overall_sentiment=0.65,
        overall_recommendation="HOLD portfolio; add MSFT on weakness.",
    )
