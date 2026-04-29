"""Main pipeline — runs the full financial-bytes workflow end-to-end."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from loguru import logger

from src.config import settings
from src.portfolio.reader import read_portfolio, save_portfolio_to_db
from src.portfolio.models import PortfolioSnapshot


def _load_purchase_history(portfolio_name: str) -> dict[str, list[dict]]:
    """Load per-lot purchase history from the portfolio def, if configured.

    Returns a dict keyed by ticker → list of lot dicts with keys:
      shares (Decimal | None), cost_basis (Decimal), purchase_date (str ISO).
    Empty dict if no purchase_history is configured.
    """
    import json
    from src.portfolio.portfolio_config import load_portfolio_defs

    try:
        defs = load_portfolio_defs()
    except Exception:
        return {}

    pdef = next((d for d in defs if d.name == portfolio_name), None)
    if pdef is None or not pdef.purchase_history:
        return {}

    history_path = Path(pdef.purchase_history)
    if not history_path.exists():
        logger.warning(f"Purchase history file not found: {history_path}")
        return {}

    with history_path.open(encoding="utf-8") as f:
        raw = json.load(f)

    # Strip comment keys and empty lot lists
    return {k: v for k, v in raw.items() if not k.startswith("_") and v}


def _apply_purchase_history_to_holdings(
    holdings: list, lot_overrides: dict[str, list[dict]]
) -> None:
    """Mutate holdings in-place: set purchase_date to the earliest lot date per ticker.

    This gives the analyst agent accurate holding period context.
    """
    from datetime import date as _date

    for holding in holdings:
        lots = lot_overrides.get(holding.ticker)
        if not lots:
            continue
        dates = []
        for lot in lots:
            ds = lot.get("purchase_date")
            if ds:
                try:
                    dates.append(_date.fromisoformat(ds))
                except ValueError:
                    pass
        if dates:
            holding.purchase_date = min(dates)


def _resolve_portfolio_csv(portfolio_name: str) -> tuple[str, bool]:
    """Look up portfolio by name in portfolios.json and return (csv_path, is_temp).

    Tries fidelity_positions → transactions_path → csv_path in that order.
    Falls back to settings.portfolio_csv_path if nothing is configured.
    Caller must unlink the file when is_temp is True.
    """
    from src.portfolio.portfolio_config import load_portfolio_defs

    try:
        defs = load_portfolio_defs()
    except Exception as e:
        logger.warning(f"Could not load portfolio config: {e}")
        return settings.portfolio_csv_path, False

    pdef = next((d for d in defs if d.name == portfolio_name), None)
    if pdef is None:
        logger.warning(f"Portfolio '{portfolio_name}' not found in config, using default CSV")
        return settings.portfolio_csv_path, False

    # ── Plaid (live, preferred when configured) ──────────────────────
    if pdef.plaid_access_token_env:
        import os
        token = os.getenv(pdef.plaid_access_token_env)
        if token:
            try:
                from src.portfolio.plaid_reader import read_plaid_holdings
                from src.portfolio.transaction_reader import export_holdings_to_csv
                logger.info(f"[plaid] Loading live holdings for {portfolio_name}...")
                holdings = read_plaid_holdings(portfolio_name)
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".csv", delete=False, prefix="fb_portfolio_"
                ) as tmp:
                    tmp_path = tmp.name
                export_holdings_to_csv(holdings, tmp_path)
                return tmp_path, True
            except Exception as e:
                logger.warning(f"[plaid] Failed to fetch live holdings ({e}) — falling through to Fidelity CSV")
        else:
            logger.debug(f"[plaid] {pdef.plaid_access_token_env} not set — skipping Plaid")

    # ── Fidelity CSV (static export fallback) ────────────────────────
    if pdef.fidelity_positions:
        from src.portfolio.fidelity_reader import read_fidelity_positions
        from src.portfolio.transaction_reader import export_holdings_to_csv
        holdings = read_fidelity_positions(
            pdef.fidelity_positions, account_filter=pdef.fidelity_account_filter
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, prefix="fb_portfolio_"
        ) as tmp:
            tmp_path = tmp.name
        export_holdings_to_csv(holdings, tmp_path)
        return tmp_path, True

    if pdef.transactions_path:
        from src.portfolio.transaction_reader import read_transactions, export_holdings_to_csv
        holdings = read_transactions(Path(pdef.transactions_path))
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, prefix="fb_portfolio_"
        ) as tmp:
            tmp_path = tmp.name
        export_holdings_to_csv(holdings, tmp_path)
        return tmp_path, True

    if pdef.csv_path:
        return pdef.csv_path, False

    return settings.portfolio_csv_path, False


def _resolve_recipients(portfolio_name: str, override: list[str] | None) -> list[str] | None:
    """Return email recipients: explicit override takes priority, then portfolio def, then None."""
    if override:
        return override
    from src.portfolio.portfolio_config import load_portfolio_defs
    try:
        defs = load_portfolio_defs()
        pdef = next((d for d in defs if d.name == portfolio_name), None)
        if pdef and pdef.email_recipients:
            return pdef.email_recipients
    except Exception:
        pass
    return None


def _fetch_signals_for_ticker(ticker: str):
    """Fetch massive.com signals + Finviz fundamentals in a worker thread."""
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

        # Augment with Finviz fundamentals and SEC filings
        try:
            from src.scrapers.finviz_scraper import FinvizScraper
            from src.api.models import FinvizFundamentals

            fv = FinvizScraper()
            fund_data = fv.scrape_fundamentals(ticker)
            if fund_data and signals:
                # Only keep fields that belong to FinvizFundamentals
                valid_fields = FinvizFundamentals.model_fields.keys()
                filtered = {k: v for k, v in fund_data.items() if k in valid_fields}
                signals.fundamentals = FinvizFundamentals(**filtered)

            sec_filings = fv.scrape_sec_filings(ticker)
            if sec_filings and signals:
                signals.sec_filings = sec_filings
        except Exception as fv_err:
            logger.debug(f"Finviz fundamentals fetch failed for {ticker}: {fv_err}")

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
    portfolio_name: str = "nbossn",
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
    out = output_dir or Path("newsletters")
    label = portfolio_label or portfolio_name.replace("_", " ").title()
    t0 = time.monotonic()

    logger.info("=" * 60)
    logger.info(f"Financial Bytes pipeline starting — {today} [{portfolio_name}]")
    logger.info("=" * 60)

    # ── Phase 1: Portfolio ─────────────────────────────────────────
    logger.info("[1/5] Loading portfolio...")
    # Resolve holdings source: explicit path → portfolios.json lookup → env default
    csv_path, _is_temp_csv = (
        (portfolio_csv, False) if portfolio_csv
        else _resolve_portfolio_csv(portfolio_name)
    )
    email_recipients = _resolve_recipients(portfolio_name, email_recipients)
    lot_overrides = _load_purchase_history(portfolio_name)
    if lot_overrides:
        logger.info(f"      Purchase history loaded: {list(lot_overrides.keys())}")
    try:
        holdings = read_portfolio(csv_path)
        save_portfolio_to_db(holdings, portfolio_name=portfolio_name)
    finally:
        if _is_temp_csv:
            try:
                os.unlink(csv_path)
            except Exception:
                pass
    # Apply earliest lot dates to holdings for accurate analyst context
    if lot_overrides:
        _apply_purchase_history_to_holdings(holdings, lot_overrides)
    snapshot = PortfolioSnapshot(holdings=holdings, lot_overrides=lot_overrides)
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
        snapshot = PortfolioSnapshot(holdings=holdings, prices=market_prices, as_of=today, lot_overrides=lot_overrides)

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

    # Find most recent prior newsletter for continuity context
    # generator saves to {out}/{portfolio_name}/ — look there first, then root out/
    prior_nl_path: Path | None = None
    try:
        for nl_dir in [out / portfolio_name, out]:
            prior_htmls = sorted(nl_dir.glob("*.html"), reverse=True)
            if prior_htmls:
                prior_nl_path = prior_htmls[0]
                break
        if prior_nl_path:
            logger.info(f"      Prior newsletter: {prior_nl_path.name}")
    except Exception:
        pass

    director_report = synthesize_portfolio(
        snapshot, analyst_reports,
        report_date=today,
        portfolio_name=portfolio_name,
        prior_newsletter_path=prior_nl_path,
    )
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
