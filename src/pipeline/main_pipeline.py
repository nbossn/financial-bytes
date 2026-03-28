"""Main pipeline — runs the full financial-bytes workflow end-to-end."""
from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from loguru import logger

from src.config import settings
from src.portfolio.reader import read_portfolio, save_portfolio_to_db
from src.portfolio.models import PortfolioSnapshot


def _fetch_signals_for_ticker(ticker: str):
    """Fetch massive.com signals in a worker thread (each thread owns its client)."""
    try:
        from decimal import Decimal
        from src.api.massive_client import MassiveClient
        from src.api.endpoints import MassiveEndpoints

        with MassiveClient() as client:
            endpoints = MassiveEndpoints(client)
            signals = endpoints.get_ticker_signals(ticker)
            price = (
                Decimal(str(signals.quote.current_price))
                if signals and signals.quote and signals.quote.current_price
                else None
            )
            return ticker, signals, price
    except Exception as e:
        logger.warning(f"Signals fetch failed for {ticker}: {e}")
        return ticker, None, None


def run_pipeline(
    portfolio_csv: str | None = None,
    report_date: date | None = None,
    skip_scrape: bool = False,
    skip_email: bool = False,
    output_dir: Path | None = None,
    portfolio_name: str = "default",
    portfolio_label: str | None = None,
    email_recipients: list[str] | None = None,
) -> dict:
    """Full pipeline: portfolio → scrape → analyse → direct → newsletter → email.

    Performance notes (15+ tickers):
    - Phase 2 (scrape): parallel across tickers, bounded by MAX_PARALLEL_TICKERS
    - Phase 3 (signals): parallel across tickers + parallel within each ticker (4 HTTP calls)
    - Phase 4 (analysts): all tickers run concurrently via asyncio.gather, bounded by MAX_PARALLEL_ANALYSTS
    """
    today = report_date or date.today()
    csv_path = portfolio_csv or settings.portfolio_csv_path
    out = output_dir or Path("newsletters")
    label = portfolio_label or portfolio_name.replace("_", " ").title()
    t0 = time.monotonic()

    logger.info("=" * 60)
    logger.info(f"Financial Bytes pipeline starting — {today} [{portfolio_name}]")
    logger.info("=" * 60)

    # ── Phase 1: Portfolio ─────────────────────────────────────────
    logger.info("[1/5] Loading portfolio...")
    holdings = read_portfolio(csv_path)
    save_portfolio_to_db(holdings, portfolio_name=portfolio_name)
    snapshot = PortfolioSnapshot(holdings=holdings)
    logger.info(f"      {len(holdings)} holding(s) loaded — value ${snapshot.total_value:,.2f}")

    # ── Phase 2: Scrape ────────────────────────────────────────────
    all_articles: dict[str, list] = {}
    if not skip_scrape:
        logger.info(
            f"[2/5] Scraping news articles "
            f"(parallel workers: {settings.max_parallel_tickers})..."
        )
        from src.scrapers.scraper_orchestrator import scrape_tickers_parallel

        tickers = [h.ticker for h in holdings]
        all_articles = scrape_tickers_parallel(tickers, max_workers=settings.max_parallel_tickers)
        for ticker, arts in all_articles.items():
            logger.info(f"      {ticker}: {len(arts)} article(s)")
    else:
        logger.info("[2/5] Scraping skipped (--skip-scrape flag)")
        from src.db.models import Article
        from src.db.session import get_db
        from src.scrapers.base_scraper import ScrapedArticle
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.article_lookback_hours)
        with get_db() as db:
            for holding in holdings:
                rows = (
                    db.query(Article)
                    .filter(
                        Article.ticker == holding.ticker,
                        Article.scraped_at >= cutoff,
                    )
                    .order_by(Article.published_at.desc())
                    .limit(settings.max_articles_per_ticker)
                    .all()
                )
                all_articles[holding.ticker] = [
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

    # ── Phase 3: massive.com signals (parallel across tickers) ────
    logger.info("[3/5] Fetching market signals (parallel across tickers)...")
    ticker_signals: dict[str, object] = {}
    market_prices: dict[str, "Decimal"] = {}

    signal_workers = min(len(holdings), 10)
    with ThreadPoolExecutor(max_workers=signal_workers) as pool:
        future_map = {
            pool.submit(_fetch_signals_for_ticker, h.ticker): h.ticker
            for h in holdings
        }
        for future in as_completed(future_map):
            ticker, signals, price = future.result()
            if signals is not None:
                ticker_signals[ticker] = signals
            if price is not None:
                market_prices[ticker] = price
            logger.info(f"      {ticker}: {'signals fetched' if signals else 'signals unavailable'}")

    if market_prices:
        snapshot = PortfolioSnapshot(holdings=holdings, prices=market_prices, as_of=today)

    # ── Phase 4: Analyst agents (concurrent via asyncio) ──────────
    logger.info(
        f"[4/5] Running analyst agents "
        f"(max concurrent: {settings.max_parallel_analysts})..."
    )
    from src.agents.analyst_agent import run_analysts_parallel

    analyst_reports = asyncio.run(
        run_analysts_parallel(
            holdings=holdings,
            all_articles=all_articles,
            ticker_signals=ticker_signals,
            report_date=today,
            max_concurrent=settings.max_parallel_analysts,
            portfolio_name=portfolio_name,
        )
    )
    for report in analyst_reports:
        logger.info(
            f"      {report.ticker}: {report.recommendation} ({report.confidence:.0%} confidence)"
        )

    # ── Phase 5: Director agent ────────────────────────────────────
    logger.info("[5/5] Director synthesis...")
    from src.agents.director_agent import synthesize_portfolio

    director_report = synthesize_portfolio(snapshot, analyst_reports, report_date=today, portfolio_name=portfolio_name)
    logger.info(f"      Theme: {director_report.market_theme[:80]}")
    logger.info(f"      Sentiment: {director_report.overall_sentiment:+.2f}")

    # ── Newsletter generation ──────────────────────────────────────
    logger.info("[6/6] Generating newsletter...")
    from src.newsletter.generator import generate

    paths = generate(
        report=director_report,
        analyst_reports=analyst_reports,
        snapshot=snapshot,
        report_date=today,
        output_dir=out,
        portfolio_name=portfolio_name,
        portfolio_label=label,
    )

    # ── Email delivery ─────────────────────────────────────────────
    if not skip_email:
        from src.delivery.email_sender import send_newsletter

        html_path = paths.get("html")
        md_path = paths.get("md")
        pdf_path = paths.get("pdf")
        if html_path and html_path.exists():
            send_newsletter(
                report_date=today,
                html_content=html_path.read_text(encoding="utf-8"),
                markdown_content=md_path.read_text(encoding="utf-8") if md_path and md_path.exists() else "",
                pdf_path=pdf_path,
                market_theme=director_report.market_theme,
                recipients=email_recipients or None,
            )
    else:
        logger.info("Email delivery skipped (--skip-email flag)")

    elapsed = time.monotonic() - t0
    logger.info(f"Pipeline complete in {elapsed:.1f}s")
    logger.info("=" * 60)

    return {
        "status": "ok",
        "report_date": today,
        "paths": paths,
        "director_report": director_report,
        "analyst_reports": analyst_reports,
    }
