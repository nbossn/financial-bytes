"""Google News RSS scraper — aggregates from hundreds of sources, no auth required."""
import defusedxml.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from loguru import logger

from src.scrapers.base_scraper import BaseScraper, ScrapedArticle
from src.scrapers.user_agents import random_user_agent

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"

# Map tickers to company names for better search precision
COMPANY_NAME_MAP = {
    "GOOG": "Alphabet Google",
    "GOOGL": "Alphabet Google",
    "META": "Meta Platforms Facebook",
    "MSFT": "Microsoft",
    "AAPL": "Apple",
    "AMZN": "Amazon",
    "TSLA": "Tesla",
    "NVDA": "Nvidia",
    "AMD": "AMD Advanced Micro Devices",
    "JPM": "JPMorgan Chase",
    "GS": "Goldman Sachs",
    "BAC": "Bank of America",
    "WMT": "Walmart",
    "COIN": "Coinbase",
    "AVGO": "Broadcom",
    "FIG": "Portman Ridge Finance",
    "AJINY": "Ajinomoto ADR",
    "VST": "Vistra Energy",
    "QQQ": "QQQ Nasdaq ETF",
    "VOO": "VOO S&P 500 ETF",
    "VOOG": "VOOG S&P 500 Growth ETF",
}


def _parse_rss_date(date_str: str) -> datetime | None:
    """Parse RFC 2822 date string from RSS."""
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str).replace(tzinfo=timezone.utc)
    except Exception:
        return None


class GoogleNewsScraper(BaseScraper):
    source_name = "google_news"

    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        # Use company name for ADRs and tickers not commonly found alone
        company = COMPANY_NAME_MAP.get(ticker, ticker)
        query = f"{company} stock"
        params = {
            "q": query,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
        headers = {
            "User-Agent": random_user_agent(),
            "Accept": "application/rss+xml, application/xml, text/xml",
        }

        try:
            resp = requests.get(
                GOOGLE_NEWS_RSS, params=params, headers=headers, timeout=20
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as e:
            logger.warning(f"[google_news] RSS fetch failed for {ticker}: {e}")
            return []

        ns = {"media": "http://search.yahoo.com/mrss/"}
        channel = root.find("channel")
        if channel is None:
            return []

        articles = []
        for item in channel.findall("item")[:20]:
            title_el = item.find("title")
            link_el = item.find("link")
            pub_el = item.find("pubDate")
            source_el = item.find("source")

            title = (title_el.text or "").strip() if title_el is not None else ""
            link = (link_el.text or "").strip() if link_el is not None else ""
            pub_date = (pub_el.text or "").strip() if pub_el is not None else ""
            source_name = (
                source_el.text.strip()
                if source_el is not None and source_el.text
                else "google_news"
            )

            if not title or not link:
                continue
            if not link.startswith("http"):
                continue

            published_at = _parse_rss_date(pub_date)

            articles.append(
                ScrapedArticle(
                    ticker=ticker,
                    headline=title,
                    url=link,
                    source=source_name,
                    published_at=published_at,
                )
            )

        logger.info(f"[google_news] {len(articles)} articles for {ticker}")
        return articles
