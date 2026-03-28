"""Abstract base scraper with shared utilities."""
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger

from src.config import settings
from src.scrapers.user_agents import random_user_agent


@dataclass
class ScrapedArticle:
    ticker: str
    headline: str
    url: str
    source: str
    body: str | None = None
    snippet: str | None = None
    published_at: datetime | None = None

    @property
    def has_content(self) -> bool:
        return bool(self.body or self.snippet)

    @property
    def content(self) -> str:
        """Return best available content."""
        return self.body or self.snippet or ""


@dataclass
class ScrapeResult:
    source: str
    ticker: str
    articles: list[ScrapedArticle] = field(default_factory=list)
    success: bool = True
    error: str | None = None
    duration_ms: int = 0


class BaseScraper(ABC):
    source_name: str = "unknown"

    def __init__(self):
        self.delay_min = settings.scraper_delay_min
        self.delay_max = settings.scraper_delay_max

    def _sleep(self):
        """Random delay to avoid rate limiting."""
        delay = random.uniform(self.delay_min, self.delay_max)
        time.sleep(delay)

    def _get_headers(self) -> dict:
        return {
            "User-Agent": random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
        }

    def scrape(self, ticker: str) -> ScrapeResult:
        """Public interface — wraps _scrape with error handling and timing."""
        start = time.time()
        try:
            articles = self._scrape(ticker)
            duration = int((time.time() - start) * 1000)
            logger.info(f"[{self.source_name}] {ticker}: {len(articles)} articles ({duration}ms)")
            return ScrapeResult(
                source=self.source_name,
                ticker=ticker,
                articles=articles,
                success=True,
                duration_ms=duration,
            )
        except Exception as e:
            duration = int((time.time() - start) * 1000)
            logger.warning(f"[{self.source_name}] {ticker}: FAILED — {e}")
            return ScrapeResult(
                source=self.source_name,
                ticker=ticker,
                success=False,
                error=str(e),
                duration_ms=duration,
            )

    @abstractmethod
    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        """Implement per-source scraping logic."""
        raise NotImplementedError
