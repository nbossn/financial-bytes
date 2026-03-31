"""Unit tests for the scraper reliability fixes.

Covers:
1. SQLAlchemy session detachment fix in _load_cached_articles
2. Yahoo Finance JSON API scraper
3. CNBC Queryly API scraper
4. MarketWatch requests scraper
5. Google News RSS scraper
6. Morningstar ETF URL routing
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from src.scrapers.base_scraper import ScrapedArticle, ScrapeResult


# ── 1. SQLAlchemy session fix ────────────────────────────────────────────────

class TestLoadCachedArticles:
    """_load_cached_articles must build ScrapedArticle objects INSIDE the session block."""

    def test_returns_none_when_no_rows(self):
        from src.scrapers.scraper_orchestrator import _load_cached_articles

        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

        with patch("src.scrapers.scraper_orchestrator.get_db", return_value=mock_db):
            result = _load_cached_articles("MSFT")
        assert result is None

    def test_returns_articles_inside_session(self):
        """Ensure list comprehension runs inside the with-block (no DetachedInstanceError)."""
        from src.scrapers.scraper_orchestrator import _load_cached_articles

        row = MagicMock()
        row.ticker = "MSFT"
        row.headline = "Headline"
        row.url = "https://example.com"
        row.source = "test"
        row.body = "body text"
        row.snippet = None
        row.published_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [row]

        with patch("src.scrapers.scraper_orchestrator.get_db", return_value=mock_db):
            result = _load_cached_articles("MSFT")

        assert result is not None
        assert len(result) == 1
        assert result[0].ticker == "MSFT"
        assert result[0].headline == "Headline"
        assert result[0].url == "https://example.com"


# ── 2. Yahoo Finance JSON API ────────────────────────────────────────────────

class TestYahooFinanceScraper:
    def _make_api_response(self, ticker: str = "MSFT") -> dict:
        return {
            "news": [
                {
                    "title": f"{ticker} reports record revenue",
                    "link": f"https://finance.yahoo.com/news/{ticker.lower()}-q2-results",
                    "publisher": "Reuters",
                    "providerPublishTime": 1700000000,
                },
                {
                    "title": f"{ticker} stock analysis",
                    "link": f"https://example.com/{ticker.lower()}-analysis",
                    "publisher": "Benzinga",
                    "providerPublishTime": 1699900000,
                },
            ]
        }

    def test_parses_api_response(self):
        from src.scrapers.yahoo_finance_scraper import YahooFinanceScraper

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = self._make_api_response()

        article_resp = MagicMock()
        article_resp.raise_for_status = MagicMock()
        article_resp.text = "<html><body><article><p>Article content long enough here.</p></article></body></html>"

        with patch("requests.get", side_effect=[mock_resp, article_resp, article_resp]) as mock_get:
            with patch.object(YahooFinanceScraper, "_sleep"):
                scraper = YahooFinanceScraper()
                articles = scraper._scrape("MSFT")

        assert len(articles) == 2
        assert articles[0].ticker == "MSFT"
        assert "record revenue" in articles[0].headline
        assert articles[0].source == "Reuters"
        assert articles[0].published_at is not None

    def test_empty_news_returns_empty_list(self):
        from src.scrapers.yahoo_finance_scraper import YahooFinanceScraper

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"news": []}

        with patch("requests.get", return_value=mock_resp):
            scraper = YahooFinanceScraper()
            articles = scraper._scrape("MSFT")

        assert articles == []

    def test_api_failure_returns_empty_list(self):
        from src.scrapers.yahoo_finance_scraper import YahooFinanceScraper

        with patch("requests.get", side_effect=Exception("connection error")):
            scraper = YahooFinanceScraper()
            articles = scraper._scrape("MSFT")

        assert articles == []

    def test_skips_items_without_url(self):
        from src.scrapers.yahoo_finance_scraper import YahooFinanceScraper

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "news": [
                {"title": "Good article", "link": "https://example.com/good"},
                {"title": "Missing URL", "link": ""},
                {"title": "", "link": "https://example.com/no-title"},
            ]
        }

        article_resp = MagicMock()
        article_resp.raise_for_status = MagicMock()
        article_resp.text = "<html><body><p>Content</p></body></html>"

        with patch("requests.get", side_effect=[mock_resp, article_resp]):
            with patch.object(YahooFinanceScraper, "_sleep"):
                scraper = YahooFinanceScraper()
                articles = scraper._scrape("MSFT")

        # Only 1 valid article (non-empty title + valid URL)
        assert len(articles) == 1
        assert articles[0].headline == "Good article"


# ── 3. CNBC Queryly API ──────────────────────────────────────────────────────

class TestCNBCScraper:
    def _make_api_response(self) -> dict:
        return {
            "items": [
                {
                    "cn:title": "CNBC: Microsoft Azure beats estimates",
                    "link": "https://www.cnbc.com/2026/01/01/msft-azure.html",
                    "pubdate": "2026-01-01T09:00:00Z",
                },
                {
                    "title": "CNBC: Nadella on AI investment",
                    "link": "https://www.cnbc.com/2026/01/02/msft-ai.html",
                    "pubdate": "2026-01-02T10:00:00Z",
                },
            ]
        }

    def test_parses_search_response(self):
        from src.scrapers.cnbc_scraper import CNBCScraper

        api_resp = MagicMock()
        api_resp.raise_for_status = MagicMock()
        api_resp.json.return_value = self._make_api_response()

        article_resp = MagicMock()
        article_resp.raise_for_status = MagicMock()
        article_resp.text = "<html><body><article><p>Long body content here.</p></article></body></html>"

        with patch("requests.get", side_effect=[api_resp, article_resp, article_resp]):
            with patch.object(CNBCScraper, "_sleep"):
                scraper = CNBCScraper()
                articles = scraper._scrape("MSFT")

        assert len(articles) == 2
        assert articles[0].ticker == "MSFT"
        assert "Azure" in articles[0].headline
        assert articles[0].source == "cnbc"

    def test_empty_items_returns_empty_list(self):
        from src.scrapers.cnbc_scraper import CNBCScraper

        api_resp = MagicMock()
        api_resp.raise_for_status = MagicMock()
        api_resp.json.return_value = {"items": []}

        with patch("requests.get", return_value=api_resp):
            scraper = CNBCScraper()
            articles = scraper._scrape("MSFT")

        assert articles == []


# ── 4. MarketWatch requests scraper ─────────────────────────────────────────

class TestMarketWatchScraper:
    SEARCH_HTML = """
    <html><body>
    <h3 class="article__headline">
      <a href="https://www.marketwatch.com/story/msft-results">Microsoft beats Q2</a>
    </h3>
    </body></html>
    """

    def test_parses_search_html(self):
        from src.scrapers.marketwatch_scraper import MarketWatchScraper

        search_resp = MagicMock()
        search_resp.raise_for_status = MagicMock()
        search_resp.text = self.SEARCH_HTML

        snippet_resp = MagicMock()
        snippet_resp.raise_for_status = MagicMock()
        snippet_resp.text = "<html><body><div class='article__body'><p>Pre-paywall content here.</p></div></body></html>"

        with patch("requests.get", side_effect=[search_resp, snippet_resp]):
            with patch.object(MarketWatchScraper, "_sleep"):
                scraper = MarketWatchScraper()
                articles = scraper._scrape("MSFT")

        assert len(articles) == 1
        assert articles[0].headline == "Microsoft beats Q2"
        assert articles[0].source == "marketwatch"

    def test_search_failure_returns_empty(self):
        from src.scrapers.marketwatch_scraper import MarketWatchScraper

        with patch("requests.get", side_effect=Exception("timeout")):
            scraper = MarketWatchScraper()
            articles = scraper._scrape("MSFT")

        assert articles == []


# ── 5. Google News RSS ───────────────────────────────────────────────────────

GOOGLE_RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>MSFT stock - Google News</title>
    <item>
      <title>Microsoft Reports Record Q2 Revenue</title>
      <link>https://example.com/msft-q2</link>
      <pubDate>Mon, 01 Jan 2026 10:00:00 +0000</pubDate>
      <source url="https://reuters.com">Reuters</source>
    </item>
    <item>
      <title>Azure cloud growth accelerates</title>
      <link>https://example.com/azure-growth</link>
      <pubDate>Mon, 01 Jan 2026 08:00:00 +0000</pubDate>
      <source url="https://bloomberg.com">Bloomberg</source>
    </item>
  </channel>
</rss>"""


class TestGoogleNewsScraper:
    def test_parses_rss_feed(self):
        from src.scrapers.google_news_scraper import GoogleNewsScraper

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.content = GOOGLE_RSS_SAMPLE

        with patch("requests.get", return_value=mock_resp):
            scraper = GoogleNewsScraper()
            articles = scraper._scrape("MSFT")

        assert len(articles) == 2
        assert articles[0].ticker == "MSFT"
        assert "Record Q2 Revenue" in articles[0].headline
        assert articles[0].source == "Reuters"
        assert articles[0].published_at is not None

    def test_company_name_used_for_adrs(self):
        """For ADR tickers like AJINY, the query should use the company name."""
        from src.scrapers.google_news_scraper import GoogleNewsScraper, COMPANY_NAME_MAP

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.content = GOOGLE_RSS_SAMPLE

        captured_params = {}
        def capture_get(url, params=None, **kwargs):
            captured_params.update(params or {})
            return mock_resp

        with patch("requests.get", side_effect=capture_get):
            scraper = GoogleNewsScraper()
            scraper._scrape("AJINY")

        assert "Ajinomoto" in captured_params.get("q", "")

    def test_fetch_failure_returns_empty(self):
        from src.scrapers.google_news_scraper import GoogleNewsScraper

        with patch("requests.get", side_effect=Exception("DNS failure")):
            scraper = GoogleNewsScraper()
            articles = scraper._scrape("MSFT")

        assert articles == []

    def test_invalid_rss_returns_empty(self):
        from src.scrapers.google_news_scraper import GoogleNewsScraper

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.content = b"not valid xml!!!"

        with patch("requests.get", return_value=mock_resp):
            scraper = GoogleNewsScraper()
            articles = scraper._scrape("MSFT")

        assert articles == []


# ── 6. Morningstar ETF URL routing ───────────────────────────────────────────

class TestMorningstarETFRouting:
    def test_etf_uses_etf_url(self):
        """VOO, QQQ etc. should hit /etfs/ not /stocks/."""
        from src.scrapers.morningstar_scraper import MorningstarScraper

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = "<html><body></body></html>"

        captured_url = {}
        def capture_get(url, **kwargs):
            captured_url["url"] = url
            return mock_resp

        with patch("requests.get", side_effect=capture_get):
            with patch.object(MorningstarScraper, "_sleep"):
                scraper = MorningstarScraper()
                scraper._scrape("VOO")

        assert "/etfs/" in captured_url["url"]
        assert "/stocks/" not in captured_url["url"]

    def test_stock_uses_stock_url(self):
        from src.scrapers.morningstar_scraper import MorningstarScraper

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = "<html><body></body></html>"

        captured_url = {}
        def capture_get(url, **kwargs):
            captured_url["url"] = url
            return mock_resp

        with patch("requests.get", side_effect=capture_get):
            with patch.object(MorningstarScraper, "_sleep"):
                scraper = MorningstarScraper()
                scraper._scrape("MSFT")

        assert "/stocks/" in captured_url["url"]
        assert "/etfs/" not in captured_url["url"]

    def test_unknown_ticker_defaults_to_stock(self):
        """Tickers not in any map should default to stock URL."""
        from src.scrapers.morningstar_scraper import MorningstarScraper

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = "<html><body></body></html>"

        captured_url = {}
        def capture_get(url, **kwargs):
            captured_url["url"] = url
            return mock_resp

        with patch("requests.get", side_effect=capture_get):
            with patch.object(MorningstarScraper, "_sleep"):
                scraper = MorningstarScraper()
                scraper._scrape("UNKN")

        assert "/stocks/" in captured_url["url"]
