"""Click CLI — entry point for financial-bytes commands."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import click
from loguru import logger

from src.config import settings


def _setup_logging(level: str = "INFO") -> None:
    import sys
    logger.remove()
    logger.add(sys.stderr, level=level, colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
    logger.add(
        settings.log_file,
        level=level,
        rotation="10 MB",
        retention="30 days",
        compression="gz",
    )


_TICKER_RE = __import__("re").compile(r"^[A-Z]{1,5}$")

_ALLOWED_OUTPUT_BASE = Path("newsletters").resolve()


def _parse_date(ctx, param, value) -> date | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise click.BadParameter("Date must be in YYYY-MM-DD format")


def _validate_ticker(ticker: str) -> str:
    """Raise UsageError if ticker is not 1-5 uppercase letters."""
    t = ticker.strip().upper()
    if not _TICKER_RE.match(t):
        raise click.UsageError(f"Invalid ticker symbol '{ticker}' — must be 1-5 letters (A-Z)")
    return t


def _validate_output_dir(output_dir: str) -> Path:
    """Resolve and confine output_dir to the newsletters tree."""
    resolved = Path(output_dir).resolve()
    # Allow any path under the project root or under newsletters/
    # (operator may choose a custom absolute path; just block traversal tricks)
    if ".." in Path(output_dir).parts:
        raise click.UsageError(f"Invalid output directory '{output_dir}' — path traversal not allowed")
    return resolved


@click.group()
@click.option("--log-level", default="INFO", show_default=True,
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
              help="Log verbosity level")
def cli(log_level: str) -> None:
    """Financial Bytes — automated daily portfolio newsletter."""
    _setup_logging(log_level)


# ── run ───────────────────────────────────────────────────────────
@cli.command()
@click.option("--portfolio", "-p", default=None, help="Path to portfolio CSV")
@click.option("--transactions", "-t", default=None,
              help="Path to Robinhood transaction CSV (builds portfolio on the fly)")
@click.option("--date", "-d", default=None, callback=_parse_date, help="Report date (YYYY-MM-DD)")
@click.option("--skip-scrape", is_flag=True, help="Use cached articles from DB")
@click.option("--skip-email", is_flag=True, help="Generate newsletter but don't send email")
@click.option("--output-dir", default="newsletters", show_default=True, help="Output directory")
def run(portfolio, transactions, date, skip_scrape, skip_email, output_dir) -> None:
    """Run the full pipeline: scrape → analyse → newsletter → email.

    Portfolio source (pick one):
      --portfolio   existing portfolio CSV (ticker, shares, cost_basis, purchase_date)
      --transactions  Robinhood activity export CSV (holdings computed automatically)
    """
    if transactions and portfolio:
        raise click.UsageError("Specify either --portfolio or --transactions, not both.")

    if transactions:
        # Derive a temporary portfolio CSV from the transaction history
        from src.portfolio.transaction_reader import read_transactions, export_holdings_to_csv
        import tempfile, os

        txn_path = Path(transactions)
        if not txn_path.exists():
            raise click.BadParameter(f"Transaction file not found: {txn_path}", param_hint="--transactions")

        holdings = read_transactions(txn_path)
        click.echo(f"Derived {len(holdings)} holdings from transactions: {[h.ticker for h in holdings]}")

        # Write to a temp portfolio CSV and hand off to the pipeline
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, prefix="fb_portfolio_") as tmp:
            tmp_path = tmp.name
        try:
            export_holdings_to_csv(holdings, tmp_path)
            portfolio = tmp_path
            from src.pipeline.main_pipeline import run_pipeline
            result = run_pipeline(
                portfolio_csv=portfolio,
                report_date=date,
                skip_scrape=skip_scrape,
                skip_email=skip_email,
                output_dir=_validate_output_dir(output_dir),
            )
        finally:
            os.unlink(tmp_path)
    else:
        from src.pipeline.main_pipeline import run_pipeline
        result = run_pipeline(
            portfolio_csv=portfolio,
            report_date=date,
            skip_scrape=skip_scrape,
            skip_email=skip_email,
            output_dir=_validate_output_dir(output_dir),
        )

    click.echo(f"Done. Status: {result['status']}")


# ── import-transactions ───────────────────────────────────────────
@cli.command("import-transactions")
@click.argument("csv_path", type=click.Path(exists=True))
@click.option("--output", "-o", default=None,
              help="Write derived portfolio to this file (default: overwrites portfolio.csv)")
@click.option("--dry-run", is_flag=True,
              help="Print derived holdings without writing any files")
def import_transactions(csv_path: str, output: str | None, dry_run: bool) -> None:
    """Derive current holdings from a Robinhood transaction CSV.

    Parses all Buy/Sell rows, computes net shares and weighted average
    cost basis per ticker, and writes a portfolio CSV ready for `run`.

    CSV_PATH: path to the Robinhood activity export file.
    """
    from src.portfolio.transaction_reader import read_transactions, export_holdings_to_csv

    holdings = read_transactions(Path(csv_path))

    click.echo(f"Derived {len(holdings)} open holdings:\n")
    click.echo(f"  {'TICKER':<8} {'SHARES':>12} {'AVG COST':>12} {'TOTAL COST':>14}  FIRST BUY")
    click.echo("  " + "-" * 58)
    for h in holdings:
        first_buy = str(h.purchase_date) if h.purchase_date else "unknown"
        click.echo(
            f"  {h.ticker:<8} {float(h.shares):>12.4f} "
            f"{float(h.cost_basis):>12.4f} "
            f"{float(h.total_cost):>14,.2f}  {first_buy}"
        )

    if dry_run:
        click.echo("\n(Dry run — no files written)")
        return

    dest = Path(output) if output else Path(settings.portfolio_csv_path)
    export_holdings_to_csv(holdings, dest)
    click.echo(f"\nPortfolio written to {dest}")


# ── schedule ──────────────────────────────────────────────────────
@cli.command()
def schedule() -> None:
    """Start the APScheduler daemon (runs daily at configured time)."""
    from src.scheduler import start_scheduler
    logger.info("Starting scheduler daemon...")
    start_scheduler()


# ── scrape ────────────────────────────────────────────────────────
@cli.command()
@click.argument("tickers", nargs=-1, required=True)
def scrape(tickers: tuple[str, ...]) -> None:
    """Scrape news articles for one or more TICKERS."""
    if len(tickers) > 50:
        raise click.UsageError("Too many tickers — maximum 50 per invocation")
    from src.scrapers.scraper_orchestrator import scrape_ticker
    for ticker in tickers:
        t = _validate_ticker(ticker)
        articles = scrape_ticker(t)
        click.echo(f"{t}: {len(articles)} article(s) scraped")


# ── analyse ───────────────────────────────────────────────────────
@cli.command()
@click.argument("tickers", nargs=-1, required=True)
@click.option("--date", "-d", default=None, callback=_parse_date, help="Report date (YYYY-MM-DD)")
def analyse(tickers: tuple[str, ...], date: date | None) -> None:
    """Run analyst agents for one or more TICKERS (uses cached data)."""
    from src.agents.analyst_agent import analyze_ticker
    from src.portfolio.reader import read_portfolio
    from src.scrapers.base_scraper import ScrapedArticle
    from src.db.models import Article as DBArticle
    from src.db.session import get_db
    from datetime import datetime, timedelta, timezone

    holdings_map = {h.ticker: h for h in read_portfolio(settings.portfolio_csv_path)}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.article_lookback_hours)

    for ticker in tickers:
        ticker = _validate_ticker(ticker)
        if ticker not in holdings_map:
            click.echo(f"Warning: {ticker} not in portfolio, skipping", err=True)
            continue
        holding = holdings_map[ticker]
        with get_db() as db:
            rows = (
                db.query(DBArticle)
                .filter(DBArticle.ticker == ticker, DBArticle.scraped_at >= cutoff)
                .order_by(DBArticle.published_at.desc())
                .limit(settings.max_articles_per_ticker)
                .all()
            )
        articles = [
            ScrapedArticle(
                ticker=r.ticker, headline=r.headline, url=r.url,
                source=r.source, body=r.body, snippet=r.snippet,
                published_at=r.published_at,
            )
            for r in rows
        ]
        report = analyze_ticker(holding, articles, report_date=date)
        click.echo(f"{ticker}: {report.recommendation} ({report.confidence:.0%}) — {report.sentiment_label}")


# ── newsletter ────────────────────────────────────────────────────
@cli.command()
@click.option("--date", "-d", default=None, callback=_parse_date, help="Report date (YYYY-MM-DD)")
@click.option("--skip-email", is_flag=True, help="Generate only, don't send")
@click.option("--output-dir", default="newsletters", show_default=True)
def newsletter(date: date | None, skip_email: bool, output_dir: str) -> None:
    """Regenerate newsletter from existing DB data and optionally send."""
    from src.pipeline.main_pipeline import run_pipeline
    result = run_pipeline(
        report_date=date,
        skip_scrape=True,
        skip_email=skip_email,
        output_dir=Path(output_dir),
    )
    for fmt, path in result["paths"].items():
        if path:
            click.echo(f"  {fmt.upper()}: {path}")


# ── test-newsletter ───────────────────────────────────────────────
@cli.command("test-newsletter")
@click.option("--output-dir", default="newsletters/test", show_default=True)
def test_newsletter(output_dir: str) -> None:
    """Generate a test newsletter with synthetic data (no API calls)."""
    from src.agents.analyst_agent import AnalystReport
    from src.agents.director_agent import DirectorReport, StockSignal
    from src.portfolio.models import Holding, PortfolioSnapshot
    from src.newsletter.generator import generate
    from decimal import Decimal

    holdings = [
        Holding(ticker="MSFT", shares=Decimal("100"), cost_basis=Decimal("555.23"),
                purchase_date=date(2025, 8, 15)),
        Holding(ticker="NVDA", shares=Decimal("200"), cost_basis=Decimal("206.45"),
                purchase_date=date(2025, 11, 5)),
    ]
    snapshot = PortfolioSnapshot(holdings=holdings)

    analyst_reports = [
        AnalystReport(
            ticker="MSFT", report_date=date.today(), article_count=12,
            summary="Microsoft continues to show strong momentum driven by Azure AI adoption. "
                    "Copilot integration across Office suite is seeing higher-than-expected uptake.",
            sentiment=0.72, sentiment_label="Bullish",
            recommendation="BUY", confidence=0.81,
            recommendation_context="Current dip below cost basis represents a buying opportunity. "
                                   "Short-term: expect recovery to $380. Long-term: $450+ on AI tailwinds.",
            key_catalysts=["Azure AI revenue acceleration", "Copilot enterprise uptake", "Upcoming earnings beat"],
            key_risks=["Regulatory antitrust scrutiny", "OpenAI partnership uncertainty"],
            analyst_consensus="Strong Buy", price_target=450.0,
            technical_signal="RSI 42 (oversold); MACD crossing positive — bullish setup.",
        ),
        AnalystReport(
            ticker="NVDA", report_date=date.today(), article_count=15,
            summary="NVIDIA remains the dominant AI infrastructure play. Data center demand continues to exceed "
                    "supply with Blackwell GPUs sold out through 2025.",
            sentiment=0.85, sentiment_label="Very Bullish",
            recommendation="HOLD", confidence=0.88,
            recommendation_context="Position is significantly in the money at +47%. Hold for continued AI capex "
                                   "cycle. Short-term: $145-160 range. Long-term: $200+ as Blackwell ramps.",
            key_catalysts=["Blackwell GPU demand", "Sovereign AI spending", "Data center capex acceleration"],
            key_risks=["Export restrictions", "Valuation at 35x forward earnings", "AMD/Intel competition"],
            analyst_consensus="Buy", price_target=185.0,
            technical_signal="RSI 58 (neutral); trend intact above 50-day MA.",
        ),
    ]

    director = DirectorReport(
        report_date=date.today(),
        market_theme="AI infrastructure dominates as Fed pivot hopes lift tech sentiment.",
        five_min_summary="Markets are pricing in a softer Fed path following yesterday's CPI print. "
                         "Your portfolio is well-positioned: NVDA continues its AI infrastructure dominance "
                         "and MSFT is attractively priced for a re-entry. No immediate action required — "
                         "monitor Thursday's NVDA earnings call for any supply guidance changes.",
        portfolio_summary="Portfolio up 15.4% overall. NVDA driving gains at +47%, MSFT slightly under water "
                          "at -2.1%. Combined value $84,520 vs $82,368 cost basis.",
        global_market_context="S&P 500 futures +0.4% on cooler CPI. Nasdaq leading. Asia closed mixed; "
                               "Nikkei +0.8%, Hang Seng -0.3%. European tech higher pre-market.",
        top_opportunities=[
            StockSignal(ticker="MSFT", signal="BUY", rationale="Trading below cost basis with strong AI catalysts ahead.",
                        short_term="Recovery to $380 on earnings beat", long_term="$450+ on Copilot monetisation"),
        ],
        top_risks=[
            StockSignal(ticker="NVDA", risk="Export restriction escalation could clip data center revenue by 10-15%.",
                        severity="Medium", mitigation="Diversify into MSFT to reduce single-name concentration."),
        ],
        action_items=[
            "Monitor NVDA earnings call Thursday — listen for Blackwell supply commentary",
            "Consider adding to MSFT on any further weakness below $330",
            "Review portfolio allocation if NVDA >50% of total value",
        ],
        overall_sentiment=0.65,
        overall_recommendation="HOLD with selective add. Portfolio is healthy; MSFT weakness is a buying opportunity. No sells warranted today.",
    )

    paths = generate(
        report=director,
        analyst_reports=analyst_reports,
        snapshot=snapshot,
        output_dir=Path(output_dir),
    )
    click.echo("Test newsletter generated:")
    for fmt, path in paths.items():
        if path:
            click.echo(f"  {fmt.upper()}: {path}")


# ── audit ─────────────────────────────────────────────────────────
@cli.command()
def audit() -> None:
    """Run the fullstack agent: DB audit, cost audit, security scan, GitHub sync."""
    from src.agents.fullstack_agent import run_audit
    run_audit()


if __name__ == "__main__":
    cli()
