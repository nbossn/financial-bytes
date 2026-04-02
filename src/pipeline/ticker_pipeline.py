"""Standalone ticker analysis pipeline — scrape, signals, quant, analyst, MD report."""
from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from pathlib import Path

from loguru import logger


def run_ticker_pipeline(
    ticker: str,
    report_date: date | None = None,
    skip_scrape: bool = False,
    output_dir: Path | None = None,
) -> dict:
    """Full pipeline for a single ticker:
      scrape → signals + quant → analyst → quant agent → MD plays → report

    Returns dict with keys: status, ticker, report_date, analyst_report,
    quant_report, md_report, paths.
    """
    today = report_date or date.today()
    out = (output_dir or Path("newsletters")) / "ticker-reports"
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()

    logger.info("=" * 60)
    logger.info(f"Ticker pipeline starting — {ticker} [{today}]")
    logger.info("=" * 60)

    # ── Phase 1: Scrape ───────────────────────────────────────────
    from src.scrapers.scraper_orchestrator import scrape_ticker
    from src.scrapers.base_scraper import ScrapedArticle
    from src.db.models import Article
    from src.db.session import get_db
    from src.config import settings
    from datetime import datetime, timedelta, timezone

    articles: list[ScrapedArticle]
    if skip_scrape:
        logger.info(f"[1/5] Loading cached articles for {ticker}...")
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.article_lookback_hours)
        with get_db() as db:
            rows = (
                db.query(Article)
                .filter(Article.ticker == ticker, Article.scraped_at >= cutoff)
                .order_by(Article.published_at.desc())
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
        logger.info(f"      {len(articles)} cached article(s) loaded")
    else:
        logger.info(f"[1/5] Scraping articles for {ticker}...")
        articles = scrape_ticker(ticker, skip_cached=False)
        logger.info(f"      {len(articles)} article(s) scraped")

    # ── Phase 2: Signals + Finviz deep-scrape ─────────────────────
    logger.info(f"[2/5] Fetching market signals + Finviz deep data for {ticker}...")
    from src.api.models import (
        FinvizFundamentals, FinvizAnalystRating, InsiderTrade, QuantMetrics
    )
    from src.scrapers.finviz_scraper import FinvizScraper
    from src.api.massive_client import MassiveClient
    from src.api.endpoints import MassiveEndpoints

    signals = None
    current_price: Decimal | None = None
    fundamentals: FinvizFundamentals | None = None
    finviz_analyst_ratings: list[FinvizAnalystRating] = []
    insider_trades: list[InsiderTrade] = []

    # Massive.com signals (may 403 on lower plans — graceful fallback)
    try:
        with MassiveClient() as client:
            endpoints = MassiveEndpoints(client)
            signals = endpoints.get_ticker_signals(ticker)
            if signals and signals.quote and signals.quote.current_price:
                current_price = Decimal(str(signals.quote.current_price))
    except Exception as e:
        logger.warning(f"      Massive signals unavailable: {e}")

    # Finviz comprehensive scrape
    fv = FinvizScraper()
    try:
        fund_data = fv.scrape_fundamentals(ticker)
        if fund_data:
            valid_fields = FinvizFundamentals.model_fields.keys()
            filtered = {k: v for k, v in fund_data.items() if k in valid_fields}
            fundamentals = FinvizFundamentals(**filtered)
            if signals:
                signals.fundamentals = fundamentals
            if fundamentals.current_price_raw and not current_price:
                current_price = Decimal(str(fundamentals.current_price_raw))
            logger.info(
                f"      Fundamentals: P/S={fundamentals.ps_ratio}, "
                f"GrossMargin={fundamentals.gross_margin}%, "
                f"ShortFloat={fundamentals.short_float}%"
            )
    except Exception as e:
        logger.warning(f"      Finviz fundamentals error: {e}")

    try:
        raw_ratings = fv.scrape_analyst_ratings(ticker)
        finviz_analyst_ratings = [FinvizAnalystRating(**r) for r in raw_ratings]
        if signals:
            signals.finviz_analyst_ratings = finviz_analyst_ratings
        logger.info(f"      {len(finviz_analyst_ratings)} analyst ratings scraped")
    except Exception as e:
        logger.warning(f"      Analyst ratings scrape error: {e}")

    try:
        raw_trades = fv.scrape_insider_trades(ticker)
        insider_trades = [InsiderTrade(**t) for t in raw_trades]
        if signals:
            signals.insider_trades = insider_trades
        logger.info(f"      {len(insider_trades)} insider trades scraped")
    except Exception as e:
        logger.warning(f"      Insider trades scrape error: {e}")

    try:
        sec_filings = fv.scrape_sec_filings(ticker)
        if signals:
            signals.sec_filings = sec_filings
    except Exception as e:
        logger.debug(f"      SEC filings error: {e}")

    # ── Phase 3: Quant metrics ─────────────────────────────────────
    logger.info(f"[3/5] Computing quantitative metrics for {ticker}...")
    from src.scrapers.yahoo_finance_data import compute_quant_metrics

    quant_metrics = compute_quant_metrics(ticker)
    if signals:
        signals.quant_metrics = quant_metrics

    # ── Phase 4: Analyst agent ─────────────────────────────────────
    logger.info(f"[4/5] Running analyst agent for {ticker}...")
    from src.agents.analyst_agent import analyze_ticker
    from src.portfolio.models import Holding

    cost = current_price or Decimal("0")
    holding = Holding(ticker=ticker, shares=Decimal("0"), cost_basis=cost, purchase_date=today)
    analyst_report = analyze_ticker(
        holding, articles, signals=signals, report_date=today, portfolio_name="ticker-report"
    )
    logger.info(f"      {ticker}: {analyst_report.recommendation} ({analyst_report.confidence:.0%})")

    # ── Phase 4b: Quant agent ─────────────────────────────────────
    logger.info(f"[4b/5] Running quant agent for {ticker}...")
    from src.agents.quant_agent import run_quant_agent

    quant_report = run_quant_agent(
        ticker=ticker,
        quant_metrics=quant_metrics,
        fundamentals=fundamentals,
        insider_trades=insider_trades,
        report_date=today,
    )
    logger.info(f"      Risk: {quant_report.risk_profile}, Momentum: {quant_report.momentum_signal}")

    # ── Phase 5: MD agent ─────────────────────────────────────────
    logger.info(f"[5/5] Running Managing Director agent for {ticker}...")
    from src.agents.managing_director_agent import run_md_agent

    md_report = run_md_agent(
        ticker=ticker,
        analyst_report=analyst_report,
        quant_report=quant_report,
        fundamentals=fundamentals,
        finviz_ratings=finviz_analyst_ratings,
        insider_trades=insider_trades,
        report_date=today,
    )
    logger.info(f"      Stance: {md_report.overall_stance}, Plays: {len(md_report.plays)}")

    # ── Render comprehensive report ────────────────────────────────
    paths = _render_report(ticker, analyst_report, quant_report, md_report, current_price, out, today)

    elapsed = time.monotonic() - t0
    logger.info(f"Ticker pipeline complete in {elapsed:.1f}s")
    logger.info("=" * 60)

    return {
        "status": "ok",
        "ticker": ticker,
        "report_date": today,
        "analyst_report": analyst_report,
        "quant_report": quant_report,
        "md_report": md_report,
        "paths": paths,
    }


def _render_report(ticker, analyst_report, quant_report, md_report, current_price, out, report_date) -> dict:
    date_str = report_date.strftime("%Y-%m-%d")
    md = _build_markdown(ticker, analyst_report, quant_report, md_report, current_price, report_date)
    md_path = out / f"{ticker}-{date_str}.md"
    md_path.write_text(md, encoding="utf-8")

    html = _md_to_html(md, ticker, date_str, md_report.overall_stance)
    html_path = out / f"{ticker}-{date_str}.html"
    html_path.write_text(html, encoding="utf-8")

    logger.info(f"Report written → {md_path}")
    return {"md": md_path, "html": html_path}


def _sentiment_bar(sentiment: float) -> str:
    filled = round((sentiment + 1) / 2 * 20)
    return "[" + "█" * filled + "░" * (20 - filled) + f"] {sentiment:+.2f}"


def _stance_emoji(stance: str) -> str:
    mapping = {
        "Aggressive Long": "🚀", "Long": "🟢", "Cautious Long": "📈",
        "Neutral": "⚪", "Cautious Short": "📉", "Short": "🔴", "Aggressive Short": "🩸",
    }
    return mapping.get(stance, "⚪")


def _build_markdown(ticker, analyst, quant, md, current_price, report_date) -> str:
    rec_emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}.get(analyst.recommendation, "⚪")
    price_str = f"${current_price:.2f}" if current_price else "N/A"
    pt_str = f"${analyst.price_target:.2f}" if analyst.price_target else "N/A"

    def _n(v, fmt=".2f", suffix="", default="N/A"):
        return f"{v:{fmt}}{suffix}" if v is not None else default

    # ── Quant metrics table ────────────────────────────────────────
    quant_rows = [
        ("Beta (market sensitivity)", _n(quant.beta, ".3f"), quant.beta_interpretation),
        ("Alpha (excess return vs risk)", _n(quant.alpha_annualized, "+.2f", "%"), quant.alpha_interpretation),
        ("Sharpe Ratio (return per unit of risk)", _n(quant.sharpe_ratio, ".3f"), quant.return_quality),
        ("Sortino Ratio (return per unit of downside risk)", _n(quant.sortino_ratio, ".3f"), ""),
        ("Annualized Return", _n(quant.annualized_return, "+.1f", "%"), ""),
        ("Annualized Volatility (price swings)", _n(quant.annualized_volatility, ".1f", "%"), ""),
        ("Max Drawdown (worst peak-to-trough drop)", _n(quant.max_drawdown, ".1f", "%"), quant.drawdown_assessment),
    ]
    quant_table = "| Metric | Value | What It Means |\n|--------|-------|---------------|\n"
    for label, val, interp in quant_rows:
        quant_table += f"| {label} | {val} | {interp} |\n"

    quant_flags = "\n".join(f"- {f}" for f in quant.key_quant_flags) or "- None"

    # ── Momentum table ─────────────────────────────────────────────
    m = quant
    momentum_table = (
        "| 1-Month | 3-Month | 6-Month | RSI(14) | Signal |\n"
        "|---------|---------|---------|---------|--------|\n"
        f"| {_n(m.momentum_1m, '+.1f', '%')} | {_n(m.momentum_3m, '+.1f', '%')} "
        f"| {_n(m.momentum_6m, '+.1f', '%')} | {_n(m.rsi_14, '.1f')} | **{m.momentum_signal}** |\n"
    ) if hasattr(m, 'momentum_1m') else ""

    # ── MD plays section ───────────────────────────────────────────
    plays_md = ""
    for i, play in enumerate(md.plays, 1):
        plays_md += f"""
### Play {i}: {play.play_type} ({play.time_horizon.title()}) — Conviction: {play.conviction}

{play.thesis}

| | |
|--|--|
| **Entry** | {play.entry} |
| **Target** | {play.target} |
| **Stop Loss** | {play.stop_loss} |
| **Structure** | {play.specific_structure} |
| **Position Size** | {play.position_size} |
| **Risk/Reward** | {play.risk_reward} |
"""

    key_levels = ""
    if md.key_levels:
        kl = md.key_levels
        key_levels = (
            f"\n| Level | Price | Why It Matters |\n|-------|-------|----------------|\n"
            f"| Strong Support | {kl.strong_support or 'N/A'} | Floor where buyers have stepped in |\n"
            f"| Resistance | {kl.resistance or 'N/A'} | Ceiling where sellers have dominated |\n"
            f"| Breakout Trigger | {kl.breakout_trigger or 'N/A'} | A close above this shifts bias to bullish |\n"
        )

    insider_warn = f"\n> **⚠️ Insider Warning:** {md.insider_warning}\n" if md.insider_warning else ""

    catalysts = "\n".join(f"- {c}" for c in analyst.key_catalysts) or "- None identified"
    risks = "\n".join(f"- {r}" for r in analyst.key_risks) or "- None identified"

    return f"""# {ticker} — Deep-Dive Report  ·  {report_date.strftime("%B %d, %Y")}

| Price | Analyst Target | Recommendation | Sentiment | Articles |
|-------|---------------|----------------|-----------|----------|
| {price_str} | {pt_str} | {rec_emoji} **{analyst.recommendation}** ({analyst.confidence:.0%}) | {analyst.sentiment_label} ({analyst.sentiment:+.2f}) | {analyst.article_count} |

---

## Analyst View

{analyst.summary}

{analyst.recommendation_context}

**Catalysts:** {" · ".join(analyst.key_catalysts) if analyst.key_catalysts else "None identified"}

**Risks:** {" · ".join(analyst.key_risks) if analyst.key_risks else "None identified"}

---

## Trade Stance

{_stance_emoji(md.overall_stance)} **{md.overall_stance}** | Conviction: **{md.conviction}**

{md.md_thesis}
{insider_warn}
{plays_md}
### Key Levels
{key_levels}
**Macro:** {md.macro_considerations}

**Position Management:** {md.position_management}

---

## Quantitative Snapshot

{quant_table}

**Risk Profile:** {quant.risk_profile} — {quant.risk_profile_rationale}

**Fair Value:** {quant.fair_value_note}

### Momentum  *(how the stock has trended over time)*
{momentum_table}
**Short Squeeze Risk** *(what happens if too many short-sellers are forced to buy at once)*: {quant.short_squeeze_risk} | **Insider Signal** *(are executives buying or selling their own stock?)*: {quant.insider_signal}

### Quant Flags
{quant_flags}

---

*Report date: {report_date} · Articles analyzed: {analyst.article_count}*
"""


def _md_to_html(md_text: str, ticker: str, date_str: str, stance: str) -> str:
    try:
        import markdown as md_lib  # type: ignore
        body = md_lib.markdown(md_text, extensions=["tables", "fenced_code"])
    except ImportError:
        body = f"<pre>{md_text}</pre>"

    stance_color = (
        "#2ea44f" if "Long" in stance else
        "#d93f0b" if "Short" in stance else
        "#e3b341"
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{ticker} Deep-Dive — {date_str}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         max-width: 960px; margin: 40px auto; padding: 0 24px;
         background: #0d1117; color: #c9d1d9; line-height: 1.65; }}
  h1 {{ color: #58a6ff; border-bottom: 2px solid #30363d; padding-bottom: 12px; }}
  h2 {{ color: {stance_color}; margin-top: 36px; border-bottom: 1px solid #30363d; padding-bottom: 6px; }}
  h3 {{ color: #79c0ff; margin-top: 24px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
  th {{ background: #161b22; color: #58a6ff; padding: 8px 12px; text-align: left; }}
  td {{ padding: 7px 12px; border-bottom: 1px solid #21262d; }}
  tr:hover td {{ background: #161b22; }}
  code, pre {{ background: #161b22; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }}
  blockquote {{ border-left: 3px solid #d93f0b; padding-left: 16px; color: #f0a0a0; margin: 16px 0; }}
  hr {{ border: none; border-top: 1px solid #30363d; margin: 28px 0; }}
  ul, ol {{ padding-left: 24px; }}
  li {{ margin: 5px 0; }}
  strong {{ color: #f0f6fc; }}
  a {{ color: #58a6ff; }}
</style>
</head>
<body>
{body}
</body>
</html>"""
