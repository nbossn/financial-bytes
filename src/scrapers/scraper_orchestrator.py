"""Orchestrates all scrapers per ticker, deduplicates, and stores results."""
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.exc import IntegrityError

from src.config import settings
from src.db.models import Article, ScrapeLog
from src.db.session import get_db
from src.scrapers.base_scraper import ScrapedArticle, ScrapeResult
from src.scrapers.cnbc_scraper import CNBCScraper
from src.scrapers.finviz_scraper import FinvizScraper
from src.scrapers.google_news_scraper import GoogleNewsScraper
from src.scrapers.marketwatch_scraper import MarketWatchScraper
from src.scrapers.morningstar_scraper import MorningstarScraper
from src.scrapers.web_search_fallback import WebSearchFallback
from src.scrapers.yahoo_finance_scraper import YahooFinanceScraper

MIN_ARTICLES_BEFORE_FALLBACK = 3


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _deduplicate(articles: list[ScrapedArticle]) -> list[ScrapedArticle]:
    """Remove duplicate articles by URL and headline similarity."""
    seen_urls = set()
    seen_headlines = set()
    unique = []

    for article in articles:
        url_h = _url_hash(article.url)
        # Normalize headline for similarity check (lowercase, no punctuation)
        headline_key = "".join(c.lower() for c in article.headline if c.isalnum() or c.isspace())[:60]

        if url_h in seen_urls or headline_key in seen_headlines:
            continue

        seen_urls.add(url_h)
        seen_headlines.add(headline_key)
        unique.append(article)

    return unique


def _sort_by_recency_and_quality(articles: list[ScrapedArticle]) -> list[ScrapedArticle]:
    """Sort by: has body > has snippet > recency."""
    def score(a: ScrapedArticle) -> tuple:
        has_body = 1 if a.body and len(a.body) > 100 else 0
        has_snippet = 1 if a.snippet else 0
        ts = a.published_at.timestamp() if a.published_at else 0
        return (has_body, has_snippet, ts)

    return sorted(articles, key=score, reverse=True)


def _save_articles(ticker: str, articles: list[ScrapedArticle]) -> int:
    """Persist articles to database, skip duplicates (unique URL constraint)."""
    saved = 0
    with get_db() as db:
        for article in articles:
            try:
                db.add(
                    Article(
                        ticker=ticker,
                        headline=article.headline[:500],
                        url=article.url,
                        source=article.source,
                        body=article.body,
                        snippet=article.snippet,
                        published_at=article.published_at,
                    )
                )
                db.flush()
                saved += 1
            except IntegrityError:
                db.rollback()  # URL already exists — skip
    logger.info(f"Saved {saved}/{len(articles)} new articles for {ticker}")
    return saved


def _log_scrape(ticker: str, result: ScrapeResult) -> None:
    with get_db() as db:
        db.add(
            ScrapeLog(
                ticker=ticker,
                source=result.source,
                articles_found=len(result.articles),
                success=result.success,
                error_message=result.error,
                duration_ms=result.duration_ms,
            )
        )


def _load_cached_articles(ticker: str) -> list[ScrapedArticle] | None:
    """Return cached articles if ticker was already scraped today, else None."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.article_lookback_hours)
    with get_db() as db:
        rows = (
            db.query(Article)
            .filter(Article.ticker == ticker, Article.scraped_at >= cutoff)
            .order_by(Article.published_at.desc())
            .limit(settings.max_articles_per_ticker)
            .all()
        )
        if not rows:
            return None
        logger.info(f"{ticker}: using {len(rows)} cached article(s) (scraped within lookback window)")
        return [
            ScrapedArticle(
                ticker=r.ticker,
                headline=r.headline,
                url=r.url,
                source=r.source,
                body=r.body,
                snippet=r.snippet,
                published_at=r.published_at,
            )
            for r in rows
        ]


def scrape_ticker(ticker: str, skip_cached: bool = True) -> list[ScrapedArticle]:
    """Run all scrapers for one ticker, deduplicate, cap at max_articles, and persist.

    If skip_cached=True (default) and the ticker already has articles in DB within
    the lookback window, return those without re-scraping. This prevents redundant
    scraping when the same ticker appears across multiple portfolios on the same day.
    """
    if skip_cached:
        cached = _load_cached_articles(ticker)
        if cached is not None:
            return cached

    logger.info(f"=== Scraping {ticker} ===")
    max_articles = settings.max_articles_per_ticker

    # Scrapers to run (Finviz first as primary link source)
    scrapers = [
        FinvizScraper(),
        YahooFinanceScraper(),
        CNBCScraper(),
        MarketWatchScraper(),
        MorningstarScraper(),
        GoogleNewsScraper(),
    ]

    all_articles: list[ScrapedArticle] = []

    # Run scrapers — Finviz sequentially (Selenium), others can be parallel
    finviz_result = scrapers[0].scrape(ticker)
    _log_scrape(ticker, finviz_result)
    all_articles.extend(finviz_result.articles)

    # Run remaining scrapers in parallel (max 3 concurrent to avoid overwhelming)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(s.scrape, ticker): s for s in scrapers[1:]}
        for future in as_completed(futures):
            result = future.result()
            _log_scrape(ticker, result)
            all_articles.extend(result.articles)

    # Deduplicate and sort
    unique = _deduplicate(all_articles)
    sorted_articles = _sort_by_recency_and_quality(unique)

    # Trigger web search fallback if insufficient coverage
    if len(sorted_articles) < MIN_ARTICLES_BEFORE_FALLBACK:
        logger.warning(f"{ticker}: only {len(sorted_articles)} articles — triggering web search fallback")
        fallback = WebSearchFallback()
        fallback_result = fallback.scrape(ticker)
        _log_scrape(ticker, fallback_result)
        all_articles.extend(fallback_result.articles)
        unique = _deduplicate(all_articles)
        sorted_articles = _sort_by_recency_and_quality(unique)

    # Cap at max articles
    final_articles = sorted_articles[:max_articles]
    logger.info(f"{ticker}: {len(final_articles)} articles selected (from {len(all_articles)} total scraped)")

    # Persist to DB
    _save_articles(ticker, final_articles)

    return final_articles


def scrape_all_tickers(tickers: list[str]) -> dict[str, list[ScrapedArticle]]:
    """Scrape all tickers sequentially (respects Selenium/browser resource limits)."""
    results = {}
    for ticker in tickers:
        results[ticker] = scrape_ticker(ticker)
    return results


def scrape_tickers_parallel(
    tickers: list[str],
    max_workers: int = 3,
    skip_cached: bool = True,
) -> dict[str, list[ScrapedArticle]]:
    """Scrape multiple tickers concurrently.

    max_workers controls how many browser (Selenium) instances run at once.
    Values above 3 risk RAM exhaustion on typical VPS hardware.

    skip_cached=True (default) skips re-scraping tickers that already have
    fresh articles in the DB — shared across portfolios on the same day.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: dict[str, list[ScrapedArticle]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(scrape_ticker, t, skip_cached): t for t in tickers}
        for future in as_completed(future_map):
            ticker = future_map[future]
            try:
                results[ticker] = future.result()
            except Exception as e:
                logger.error(f"Scrape failed for {ticker}: {e}")
                results[ticker] = []
    return results
