"""Finviz scraper using Selenium for JS rendering + BeautifulSoup for parsing."""
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from loguru import logger
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from src.scrapers._utils import is_safe_url
from src.scrapers.base_scraper import BaseScraper, ScrapedArticle
from src.scrapers.user_agents import random_user_agent

FINVIZ_BASE = "https://finviz.com"
FINVIZ_QUOTE_URL = "https://finviz.com/quote.ashx?t={ticker}&p=d"


def _build_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(f"--user-agent={random_user_agent()}")
    options.add_argument("--window-size=1920,1080")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


def _extract_article_text(url: str, headers: dict) -> str | None:
    """Fetch article URL and extract body text."""
    if not is_safe_url(url):
        logger.warning(f"[finviz] Blocked unsafe URL: {url[:80]}")
        return None
    try:
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Remove nav, ads, scripts
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "iframe"]):
            tag.decompose()

        # Try common article body selectors
        for selector in [
            "article", ".article-body", ".article__body", "#article-body",
            ".story-body", ".entry-content", ".post-content", "main",
        ]:
            element = soup.select_one(selector)
            if element:
                text = element.get_text(separator=" ", strip=True)
                if len(text) > 200:
                    return text[:3000]  # cap at 3000 chars

        # Fallback: all paragraphs
        paragraphs = soup.find_all("p")
        text = " ".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 50)
        return text[:3000] if text else None
    except Exception as e:
        logger.debug(f"Could not extract text from {url}: {e}")
        return None


class FinvizScraper(BaseScraper):
    source_name = "finviz"

    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        url = FINVIZ_QUOTE_URL.format(ticker=ticker)
        driver = None
        articles = []

        try:
            driver = _build_driver()
            driver.get(url)

            # Wait for news table to load
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table.fullview-news-outer, .news-table"))
            )
            self._sleep()

            soup = BeautifulSoup(driver.page_source, "lxml")

            # Finviz news table selectors
            news_table = soup.find("table", class_="fullview-news-outer") or soup.find(class_="news-table")
            if not news_table:
                logger.warning(f"[finviz] No news table found for {ticker}")
                return []

            headers = self._get_headers()
            rows = news_table.find_all("tr")

            for row in rows[:20]:  # top 20 links max
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue

                link_cell = cells[-1]
                a_tag = link_cell.find("a")
                if not a_tag or not a_tag.get("href"):
                    continue

                article_url = a_tag["href"]
                if not article_url.startswith("http"):
                    article_url = urljoin(FINVIZ_BASE, article_url)

                headline = a_tag.get_text(strip=True)
                source = link_cell.find("span") or link_cell.find(class_="news-link-source")
                source_name = source.get_text(strip=True) if source else "finviz"

                # Parse timestamp from first cell
                time_cell = cells[0].get_text(strip=True) if len(cells) > 1 else ""
                published_at = _parse_finviz_time(time_cell)

                self._sleep()
                body = _extract_article_text(article_url, headers)

                articles.append(
                    ScrapedArticle(
                        ticker=ticker,
                        headline=headline,
                        url=article_url,
                        source=source_name or "finviz",
                        body=body,
                        published_at=published_at,
                    )
                )

        finally:
            if driver:
                driver.quit()

        return articles


def _parse_finviz_time(time_str: str) -> datetime | None:
    """Parse Finviz time strings like 'Mar-27-26 08:30AM' or 'Today 08:30AM'."""
    if not time_str:
        return None
    try:
        now = datetime.now(timezone.utc)
        if "today" in time_str.lower():
            time_part = time_str.lower().replace("today", "").strip()
            return datetime.strptime(f"{now.strftime('%Y-%m-%d')} {time_part}", "%Y-%m-%d %I:%M%p").replace(tzinfo=timezone.utc)
        return datetime.strptime(time_str, "%b-%d-%y %I:%M%p").replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None
