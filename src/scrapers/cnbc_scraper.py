"""CNBC scraper using Playwright."""
from bs4 import BeautifulSoup
from loguru import logger
from playwright.sync_api import sync_playwright

from src.scrapers.base_scraper import BaseScraper, ScrapedArticle
from src.scrapers.user_agents import random_user_agent

CNBC_SEARCH_URL = "https://www.cnbc.com/search/?query={ticker}&qsearchterm={ticker}"


class CNBCScraper(BaseScraper):
    source_name = "cnbc"

    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        url = CNBC_SEARCH_URL.format(ticker=ticker)
        articles = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=random_user_agent())
            page = context.new_page()

            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
                self._sleep()

                soup = BeautifulSoup(page.content(), "lxml")

                # CNBC search result cards
                cards = soup.select(".SearchResult-searchResultContent, .Card-titleContainer, .resultlink")
                seen_urls = set()

                for card in cards[:15]:
                    a_tag = card.find("a") if not card.name == "a" else card
                    if not a_tag:
                        continue

                    href = a_tag.get("href", "")
                    if not href or href in seen_urls or "cnbc.com" not in href:
                        continue
                    seen_urls.add(href)

                    headline = a_tag.get_text(strip=True) or card.get_text(strip=True)
                    if not headline or len(headline) < 10:
                        continue

                    body = self._fetch_article(page, href)

                    articles.append(
                        ScrapedArticle(
                            ticker=ticker,
                            headline=headline[:300],
                            url=href,
                            source="cnbc",
                            body=body,
                        )
                    )

            finally:
                browser.close()

        return articles

    def _fetch_article(self, page, url: str) -> str | None:
        try:
            self._sleep()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            soup = BeautifulSoup(page.content(), "lxml")

            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()

            for selector in [".ArticleBody-articleBody", ".group", "article .content", ".InlineImage-imageEmbed ~ div"]:
                el = soup.select_one(selector)
                if el:
                    text = el.get_text(separator=" ", strip=True)
                    if len(text) > 100:
                        return text[:3000]
            return None
        except Exception as e:
            logger.debug(f"CNBC article fetch failed {url}: {e}")
            return None
