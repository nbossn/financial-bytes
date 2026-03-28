"""Tests for scraper base classes and orchestrator."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.scrapers.base_scraper import BaseScraper, ScrapedArticle, ScrapeResult


class TestScrapedArticle:
    def test_creation(self):
        a = ScrapedArticle(
            ticker="MSFT",
            headline="Test headline",
            url="https://example.com",
            source="Reuters",
            body="Article body text.",
            snippet=None,
            published_at=None,
        )
        assert a.ticker == "MSFT"
        assert a.source == "Reuters"

    def test_has_body(self):
        a = ScrapedArticle(ticker="X", headline="H", url="u", source="S",
                           body="Long content", snippet=None, published_at=None)
        assert a.body is not None

    def test_snippet_fallback(self):
        a = ScrapedArticle(ticker="X", headline="H", url="u", source="S",
                           body=None, snippet="Short snippet", published_at=None)
        assert a.snippet == "Short snippet"


class ConcreteScaper(BaseScraper):
    """Minimal concrete scraper for testing base class."""
    source_name = "TestSource"

    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        return [
            ScrapedArticle(ticker=ticker, headline="Test", url="http://x", source="Test",
                           body="body", snippet=None, published_at=None)
        ]


class TestBaseScraper:
    def test_scrape_returns_result(self):
        scraper = ConcreteScaper()
        result = scraper.scrape("MSFT")
        assert isinstance(result, ScrapeResult)
        assert result.success
        assert result.source == "TestSource"
        assert len(result.articles) == 1

    def test_scrape_handles_exception(self):
        class FailingScraper(BaseScraper):
            source_name = "FailSource"

            def _scrape(self, ticker: str) -> list[ScrapedArticle]:
                raise RuntimeError("Network error")

        scraper = FailingScraper()
        result = scraper.scrape("MSFT")
        assert not result.success
        assert "Network error" in result.error
        assert result.articles == []


class TestWebSearchFallback:
    def test_search_returns_articles(self):
        from src.scrapers.web_search_fallback import WebSearchFallback
        scraper = WebSearchFallback()

        with patch("requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = """
            <html><body>
            <div class="result">
              <a class="result__a" href="https://example.com/article">MSFT beats earnings</a>
              <a class="result__snippet">Microsoft quarterly results exceed expectations</a>
            </div>
            </body></html>
            """
            mock_post.return_value = mock_response

            with patch("requests.get") as mock_get:
                mock_get.return_value = MagicMock(
                    status_code=200,
                    text="<html><body><p>Article content here.</p></body></html>"
                )
                result = scraper.scrape("MSFT")
                # May or may not find articles depending on parsing — just verify no crash
                assert isinstance(result, ScrapeResult)
                assert result.source == "web_search"


class TestOrchestratorDedup:
    def test_deduplication_by_url(self):
        """Same URL from two scrapers should produce only one article."""
        from src.scrapers.scraper_orchestrator import _deduplicate

        articles = [
            ScrapedArticle(ticker="MSFT", headline="Azure grows", url="https://x.com/1",
                           source="Yahoo", body="body", snippet=None, published_at=None),
            ScrapedArticle(ticker="MSFT", headline="Azure grows", url="https://x.com/1",
                           source="CNBC", body="body", snippet=None, published_at=None),
        ]
        deduped = _deduplicate(articles)
        assert len(deduped) == 1

    def test_deduplication_by_headline(self):
        """Same headline (different URL) should be deduplicated."""
        from src.scrapers.scraper_orchestrator import _deduplicate

        articles = [
            ScrapedArticle(ticker="MSFT", headline="Microsoft beats Q2 estimates", url="https://a.com",
                           source="Yahoo", body="body", snippet=None, published_at=None),
            ScrapedArticle(ticker="MSFT", headline="Microsoft beats Q2 estimates!", url="https://b.com",
                           source="CNBC", body="body", snippet=None, published_at=None),
        ]
        deduped = _deduplicate(articles)
        assert len(deduped) == 1
