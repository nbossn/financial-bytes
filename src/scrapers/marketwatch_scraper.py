"""MarketWatch scraper using their search API + RSS feed (no Playwright)."""
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from loguru import logger

from src.scrapers._utils import is_safe_url
from src.scrapers.base_scraper import BaseScraper, ScrapedArticle
from src.scrapers.user_agents import random_user_agent

MARKETWATCH_SEARCH_API = "https://www.marketwatch.com/search"
MARKETWATCH_BASE = "https://www.marketwatch.com"


def _fetch_snippet(url: str, headers: dict) -> str | None:
    """Fetch pre-paywall paragraph text from a MarketWatch article."""
    if not is_safe_url(url):
        return None
    try:
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        # MarketWatch shows a few paragraphs before the paywall wall
        paragraphs = soup.select(".article__body p, .paywall p, .full-story p")
        if not paragraphs:
            paragraphs = soup.find_all("p")
        texts = []
        for p in paragraphs[:6]:
            text = p.get_text(strip=True)
            if len(text) > 30:
                texts.append(text)
        return " ".join(texts)[:1500] if texts else None
    except Exception as e:
        logger.debug(f"[marketwatch] Snippet fetch failed for {url}: {e}")
        return None


class MarketWatchScraper(BaseScraper):
    source_name = "marketwatch"

    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        params = {
            "q": ticker,
            "m": "article",
            "rpp": 15,
            "mp": 806,
            "bd": "false",
            "rs": "true",
        }
        headers = {
            "User-Agent": random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.marketwatch.com/",
        }

        articles = []
        seen_urls: set[str] = set()

        try:
            resp = requests.get(
                MARKETWATCH_SEARCH_API, params=params, headers=headers, timeout=20
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            results = soup.select(
                ".article__headline a, .searchresult a, h3.article__headline a, "
                ".search-headline a, .element--article a"
            )

            fetch_headers = self._get_headers()
            for a_tag in results[:12]:
                href = a_tag.get("href", "").strip()
                if not href or href in seen_urls:
                    continue
                if not href.startswith("http"):
                    href = urljoin(MARKETWATCH_BASE, href)
                seen_urls.add(href)

                headline = a_tag.get_text(strip=True)
                if not headline:
                    continue

                self._sleep()
                snippet = _fetch_snippet(href, fetch_headers)

                articles.append(
                    ScrapedArticle(
                        ticker=ticker,
                        headline=headline,
                        url=href,
                        source="marketwatch",
                        snippet=snippet,
                    )
                )

        except Exception as e:
            logger.warning(f"[marketwatch] Search failed for {ticker}: {e}")

        logger.info(f"[marketwatch] {len(articles)} articles for {ticker}")
        return articles
