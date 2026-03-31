"""CNBC scraper using the public Queryly search API (no JS required)."""
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from loguru import logger

from src.config import settings
from src.scrapers._utils import is_safe_url
from src.scrapers.base_scraper import BaseScraper, ScrapedArticle
from src.scrapers.user_agents import random_user_agent

# Queryly powers CNBC's own site search — key configurable via QUERYLY_API_KEY env var
CNBC_SEARCH_URL = "https://api.queryly.com/cnbc/json.aspx"
CNBC_BASE = "https://www.cnbc.com"


def _get_article_body(url: str, headers: dict) -> str | None:
    """Fetch CNBC article body."""
    if not is_safe_url(url):
        return None
    try:
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        for selector in [".ArticleBody-articleBody", ".group", "[data-module='ArticleBody']", "article"]:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(separator=" ", strip=True)
                if len(text) > 200:
                    return text[:3000]
        paragraphs = soup.find_all("p")
        text = " ".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 50)
        return text[:3000] if text else None
    except Exception as e:
        logger.debug(f"[cnbc] Article fetch failed for {url}: {e}")
        return None


class CNBCScraper(BaseScraper):
    source_name = "cnbc"

    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        params = {
            "queryly_key": settings.queryly_api_key,
            "query": ticker,
            "endindex": 0,
            "num": 20,
            "callback": "",
            "showfaceted": "false",
            "tm": "1",
            "content_type": "cnbcarticle",
        }
        headers = {
            "User-Agent": random_user_agent(),
            "Accept": "application/json",
            "Referer": "https://www.cnbc.com/",
        }

        try:
            resp = requests.get(CNBC_SEARCH_URL, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"[cnbc] Search API failed for {ticker}: {e}")
            return []

        items = data.get("items", [])
        if not items:
            logger.warning(f"[cnbc] No results for {ticker}")
            return []

        fetch_headers = self._get_headers()
        articles = []
        for item in items[:12]:
            headline = item.get("cn:title", "") or item.get("title", "")
            headline = headline.strip()
            url = item.get("link", "") or item.get("url", "")
            url = url.strip()
            if not headline or not url:
                continue
            if not url.startswith("http"):
                url = CNBC_BASE + url

            # Parse ISO date if present
            pub_str = item.get("pubdate", "") or item.get("cn:pubdate", "")
            published_at = None
            if pub_str:
                try:
                    published_at = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                except ValueError:
                    pass

            self._sleep()
            body = _get_article_body(url, fetch_headers)

            articles.append(
                ScrapedArticle(
                    ticker=ticker,
                    headline=headline[:300],
                    url=url,
                    source="cnbc",
                    body=body,
                    published_at=published_at,
                )
            )

        logger.info(f"[cnbc] {len(articles)} articles for {ticker}")
        return articles
