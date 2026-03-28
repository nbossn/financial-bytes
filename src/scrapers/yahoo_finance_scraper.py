"""Yahoo Finance scraper using Playwright for JS-rendered news."""
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from loguru import logger
from playwright.sync_api import sync_playwright

from src.scrapers.base_scraper import BaseScraper, ScrapedArticle
from src.scrapers.user_agents import random_user_agent

YAHOO_NEWS_URL = "https://finance.yahoo.com/quote/{ticker}/news/"


class YahooFinanceScraper(BaseScraper):
    source_name = "yahoo_finance"

    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        url = YAHOO_NEWS_URL.format(ticker=ticker)
        articles = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=random_user_agent())
            page = context.new_page()

            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
                self._sleep()

                # Scroll to load more articles
                page.evaluate("window.scrollBy(0, 1500)")
                self._sleep()

                soup = BeautifulSoup(page.content(), "lxml")

                # Yahoo Finance news feed article cards
                article_links = soup.select("a[href*='/news/']") + soup.select("a[data-ylk*='sec:lead']")
                seen_urls = set()

                for a_tag in article_links[:20]:
                    href = a_tag.get("href", "")
                    if not href or href in seen_urls:
                        continue
                    if not href.startswith("http"):
                        href = f"https://finance.yahoo.com{href}"
                    seen_urls.add(href)

                    headline = a_tag.get_text(strip=True)
                    if not headline or len(headline) < 10:
                        continue

                    # Try to get article body
                    body = self._fetch_article(page, href)

                    articles.append(
                        ScrapedArticle(
                            ticker=ticker,
                            headline=headline,
                            url=href,
                            source="yahoo_finance",
                            body=body,
                        )
                    )

            finally:
                browser.close()

        return articles

    def _fetch_article(self, page, url: str) -> str | None:
        """Navigate to article and extract body text."""
        try:
            self._sleep()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)

            soup = BeautifulSoup(page.content(), "lxml")

            # Remove junk
            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()

            for selector in [".caas-body", "article", ".article-body", ".body", "[data-testid='article-body']"]:
                el = soup.select_one(selector)
                if el:
                    text = el.get_text(separator=" ", strip=True)
                    if len(text) > 100:
                        return text[:3000]

            # Video transcript fallback — look for transcript container
            transcript = soup.select_one(".transcript, .video-transcript, [data-testid='transcript']")
            if transcript:
                return transcript.get_text(separator=" ", strip=True)[:3000]

            return None
        except Exception as e:
            logger.debug(f"Yahoo article fetch failed {url}: {e}")
            return None
