"""Tests for analyst and director agents."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from src.agents.analyst_agent import AnalystReport, analyze_ticker
from src.agents.director_agent import DirectorReport, StockSignal, synthesize_portfolio
from src.portfolio.models import Holding, PortfolioSnapshot
from src.scrapers.base_scraper import ScrapedArticle


VALID_ANALYST_JSON = {
    "ticker": "MSFT",
    "summary": "Microsoft shows strong AI momentum.",
    "sentiment": 0.72,
    "sentiment_label": "Bullish",
    "recommendation": "BUY",
    "recommendation_context": "Buy on current dip. Short-term $380. Long-term $450.",
    "confidence": 0.81,
    "key_catalysts": ["Azure AI", "Copilot"],
    "key_risks": ["Regulation"],
    "analyst_consensus": "Strong Buy",
    "price_target": 450.0,
    "technical_signal": "RSI 42, MACD bullish.",
}

VALID_DIRECTOR_JSON = {
    "market_theme": "AI dominates markets.",
    "five_min_summary": "Portfolio looks healthy. No immediate action needed.",
    "portfolio_summary": "Up 5.2% overall.",
    "global_market_context": "S&P +0.3%, Asia mixed.",
    "top_opportunities": [
        {"ticker": "MSFT", "signal": "BUY", "rationale": "Cheap vs. fair value.",
         "short_term": "$380", "long_term": "$450+"},
    ],
    "top_risks": [
        {"ticker": "NVDA", "risk": "Export restrictions", "severity": "Medium",
         "mitigation": "Diversify."},
    ],
    "action_items": ["Watch NVDA earnings", "Add MSFT on dips"],
    "overall_sentiment": 0.65,
    "overall_recommendation": "HOLD; add MSFT on weakness.",
}


class TestAnalystAgent:
    def test_analyze_ticker_valid_response(self, sample_holdings, sample_articles):
        holding = sample_holdings[0]  # MSFT

        with patch("src.agents.analyst_agent._call_claude") as mock_claude, \
             patch("src.agents.analyst_agent._save_report"):
            mock_claude.return_value = json.dumps(VALID_ANALYST_JSON)
            report = analyze_ticker(holding, sample_articles, report_date=date(2026, 3, 27))

        assert report.ticker == "MSFT"
        assert report.recommendation == "BUY"
        assert report.sentiment == pytest.approx(0.72)
        assert report.confidence == pytest.approx(0.81)
        assert "Azure AI" in report.key_catalysts

    def test_analyze_ticker_handles_code_fences(self, sample_holdings, sample_articles):
        holding = sample_holdings[0]
        fenced = f"```json\n{json.dumps(VALID_ANALYST_JSON)}\n```"

        with patch("src.agents.analyst_agent._call_claude") as mock_claude, \
             patch("src.agents.analyst_agent._save_report"):
            mock_claude.return_value = fenced
            report = analyze_ticker(holding, sample_articles, report_date=date(2026, 3, 27))

        assert report.recommendation == "BUY"

    def test_analyze_ticker_json_parse_fallback(self, sample_holdings, sample_articles):
        holding = sample_holdings[0]

        with patch("src.agents.analyst_agent._call_claude") as mock_claude, \
             patch("src.agents.analyst_agent._save_report"):
            mock_claude.return_value = "This is not JSON at all."
            report = analyze_ticker(holding, sample_articles, report_date=date(2026, 3, 27))

        # Falls back to HOLD minimal report
        assert report.recommendation == "HOLD"
        assert report.confidence == pytest.approx(0.1)

    def test_analyst_report_sentiment_bounds(self):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            AnalystReport(
                ticker="X", report_date=date.today(), article_count=0,
                summary="s", sentiment=2.0,  # Out of range
                sentiment_label="Bullish", recommendation="BUY",
                recommendation_context="ctx", confidence=0.5,
            )

    def test_analyst_report_confidence_bounds(self):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            AnalystReport(
                ticker="X", report_date=date.today(), article_count=0,
                summary="s", sentiment=0.5,
                sentiment_label="Bullish", recommendation="BUY",
                recommendation_context="ctx", confidence=1.5,  # Out of range
            )


class TestDirectorAgent:
    def test_synthesize_portfolio_valid(self, sample_holdings, sample_analyst_report):
        snapshot = PortfolioSnapshot(holdings=sample_holdings)

        with patch("src.agents.director_agent._call_claude") as mock_claude, \
             patch("src.agents.director_agent._save_report"), \
             patch("src.agents.director_agent._get_global_market_context") as mock_ctx:
            mock_claude.return_value = json.dumps(VALID_DIRECTOR_JSON)
            mock_ctx.return_value = "S&P +0.3%"

            report = synthesize_portfolio(snapshot, [sample_analyst_report],
                                          report_date=date(2026, 3, 27))

        assert report.market_theme == "AI dominates markets."
        assert report.overall_sentiment == pytest.approx(0.65)
        assert len(report.top_opportunities) == 1
        assert len(report.top_risks) == 1
        assert report.top_opportunities[0].ticker == "MSFT"

    def test_synthesize_handles_code_fences(self, sample_holdings, sample_analyst_report):
        snapshot = PortfolioSnapshot(holdings=sample_holdings)
        fenced = f"```json\n{json.dumps(VALID_DIRECTOR_JSON)}\n```"

        with patch("src.agents.director_agent._call_claude") as mock_claude, \
             patch("src.agents.director_agent._save_report"), \
             patch("src.agents.director_agent._get_global_market_context") as mock_ctx:
            mock_claude.return_value = fenced
            mock_ctx.return_value = "S&P +0.3%"

            report = synthesize_portfolio(snapshot, [sample_analyst_report])

        assert report.market_theme == "AI dominates markets."

    def test_synthesize_json_parse_fallback(self, sample_holdings, sample_analyst_report):
        snapshot = PortfolioSnapshot(holdings=sample_holdings)

        with patch("src.agents.director_agent._call_claude") as mock_claude, \
             patch("src.agents.director_agent._save_report"), \
             patch("src.agents.director_agent._get_global_market_context") as mock_ctx:
            mock_claude.return_value = "not json"
            mock_ctx.return_value = "unavailable"

            report = synthesize_portfolio(snapshot, [sample_analyst_report])

        assert report.market_theme == "Analysis unavailable"
        assert report.overall_sentiment == pytest.approx(0.0)

    def test_director_report_sentiment_bounds(self):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            DirectorReport(
                report_date=date.today(), market_theme="t",
                five_min_summary="s", portfolio_summary="s",
                global_market_context="s", overall_sentiment=2.0,  # Out of range
                overall_recommendation="HOLD",
            )
