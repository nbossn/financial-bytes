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
_PORTFOLIO_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9_-]{1,64}$")

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


def _validate_portfolio_name(ctx, param, value: str) -> str:  # noqa: ARG001
    if not _PORTFOLIO_NAME_RE.match(value):
        raise click.BadParameter(
            "Portfolio name must be 1–64 alphanumeric characters, hyphens, or underscores."
        )
    return value


def _validate_output_dir(output_dir: str) -> Path:
    """Resolve and confine output_dir to within the newsletters tree."""
    resolved = Path(output_dir).resolve()
    try:
        resolved.relative_to(_ALLOWED_OUTPUT_BASE)
    except ValueError:
        raise click.UsageError(
            f"Output directory must be inside {_ALLOWED_OUTPUT_BASE}. Got: {resolved}"
        )
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
              help="Path to Robinhood/Fidelity transaction CSV (builds portfolio on the fly)")
@click.option("--date", "-d", default=None, callback=_parse_date, help="Report date (YYYY-MM-DD)")
@click.option("--skip-scrape", is_flag=True, help="Use cached articles from DB")
@click.option("--skip-email", is_flag=True, help="Generate newsletter but don't send email")
@click.option("--output-dir", default="newsletters", show_default=True, help="Output directory")
@click.option("--portfolio-name", default="nbossn", show_default=True, callback=_validate_portfolio_name,
              help="Portfolio identifier (used in DB and output path)")
@click.option("--portfolio-label", default=None,
              help="Display name for newsletter title (e.g. 'Roth IRA')")
@click.option("--email-recipients", "-r", multiple=True,
              help="Override email recipients (repeatable: -r a@b.com -r c@d.com)")
def run(portfolio, transactions, date, skip_scrape, skip_email, output_dir,
        portfolio_name, portfolio_label, email_recipients) -> None:
    """Run the full pipeline: scrape → analyse → newsletter → email.

    Portfolio source (pick one):
      --portfolio     existing portfolio CSV (ticker, shares, cost_basis, purchase_date)
      --transactions  activity export CSV (Robinhood or Fidelity; holdings computed automatically)
    """
    if transactions and portfolio:
        raise click.UsageError("Specify either --portfolio or --transactions, not both.")

    # Special sentinel: --portfolio robinhood pulls live positions via robin_stocks
    if portfolio and portfolio.lower() == "robinhood":
        from src.portfolio.robinhood_reader import read_robinhood_holdings, export_robinhood_to_csv, RobinhoodAuthError, RobinhoodReadError
        import tempfile, os as _os

        try:
            rh_holdings = read_robinhood_holdings()
        except (RobinhoodAuthError, RobinhoodReadError) as e:
            raise click.UsageError(str(e))

        click.echo(f"Robinhood: {len(rh_holdings)} holdings — {[h.ticker for h in rh_holdings]}")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, prefix="fb_rh_") as tmp:
            tmp_path = tmp.name
        try:
            export_robinhood_to_csv(rh_holdings, tmp_path)
            from src.pipeline.main_pipeline import run_pipeline
            result = run_pipeline(
                portfolio_csv=tmp_path,
                report_date=date,
                skip_scrape=skip_scrape,
                skip_email=skip_email,
                output_dir=_validate_output_dir(output_dir),
                portfolio_name=portfolio_name,
                portfolio_label=portfolio_label,
                email_recipients=recipients,
            )
        finally:
            _os.unlink(tmp_path)
        click.echo(f"Done. Status: {result['status']}")
        return

    recipients = list(email_recipients) if email_recipients else None

    if transactions:
        from src.portfolio.transaction_reader import read_transactions, export_holdings_to_csv
        import tempfile, os

        txn_path = Path(transactions)
        if not txn_path.exists():
            raise click.BadParameter(f"Transaction file not found: {txn_path}", param_hint="--transactions")

        holdings = read_transactions(txn_path)
        click.echo(f"Derived {len(holdings)} holdings from transactions: {[h.ticker for h in holdings]}")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, prefix="fb_portfolio_") as tmp:
            tmp_path = tmp.name
        try:
            export_holdings_to_csv(holdings, tmp_path)
            from src.pipeline.main_pipeline import run_pipeline
            result = run_pipeline(
                portfolio_csv=tmp_path,
                report_date=date,
                skip_scrape=skip_scrape,
                skip_email=skip_email,
                output_dir=_validate_output_dir(output_dir),
                portfolio_name=portfolio_name,
                portfolio_label=portfolio_label,
                email_recipients=recipients,
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
            portfolio_name=portfolio_name,
            portfolio_label=portfolio_label,
            email_recipients=recipients,
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


# ── robinhood-import ─────────────────────────────────────────────
@cli.command("robinhood-import")
@click.option("--output", "-o", default="portfolio-robinhood.csv",
              show_default=True, help="Write derived portfolio CSV to this path")
@click.option("--dry-run", is_flag=True, help="Print holdings without writing any files")
def robinhood_import(output: str, dry_run: bool) -> None:
    """Pull live holdings from Robinhood and write a portfolio CSV.

    Uses ROBINHOOD_EMAIL, ROBINHOOD_PASSWORD, ROBINHOOD_MFA_SECRET from .env.
    Output CSV is compatible with `run --portfolio <file>`.

    Note: robin_stocks is unofficial and technically against Robinhood ToS.
    Use for personal automation only.
    """
    from src.portfolio.robinhood_reader import (
        read_robinhood_holdings, export_robinhood_to_csv,
        RobinhoodAuthError, RobinhoodReadError,
    )

    try:
        holdings = read_robinhood_holdings()
    except (RobinhoodAuthError, RobinhoodReadError) as e:
        raise click.UsageError(str(e))

    click.echo(f"\nRobinhood positions ({len(holdings)}):\n")
    click.echo(f"  {'TICKER':<8} {'SHARES':>14} {'AVG COST':>12} {'TOTAL COST':>14}")
    click.echo("  " + "-" * 54)
    for h in holdings:
        click.echo(
            f"  {h.ticker:<8} {float(h.shares):>14.4f} "
            f"{float(h.cost_basis):>12.4f} "
            f"{float(h.total_cost):>14,.2f}"
        )

    if dry_run:
        click.echo("\n(Dry run — no files written)")
        return

    path = export_robinhood_to_csv(holdings, output)
    click.echo(f"\nPortfolio written to {path}")
    click.echo("Run the newsletter: financial-bytes run --portfolio " + path)


# ── fidelity-import ───────────────────────────────────────────────
@cli.command("fidelity-import")
@click.argument("positions_csv", type=click.Path(exists=True))
@click.option("--output", "-o", default=None, help="Write portfolio CSV to this path")
@click.option("--account-filter", default=None,
              help="Filter by account name substring (e.g. 'Trust')")
@click.option("--dry-run", is_flag=True, help="Print holdings without writing files")
def fidelity_import(positions_csv: str, output: str | None, account_filter: str | None, dry_run: bool) -> None:
    """Import holdings from a Fidelity Portfolio_Positions CSV export.

    POSITIONS_CSV: path to the Fidelity positions download (Portfolio_Positions_*.csv)
    """
    from src.portfolio.fidelity_reader import read_fidelity_positions, export_fidelity_to_portfolio_csv

    holdings = read_fidelity_positions(positions_csv, account_filter=account_filter)
    click.echo(f"Parsed {len(holdings)} holding(s):\n")
    click.echo(f"  {'TICKER':<8} {'SHARES':>14} {'AVG COST':>12} {'TOTAL COST':>14}")
    click.echo("  " + "-" * 54)
    for h in holdings:
        click.echo(
            f"  {h.ticker:<8} {float(h.shares):>14.4f} "
            f"{float(h.cost_basis):>12.4f} "
            f"{float(h.total_cost):>14,.2f}"
        )

    if dry_run:
        click.echo("\n(Dry run — no files written)")
        return

    if output:
        export_fidelity_to_portfolio_csv(positions_csv, output, account_filter=account_filter)
        click.echo(f"\nPortfolio CSV written to {output}")


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
@click.option("--portfolio-name", default="nbossn", show_default=True, callback=_validate_portfolio_name,
              help="Portfolio identifier to regenerate newsletter for")
@click.option("--force", is_flag=True, help="Re-run full pipeline even if newsletter already exists")
def newsletter(date: date | None, skip_email: bool, output_dir: str, portfolio_name: str, force: bool) -> None:
    """Regenerate newsletter from existing DB data and optionally send.

    If a rendered newsletter already exists for the given date and portfolio,
    skips the pipeline and sends directly from the existing files (fast path).
    Use --force to re-run the full pipeline regardless.
    """
    report_date = date or datetime.now().date()
    out_dir = _validate_output_dir(f"{output_dir}/{portfolio_name}")
    html_path = out_dir / f"{report_date.strftime('%Y-%m-%d')}.html"

    if not force and html_path.exists():
        logger.info(f"Newsletter already exists for {portfolio_name} on {report_date} — sending from files")
        if not skip_email:
            from src.delivery.email_sender import send_from_files
            from src.portfolio.portfolio_config import load_portfolio_defs
            defs = {p.name: p for p in load_portfolio_defs()}
            recipients = defs[portfolio_name].email_recipients if portfolio_name in defs else None
            send_from_files(report_date=report_date, output_dir=out_dir, recipients=recipients)
        for ext in ("html", "md", "pdf"):
            p = out_dir / f"{report_date.strftime('%Y-%m-%d')}.{ext}"
            if p.exists():
                click.echo(f"  {ext.upper()}: {p}")
        return

    from src.pipeline.main_pipeline import run_pipeline
    result = run_pipeline(
        report_date=date,
        skip_scrape=True,
        skip_email=skip_email,
        output_dir=_validate_output_dir(output_dir),
        portfolio_name=portfolio_name,
    )
    for fmt, path in result["paths"].items():
        if path:
            click.echo(f"  {fmt.upper()}: {path}")


# ── portfolios ────────────────────────────────────────────────────
@cli.command("portfolios")
def portfolios_list() -> None:
    """List configured portfolios from portfolios.json (or .env fallback)."""
    from src.portfolio.portfolio_config import load_portfolio_defs
    defs = load_portfolio_defs()
    click.echo(f"{'NAME':<20} {'LABEL':<30} {'CSV/TRANSACTIONS'}")
    click.echo("-" * 72)
    for p in defs:
        source = p.fidelity_positions or p.transactions_path or p.csv_path or "(none)"
        click.echo(f"{p.name:<20} {p.label:<30} {source}")
    click.echo(f"\n{len(defs)} portfolio(s) configured.")


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


# ── ticker-report ─────────────────────────────────────────────────
@cli.command("ticker-report")
@click.argument("ticker")
@click.option("--date", "-d", default=None, callback=_parse_date, help="Report date (YYYY-MM-DD)")
@click.option("--skip-scrape", is_flag=True, help="Use cached articles from DB")
@click.option("--output-dir", default="newsletters", show_default=True, help="Output directory")
def ticker_report(ticker: str, date: "date | None", skip_scrape: bool, output_dir: str) -> None:
    """Scrape, analyse, and report on a single TICKER (watchlist / deep-dive).

    Does not require the ticker to be in the portfolio. Scrapes fresh data,
    fetches market signals, runs the analyst agent, and writes a standalone
    Markdown + HTML report to OUTPUT_DIR/ticker-reports/.

    Example:
      financial-bytes ticker-report FIG
    """
    from src.pipeline.ticker_pipeline import run_ticker_pipeline

    t = _validate_ticker(ticker)
    result = run_ticker_pipeline(
        ticker=t,
        report_date=date,
        skip_scrape=skip_scrape,
        output_dir=_validate_output_dir(output_dir),
    )
    rpt = result["analyst_report"]
    click.echo("")
    click.echo(f"{'=' * 50}")
    click.echo(f"  {t} — Ticker Report ({result['report_date']})")
    click.echo(f"{'=' * 50}")
    click.echo(f"  Recommendation : {rpt.recommendation}")
    click.echo(f"  Confidence     : {rpt.confidence:.0%}")
    click.echo(f"  Sentiment      : {rpt.sentiment_label} ({rpt.sentiment:+.2f})")
    if rpt.price_target:
        click.echo(f"  Price Target   : ${rpt.price_target:.2f}")
    if rpt.analyst_consensus:
        click.echo(f"  Consensus      : {rpt.analyst_consensus}")
    click.echo("")
    click.echo("  Summary:")
    click.echo(f"  {rpt.summary}")
    click.echo("")
    if rpt.key_catalysts:
        click.echo("  Key Catalysts:")
        for c in rpt.key_catalysts:
            click.echo(f"    • {c}")
        click.echo("")
    if rpt.key_risks:
        click.echo("  Key Risks:")
        for r in rpt.key_risks:
            click.echo(f"    • {r}")
        click.echo("")
    # Quant section
    qrpt = result.get("quant_report")
    if qrpt:
        click.echo(f"{'─' * 50}")
        click.echo("  Quantitative Analysis:")
        click.echo(f"    Beta         : {qrpt.beta or 'N/A'}")
        click.echo(f"    Alpha (ann.) : {f'{qrpt.alpha_annualized:+.2f}%' if qrpt.alpha_annualized is not None else 'N/A'}")
        click.echo(f"    Sharpe       : {qrpt.sharpe_ratio or 'N/A'}")
        click.echo(f"    Sortino      : {qrpt.sortino_ratio or 'N/A'}")
        click.echo(f"    Ann. Return  : {f'{qrpt.annualized_return:+.1f}%' if qrpt.annualized_return is not None else 'N/A'}")
        click.echo(f"    Volatility   : {f'{qrpt.annualized_volatility:.1f}%' if qrpt.annualized_volatility is not None else 'N/A'}")
        click.echo(f"    Max Drawdown : {f'{qrpt.max_drawdown:.1f}%' if qrpt.max_drawdown is not None else 'N/A'}")
        click.echo(f"    Risk Profile : {qrpt.risk_profile}")
        click.echo(f"    Momentum     : {qrpt.momentum_signal}")
        click.echo(f"    Short Squeeze: {qrpt.short_squeeze_risk}")
        click.echo(f"    Insider      : {qrpt.insider_signal}")

    # MD section
    mdrpt = result.get("md_report")
    if mdrpt:
        click.echo(f"{'─' * 50}")
        click.echo(f"  MD Stance: {mdrpt.overall_stance} (Conviction: {mdrpt.conviction})")
        click.echo(f"  {mdrpt.md_thesis}")
        if mdrpt.insider_warning:
            click.echo(f"  ⚠  {mdrpt.insider_warning}")
        click.echo("")
        for i, play in enumerate(mdrpt.plays, 1):
            click.echo(f"  Play {i}: {play.play_type} [{play.time_horizon}] — {play.conviction}")
            click.echo(f"    Entry: {play.entry}  |  Target: {play.target}  |  Stop: {play.stop_loss}")
            click.echo(f"    R/R: {play.risk_reward}  |  Size: {play.position_size}")
            click.echo(f"    Structure: {play.specific_structure}")
            click.echo("")

    click.echo(f"{'=' * 50}")
    for fmt, path in result["paths"].items():
        if path:
            click.echo(f"  {fmt.upper()}: {path}")
    click.echo(f"{'=' * 50}")


# ── stop-loss check ───────────────────────────────────────────────
@cli.command("check-stops")
@click.option("--portfolio", "-p", default="portfolio.csv", show_default=True,
              help="Path to portfolio CSV")
@click.option("--portfolio-name", default="nbossn", show_default=True, callback=_validate_portfolio_name,
              help="Portfolio name for alert message")
@click.option("--no-alert", is_flag=True, help="Check only, do not send Discord alert")
@click.option(
    "--mode",
    type=click.Choice(["static", "dynamic", "hybrid"], case_sensitive=False),
    default="static",
    show_default=True,
    help=(
        "static: use portfolio.csv stop_loss_pct as-is (default). "
        "dynamic: use ATR/beta-computed thresholds, ignore static. "
        "hybrid: use tighter of dynamic and static (most protective)."
    ),
)
@click.option("--atr-multiplier", type=float, default=5.0, show_default=True,
              help="ATR multiplier for dynamic/hybrid modes (5.0 = 5× ATR14)")
def check_stops(portfolio: str, portfolio_name: str, no_alert: bool, mode: str, atr_multiplier: float) -> None:
    """Check portfolio positions against stop-loss thresholds and alert via Discord.

    Three modes:

    \b
    static  — uses stop_loss_pct from portfolio.csv (original behaviour)
    dynamic — computes ATR-based thresholds per position; ignores static values
    hybrid  — fires at whichever threshold is hit first (most protective)

    Dynamic thresholds scale to actual volatility: COIN (6% ATR) gets a wider
    stop than VOO (1% ATR). An earnings buffer widens any threshold within 7
    days of the next earnings report to avoid false alerts during pre-earnings
    volatility.
    """
    from pathlib import Path
    from src.alerts.stop_loss import run_stop_loss_check

    csv_path = Path(portfolio)
    if not csv_path.exists():
        raise click.UsageError(f"Portfolio CSV not found: {csv_path}")

    mode = mode.lower()
    if mode == "static":
        triggered = run_stop_loss_check(
            csv_path=csv_path,
            portfolio_name=portfolio_name,
            send_alert=not no_alert,
        )
    else:
        triggered = run_stop_loss_check(
            csv_path=csv_path,
            portfolio_name=portfolio_name,
            send_alert=not no_alert,
            dynamic_mode=mode,
            atr_multiplier=atr_multiplier,
        )

    if triggered:
        click.echo(f"\n🔴 {len(triggered)} stop-loss trigger(s) [{mode} mode]:")
        for t in triggered:
            if hasattr(t, "threshold_price_dynamic"):
                # DynamicStopCheck
                click.echo(
                    f"  {t.ticker}: ${t.current_price:.2f} "
                    f"(threshold ${t.threshold_price:.2f} [{mode}], "
                    f"pnl {t.current_pnl_pct:.1f}%)"
                )
            else:
                click.echo(
                    f"  {t.ticker}: ${float(t.current_price):.2f} "
                    f"(threshold ${float(t.threshold_price):.2f}, "
                    f"loss ${float(t.total_loss):,.0f})"
                )
    else:
        click.echo(f"✅ No stop-loss triggers — all positions within thresholds [{mode} mode]")


# ── suggest-stops ─────────────────────────────────────────────────
@cli.command("suggest-stops")
@click.option("--portfolio", "-p", default="portfolio.csv", show_default=True,
              help="Path to portfolio CSV")
@click.option("--atr-multiplier", type=float, default=5.0, show_default=True,
              help="ATR multiplier (5.0 = 5× ATR14; lower = tighter stops)")
@click.option("--update-csv", is_flag=True,
              help="Write recommended dynamic thresholds back to portfolio.csv stop_loss_pct column")
def suggest_stops(portfolio: str, atr_multiplier: float, update_csv: bool) -> None:
    """Show volatility-based stop-loss recommendations for all positions.

    Computes ATR-based thresholds (with beta-scaling fallback) and displays a
    comparison table alongside current static values. Also detects positions
    within 7 days of earnings and widens their recommendation accordingly.

    Use --update-csv to write the dynamic recommendations into portfolio.csv
    (replaces stop_loss_pct with the computed dynamic value).
    """
    import csv as csv_mod
    from pathlib import Path
    from src.alerts.dynamic_stops import suggest_all_stops, format_suggestions_table

    csv_path = Path(portfolio)
    if not csv_path.exists():
        raise click.UsageError(f"Portfolio CSV not found: {csv_path}")

    # Read all positions (including those without static stop_loss_pct)
    positions = []
    rows = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv_mod.DictReader(f)
        fieldnames = reader.fieldnames or []
        for row in reader:
            ticker = row.get("ticker", "").strip().upper()
            if not ticker:
                continue
            raw_stop = row.get("stop_loss_pct", "").strip()
            positions.append({
                "ticker": ticker,
                "stop_loss_pct": float(raw_stop) if raw_stop else None,
            })
            rows.append(dict(row))

    click.echo(f"Computing dynamic stops for {len(positions)} positions (ATR×{atr_multiplier})…\n")
    stops = suggest_all_stops(positions, atr_multiplier=atr_multiplier)

    table = format_suggestions_table(stops)
    click.echo(table)

    if update_csv:
        # Build a lookup by ticker
        stop_map = {s.ticker: s.dynamic_pct for s in stops}

        # Rewrite portfolio.csv with updated stop_loss_pct
        has_stop_col = "stop_loss_pct" in fieldnames
        out_fieldnames = list(fieldnames)
        if not has_stop_col:
            out_fieldnames.append("stop_loss_pct")

        updated_rows = []
        for row in rows:
            tkr = row.get("ticker", "").strip().upper()
            if tkr in stop_map:
                row["stop_loss_pct"] = f"{stop_map[tkr]:.4f}"
            updated_rows.append(row)

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=out_fieldnames)
            writer.writeheader()
            writer.writerows(updated_rows)

        click.echo(f"\n✅ Updated stop_loss_pct in {csv_path} ({len(stop_map)} positions)")
        click.echo("   Review changes before running check-stops — thresholds are now dynamic.")


# ── dividend check ────────────────────────────────────────────────
@cli.command("check-dividends")
@click.option("--portfolio", "-p", default="portfolio.csv", show_default=True,
              help="Path to portfolio CSV")
def check_dividends(portfolio: str) -> None:
    """Show dividend income projections and upcoming ex-dividend dates."""
    from pathlib import Path
    from decimal import Decimal
    import yfinance as yf

    from src.portfolio.reader import read_portfolio
    from src.portfolio.dividends import fetch_portfolio_dividends, format_dividend_section

    holdings = read_portfolio(portfolio)
    prices: dict[str, Decimal] = {}
    for h in holdings:
        try:
            hist = yf.Ticker(h.ticker).history(period="2d")
            if not hist.empty:
                prices[h.ticker] = Decimal(str(round(hist["Close"].iloc[-1], 4)))
        except Exception:
            prices[h.ticker] = h.cost_basis

    dividend_infos = fetch_portfolio_dividends(holdings, prices)

    if not dividend_infos:
        click.echo("No dividend-paying positions found in portfolio.")
        return

    section = format_dividend_section(dividend_infos)
    click.echo(section)


# ── audit ─────────────────────────────────────────────────────────
@cli.command()
def audit() -> None:
    """Run the fullstack agent: DB audit, cost audit, security scan, GitHub sync."""
    from src.agents.fullstack_agent import run_audit
    run_audit()


if __name__ == "__main__":
    cli()
