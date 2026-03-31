"""Morningstar scraper using requests + BeautifulSoup."""
import requests
from bs4 import BeautifulSoup
from loguru import logger

from src.scrapers._utils import is_safe_url
from src.scrapers.base_scraper import BaseScraper, ScrapedArticle

MORNINGSTAR_STOCK_URL = "https://www.morningstar.com/stocks/{exchange}/{ticker}/news"
MORNINGSTAR_ETF_URL = "https://www.morningstar.com/etfs/{exchange}/{ticker}/news"

EXCHANGE_MAP = {
    # Stocks — XNAS (Nasdaq), XNYS (NYSE)
    "MSFT": "xnas", "NVDA": "xnas", "AAPL": "xnas", "GOOGL": "xnas",
    "GOOG": "xnas", "AMZN": "xnas", "META": "xnas", "TSLA": "xnas",
    "AMD": "xnas", "COIN": "xnas", "AVGO": "xnas",
    "JPM": "xnys", "BAC": "xnys", "GS": "xnys", "WMT": "xnys",
    "FIG": "xnas", "VST": "xnys",
}

# ETF tickers — use /etfs/ path
ETF_TICKERS = {"QQQ", "VOO", "VOOG", "SPY", "IWM", "DIA", "GLD", "SLV", "TLT", "HYG"}
ETF_EXCHANGE_MAP = {
    "QQQ": "xnas",
    "VOO": "arcx", "VOOG": "arcx", "SPY": "arcx",
    "IWM": "arcx", "DIA": "arcx", "GLD": "arcx",
    "SLV": "arcx", "TLT": "xnas", "HYG": "arcx",
}


class MorningstarScraper(BaseScraper):
    source_name = "morningstar"

    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        if ticker in ETF_TICKERS:
            exchange = ETF_EXCHANGE_MAP.get(ticker, "arcx")
            url = MORNINGSTAR_ETF_URL.format(exchange=exchange, ticker=ticker.lower())
        else:
            exchange = EXCHANGE_MAP.get(ticker, "xnas")
            url = MORNINGSTAR_STOCK_URL.format(exchange=exchange, ticker=ticker.lower())

        articles = []
        headers = self._get_headers()

        try:
            self._sleep()
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            article_links = soup.select(
                "a[href*='/articles/'], a[href*='/news/'], .mdc-news__headline a, "
                ".mdc-article-list__headline a"
            )
            seen_urls: set[str] = set()

            for a_tag in article_links[:10]:
                href = a_tag.get("href", "").strip()
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

        logger.info(f"[morningstar] {len(articles)} articles for {ticker}")
        return articles

    def _fetch_article(self, url: str, headers: dict) -> str | None:
        if not is_safe_url(url):
            logger.warning(f"[morningstar] Blocked unsafe URL: {url[:80]}")
            return None
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
