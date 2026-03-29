"""Reuters scraper — replaces Bloomberg/Barrons (open access)."""
import requests
from bs4 import BeautifulSoup
from loguru import logger

from src.scrapers._utils import is_safe_url
from src.scrapers.base_scraper import BaseScraper, ScrapedArticle

REUTERS_SEARCH = "https://www.reuters.com/site-search/?query={ticker}&section=markets&offset=0"


class ReutersScraper(BaseScraper):
    source_name = "reuters"

    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        url = REUTERS_SEARCH.format(ticker=ticker)
        articles = []
        headers = self._get_headers()

        try:
            self._sleep()
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            results = soup.select('[data-testid="Heading"] a, .search-results__item a, h3 a')
            seen_urls = set()

            for a_tag in results[:10]:
                href = a_tag.get("href", "")
                if not href or href in seen_urls:
                    continue
                if not href.startswith("http"):
                    href = f"https://www.reuters.com{href}"
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
                        source="reuters",
                        body=body,
                    )
                )

        except Exception as e:
            logger.warning(f"[reuters] {ticker}: {e}")

        return articles

    def _fetch_article(self, url: str, headers: dict) -> str | None:
        if not is_safe_url(url):
            logger.warning(f"[reuters] Blocked unsafe URL: {url[:80]}")
            return None
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()

            for selector in ["[data-testid='paragraph']", ".article-body__content", ".StandardArticleBody_body", "article"]:
                elements = soup.select(selector)
                if elements:
                    text = " ".join(el.get_text(separator=" ", strip=True) for el in elements)
                    if len(text) > 100:
                        return text[:3000]
            return None
        except Exception:
            return None
