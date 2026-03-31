"""Yahoo Finance scraper using the public JSON search API (no JS/auth required)."""
from datetime import datetime, timezone

import requests
from loguru import logger

from src.scrapers._utils import is_safe_url
from src.scrapers.base_scraper import BaseScraper, ScrapedArticle
from src.scrapers.user_agents import random_user_agent

YAHOO_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"


def _get_article_body(url: str, headers: dict) -> str | None:
    """Fetch article URL and extract body text."""
    if not is_safe_url(url):
        return None
    try:
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "iframe"]):
            tag.decompose()
        for selector in [".caas-body", "article", ".article-body", ".body", "[data-testid='article-body']", "main"]:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(separator=" ", strip=True)
                if len(text) > 200:
                    return text[:3000]
        paragraphs = soup.find_all("p")
        text = " ".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 50)
        return text[:3000] if text else None
    except Exception as e:
        logger.debug(f"[yahoo_finance] Article fetch failed for {url}: {e}")
        return None


class YahooFinanceScraper(BaseScraper):
    source_name = "yahoo_finance"

    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        headers = {
            "User-Agent": random_user_agent(),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://finance.yahoo.com/",
        }
        params = {
            "q": ticker,
            "newsCount": 20,
            "quotesCount": 0,
            "enableFuzzyQuery": False,
            "lang": "en-US",
        }

        try:
            resp = requests.get(YAHOO_SEARCH_URL, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"[yahoo_finance] API request failed for {ticker}: {e}")
            return []

        news_items = data.get("news", [])
        if not news_items:
            logger.warning(f"[yahoo_finance] No news returned for {ticker}")
            return []

        fetch_headers = self._get_headers()
        articles = []
        for item in news_items[:15]:
            headline = item.get("title", "").strip()
            url = item.get("link", "").strip()
            if not headline or not url or not url.startswith("http"):
                continue

            publisher = item.get("publisher", "yahoo_finance")
            ts = item.get("providerPublishTime")
            published_at = (
                datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
            )

            self._sleep()
            body = _get_article_body(url, fetch_headers)

            articles.append(
                ScrapedArticle(
                    ticker=ticker,
                    headline=headline,
                    url=url,
                    source=publisher or "yahoo_finance",
                    body=body,
                    published_at=published_at,
                )
            )

        logger.info(f"[yahoo_finance] {len(articles)} articles for {ticker}")
        return articles
