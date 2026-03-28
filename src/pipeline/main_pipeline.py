"""Main pipeline — runs the full financial-bytes workflow end-to-end."""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from loguru import logger

from src.config import settings
from src.portfolio.reader import read_portfolio, save_portfolio_to_db
from src.portfolio.models import PortfolioSnapshot


def run_pipeline(
    portfolio_csv: str | None = None,
    report_date: date | None = None,
    skip_scrape: bool = False,
    skip_email: bool = False,
    output_dir: Path | None = None,
) -> dict:
    """Full pipeline: portfolio → scrape → analyse → direct → newsletter → email.

    Returns a dict with {"status", "report_date", "paths", "director_report"}.
    """
    today = report_date or date.today()
    csv_path = portfolio_csv or settings.portfolio_csv_path
    out = output_dir or Path("newsletters")
    t0 = time.monotonic()

    logger.info("=" * 60)
    logger.info(f"Financial Bytes pipeline starting — {today}")
    logger.info("=" * 60)

    # ── Phase 1: Portfolio ─────────────────────────────────────────
    logger.info("[1/5] Loading portfolio...")
    holdings = read_portfolio(csv_path)
    save_portfolio_to_db(holdings)
    snapshot = PortfolioSnapshot(holdings=holdings)
    logger.info(f"      {len(holdings)} holding(s) loaded — value ${snapshot.total_value:,.2f}")

    # ── Phase 2: Scrape ────────────────────────────────────────────
    all_articles: dict[str, list] = {}
    if not skip_scrape:
        logger.info("[2/5] Scraping news articles...")
        from src.scrapers.scraper_orchestrator import scrape_ticker
        for holding in holdings:
            articles = scrape_ticker(holding.ticker)
            all_articles[holding.ticker] = articles
            logger.info(f"      {holding.ticker}: {len(articles)} article(s)")
    else:
        logger.info("[2/5] Scraping skipped (--skip-scrape flag)")
        # Load from DB
        from src.db.models import Article
        from src.db.session import get_db
        from src.scrapers.base_scraper import ScrapedArticle
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(hours=settings.article_lookback_hours)
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

    # ── Phase 3: massive.com signals ──────────────────────────────
    logger.info("[3/5] Fetching market signals (massive.com)...")
    ticker_signals: dict[str, object] = {}
    market_prices: dict[str, "Decimal"] = {}
    try:
        from decimal import Decimal as _Decimal
        from src.api.massive_client import MassiveClient
        from src.api.endpoints import MassiveEndpoints
        with MassiveClient() as client:
            endpoints = MassiveEndpoints(client)
            for holding in holdings:
                signals = endpoints.get_ticker_signals(holding.ticker)
                ticker_signals[holding.ticker] = signals
                if signals and signals.quote and signals.quote.current_price:
                    market_prices[holding.ticker] = _Decimal(str(signals.quote.current_price))
                logger.info(f"      {holding.ticker}: signals fetched")
    except Exception as e:
        logger.warning(f"      massive.com signals unavailable: {e}")

    # Update snapshot with live prices if available
    if market_prices:
        from datetime import date as _date
        snapshot = PortfolioSnapshot(holdings=holdings, prices=market_prices, as_of=today)

    # ── Phase 4: Analyst agents (per ticker) ──────────────────────
    logger.info("[4/5] Running analyst agents...")
    from src.agents.analyst_agent import analyze_ticker
    analyst_reports = []
    for holding in holdings:
        articles = all_articles.get(holding.ticker, [])
        signals = ticker_signals.get(holding.ticker)  # type: ignore[arg-type]
        report = analyze_ticker(holding, articles, signals, report_date=today)
        analyst_reports.append(report)
        logger.info(f"      {holding.ticker}: {report.recommendation} ({report.confidence:.0%} confidence)")

    # ── Phase 5: Director agent ────────────────────────────────────
    logger.info("[5/5] Director synthesis...")
    from src.agents.director_agent import synthesize_portfolio
    director_report = synthesize_portfolio(snapshot, analyst_reports, report_date=today)
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
