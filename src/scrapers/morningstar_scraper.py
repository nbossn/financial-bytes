"""Morningstar scraper using requests + BeautifulSoup."""
import requests
from bs4 import BeautifulSoup
from loguru import logger

from src.scrapers.base_scraper import BaseScraper, ScrapedArticle

MORNINGSTAR_NEWS_URL = "https://www.morningstar.com/stocks/{exchange}/{ticker}/news"
MORNINGSTAR_SEARCH = "https://www.morningstar.com/search?query={ticker}&contentType=article"

EXCHANGE_MAP = {
    "MSFT": "xnas", "NVDA": "xnas", "AAPL": "xnas", "GOOGL": "xnas",
    "AMZN": "xnas", "META": "xnas", "TSLA": "xnas", "AMD": "xnas",
    "JPM": "xnys", "BAC": "xnys", "GS": "xnys", "WMT": "xnys",
}


class MorningstarScraper(BaseScraper):
    source_name = "morningstar"

    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        exchange = EXCHANGE_MAP.get(ticker, "xnas")
        url = MORNINGSTAR_NEWS_URL.format(exchange=exchange, ticker=ticker.lower())
        articles = []
        headers = self._get_headers()

        try:
            self._sleep()
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            article_links = soup.select("a[href*='/articles/'], a[href*='/news/'], .mdc-news__headline a")
            seen_urls = set()

            for a_tag in article_links[:10]:
                href = a_tag.get("href", "")
                if not href or href in seen_urls:
                    continue
                if not href.startswith("http"):
                    href = f"https://www.morningstar.com{href}"
                seen_urls.add(href)

                headline = a_tag.get_text(strip=True)
                if not headline or len(headline) < 10:
                    continue

                self._sleep()
                body = self._fetch_article(href, headers)

                articles.append(
                    ScrapedArticle(
                        ticker=ticker,
                        headline=headline,
                        url=href,
                        source="morningstar",
                        body=body,
                    )
                )

        except Exception as e:
            logger.warning(f"[morningstar] {ticker}: {e}")

        return articles

    def _fetch_article(self, url: str, headers: dict) -> str | None:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()

            for selector in [".article__body", ".mdc-article__body", "article", ".story__content"]:
                el = soup.select_one(selector)
                if el:
                    text = el.get_text(separator=" ", strip=True)
                    if len(text) > 100:
                        return text[:3000]
            return None
        except Exception:
            return None
