"""Seeking Alpha scraper — free tier headlines and snippets."""
import requests
from bs4 import BeautifulSoup
from loguru import logger

from src.scrapers.base_scraper import BaseScraper, ScrapedArticle

SEEKING_ALPHA_URL = "https://seekingalpha.com/symbol/{ticker}/news"


class SeekingAlphaScraper(BaseScraper):
    source_name = "seeking_alpha"

    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        url = SEEKING_ALPHA_URL.format(ticker=ticker)
        articles = []
        headers = self._get_headers()
        # Seeking Alpha is JS-heavy; use their public news API endpoint
        api_url = f"https://seekingalpha.com/api/v3/symbol_data/news?filter[symbol]={ticker}&filter[category]=all&page[size]=10&page[number]=1"

        try:
            self._sleep()
            resp = requests.get(api_url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("data", []):
                attrs = item.get("attributes", {})
                headline = attrs.get("title", "")
                slug = attrs.get("slug", "")
                article_url = f"https://seekingalpha.com/article/{item.get('id')}-{slug}" if slug else url
                summary = attrs.get("summary") or attrs.get("headline_summary", "")

                if not headline:
                    continue

                articles.append(
                    ScrapedArticle(
                        ticker=ticker,
                        headline=headline,
                        url=article_url,
                        source="seeking_alpha",
                        snippet=summary[:500] if summary else None,
                    )
                )

        except Exception as e:
            logger.warning(f"[seeking_alpha] {ticker}: {e}")

        return articles
