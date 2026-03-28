"""MarketWatch scraper — Playwright + pre-paywall snippet extraction."""
from bs4 import BeautifulSoup
from loguru import logger
from playwright.sync_api import sync_playwright

from src.scrapers.base_scraper import BaseScraper, ScrapedArticle
from src.scrapers.user_agents import random_user_agent

MARKETWATCH_SEARCH = "https://www.marketwatch.com/search?q={ticker}&m=article&rpp=15&mp=806&bd=false&rs=true"


class MarketWatchScraper(BaseScraper):
    source_name = "marketwatch"

    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        url = MARKETWATCH_SEARCH.format(ticker=ticker)
        articles = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=random_user_agent())
            page = context.new_page()

            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
                self._sleep()

                soup = BeautifulSoup(page.content(), "lxml")
                results = soup.select(".article__headline a, .searchresult a, h3.article__headline a")
                seen_urls = set()

                for a_tag in results[:12]:
                    href = a_tag.get("href", "")
                    if not href or href in seen_urls:
                        continue
                    if not href.startswith("http"):
                        href = f"https://www.marketwatch.com{href}"
                    seen_urls.add(href)

                    headline = a_tag.get_text(strip=True)
                    if not headline:
                        continue

                    snippet = self._fetch_snippet(page, href)

                    articles.append(
                        ScrapedArticle(
                            ticker=ticker,
                            headline=headline,
                            url=href,
                            source="marketwatch",
                            snippet=snippet,
                        )
                    )

            finally:
                browser.close()

        return articles

    def _fetch_snippet(self, page, url: str) -> str | None:
        """Extract pre-paywall snippet — MarketWatch shows ~3 paragraphs before cutting off."""
        try:
            self._sleep()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            soup = BeautifulSoup(page.content(), "lxml")

            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()

            # Get all visible paragraphs before paywall wall
            paragraphs = soup.select(".article__body p, .paywall p, .full-story p")
            if not paragraphs:
                paragraphs = soup.find_all("p")

            texts = []
            for p in paragraphs[:6]:  # first 6 paragraphs (pre-paywall)
                text = p.get_text(strip=True)
                if len(text) > 30:
                    texts.append(text)

            return " ".join(texts)[:1500] if texts else None
        except Exception as e:
            logger.debug(f"MarketWatch snippet fetch failed {url}: {e}")
            return None
