"""Financial Analyst Agent — per-ticker article analysis using claude-haiku-4-5."""
import asyncio
import json
import random
from datetime import date
from decimal import Decimal
from pathlib import Path
from string import Template

import subprocess

from loguru import logger
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings
from src.portfolio.models import Holding
from src.scrapers.base_scraper import ScrapedArticle
from src.api.models import TickerSignals

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "analyst_system.txt").read_text()
USER_PROMPT_TEMPLATE = (Path(__file__).parent / "prompts" / "analyst_user.txt").read_text()

MODEL = "claude-haiku-4-5-20251001"


class AnalystReport(BaseModel):
    ticker: str
    report_date: date
    article_count: int
    summary: str
    sentiment: float = Field(ge=-1.0, le=1.0)
    sentiment_label: str
    recommendation: str  # BUY, HOLD, SELL
    recommendation_context: str
    confidence: float = Field(ge=0.0, le=1.0)
    key_catalysts: list[str] = Field(default_factory=list)
    key_risks: list[str] = Field(default_factory=list)
    analyst_consensus: str | None = None
    price_target: float | None = None
    technical_signal: str | None = None
    chart_daily_url: str | None = None
    chart_weekly_url: str | None = None
    tax_note: str | None = None  # generated post-analysis from tax lot data


def _format_articles(articles: list[ScrapedArticle]) -> str:
    lines = []
    for i, a in enumerate(articles, 1):
        content = a.body or a.snippet or "(no content available)"
        lines.append(
            f"[{i}] {a.headline}\n"
            f"Source: {a.source} | Published: {a.published_at or 'unknown'}\n"
            f"URL: {a.url}\n"
            f"Content: {content[:800]}\n"
        )
    return "\n---\n".join(lines) if lines else "No articles available."


def _format_technicals(signals: TickerSignals | None) -> str:
    if not signals or not signals.technicals:
        return "No technical data available."
    t = signals.technicals
    return (
        f"RSI(14): {t.rsi or 'N/A'} | "
        f"MACD: {t.macd or 'N/A'} (Signal: {t.macd_signal or 'N/A'}) | "
        f"Signal: {t.signal_summary or 'N/A'}"
    )


def _format_ratings(signals: TickerSignals | None) -> str:
    if not signals or not signals.analyst_ratings:
        return "No analyst ratings available."
    lines = [f"Consensus: {signals.consensus_rating or 'N/A'} | Target: ${signals.consensus_price_target or 'N/A'}"]
    for r in signals.analyst_ratings[:5]:
        lines.append(f"  {r.analyst_firm or 'Unknown'}: {r.rating or 'N/A'} | Target: ${r.price_target or 'N/A'}")
    return "\n".join(lines)


def _format_fundamentals(signals: TickerSignals | None) -> str:
    """Format Finviz fundamentals for the analyst prompt."""
    if not signals or not signals.fundamentals:
        return "No fundamental data available."
    f = signals.fundamentals
    lines = []
    if f.market_cap_text:
        lines.append(f"Market Cap: {f.market_cap_text}")
    if f.pe_ratio is not None:
        lines.append(f"P/E: {f.pe_ratio:.1f}")
    if f.forward_pe is not None:
        lines.append(f"Forward P/E: {f.forward_pe:.1f}")
    if f.peg_ratio is not None:
        lines.append(f"PEG: {f.peg_ratio:.2f}")
    if f.eps_ttm is not None:
        lines.append(f"EPS (ttm): ${f.eps_ttm:.2f}")
    if f.eps_next_year is not None:
        lines.append(f"EPS Growth next Y: {f.eps_next_year:+.1f}%")
    if f.profit_margin is not None:
        lines.append(f"Profit Margin: {f.profit_margin:.1f}%")
    if f.oper_margin is not None:
        lines.append(f"Oper. Margin: {f.oper_margin:.1f}%")
    if f.roe is not None:
        lines.append(f"ROE: {f.roe:.1f}%")
    if f.debt_eq is not None:
        lines.append(f"Debt/Equity: {f.debt_eq:.2f}")
    if f.short_float is not None:
        lines.append(f"Short Float: {f.short_float:.1f}%")
    if f.short_ratio is not None:
        lines.append(f"Short Ratio: {f.short_ratio:.1f}")
    if f.insider_own is not None:
        lines.append(f"Insider Own: {f.insider_own:.1f}%")
    if f.inst_own is not None:
        lines.append(f"Inst. Own: {f.inst_own:.1f}%")
    if f.target_price is not None:
        lines.append(f"Analyst Target Price: ${f.target_price:.2f}")
    if f.perf_ytd is not None:
        lines.append(f"YTD Perf: {f.perf_ytd:+.1f}%")
    if f.perf_year is not None:
        lines.append(f"1Y Perf: {f.perf_year:+.1f}%")
    return " | ".join(lines) if lines else "No fundamental data available."


def _format_sec_filings(signals: TickerSignals | None) -> str:
    """Format recent SEC filings for the analyst prompt."""
    if not signals or not signals.sec_filings:
        return "No recent SEC filings available."
    lines = []
    for filing in signals.sec_filings[:8]:
        date = filing.get("date", "")
        form = filing.get("form_type", "")
        desc = filing.get("description", "")
        lines.append(f"  {date} [{form}] {desc}")
    return "\n".join(lines) if lines else "No recent SEC filings available."


def _build_user_prompt(
    holding: Holding,
    articles: list[ScrapedArticle],
    signals: TickerSignals | None,
    current_price: Decimal | None,
) -> str:
    price = current_price or holding.cost_basis
    pnl_pct = ((price - holding.cost_basis) / holding.cost_basis * 100) if holding.cost_basis else Decimal(0)
    pnl_dollars = (price - holding.cost_basis) * holding.shares

    # Simple string replacement (Jinja-style template in the .txt uses {{ }})
    prompt = USER_PROMPT_TEMPLATE
    replacements = {
        "{{ ticker }}": holding.ticker,
        "{{ shares }}": str(holding.shares),
        "{{ cost_basis }}": str(holding.cost_basis),
        "{{ purchase_date }}": str(holding.purchase_date),
        "{{ current_price }}": f"{price:.2f}",
        "{{ pnl_pct }}": f"{pnl_pct:+.1f}",
        "{{ pnl_dollars }}": f"{pnl_dollars:+.2f}",
        "{{ technical_signals }}": _format_technicals(signals),
        "{{ analyst_ratings }}": _format_ratings(signals),
        "{{ fundamentals_text }}": _format_fundamentals(signals),
        "{{ sec_filings_text }}": _format_sec_filings(signals),
        "{{ article_count }}": str(len(articles)),
        "{{ articles_text }}": _format_articles(articles),
    }
    for key, val in replacements.items():
        prompt = prompt.replace(key, val)
    return prompt


_RATE_LIMIT_SIGNALS = ("rate limit", "429", "overloaded", "529")


def _is_rate_limit_error(stderr: str) -> bool:
    low = stderr.lower()
    return any(s in low for s in _RATE_LIMIT_SIGNALS)


@retry(
    wait=wait_exponential(multiplier=2, min=5, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _call_claude(user_prompt: str) -> str:
    cmd = ["claude", "-p", "-", "--model", MODEL, "--system-prompt", SYSTEM_PROMPT]
    if settings.claude_skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    result = subprocess.run(cmd, input=user_prompt, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        err = result.stderr[:500]
        if _is_rate_limit_error(err):
            raise RuntimeError(f"Rate limit hit: {err}")
        raise RuntimeError(f"claude CLI error: {err}")
    return result.stdout.strip()


def analyze_ticker(
    holding: Holding,
    articles: list[ScrapedArticle],
    signals: TickerSignals | None = None,
    report_date: date | None = None,
    portfolio_name: str = "default",
) -> AnalystReport:
    """Run the analyst agent for one ticker and return structured report."""
    today = report_date or date.today()
    current_price = signals.quote.current_price if signals and signals.quote else None
    user_prompt = _build_user_prompt(holding, articles, signals, current_price)

    logger.info(f"Analyst agent: analyzing {holding.ticker} ({len(articles)} articles)")

    try:
        raw_json = _call_claude(user_prompt)
        # Strip markdown code fences if present
        raw_json = raw_json.strip()
        if raw_json.startswith("```"):
            raw_json = raw_json.split("```")[1]
            if raw_json.startswith("json"):
                raw_json = raw_json[4:]
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        logger.error(f"Analyst agent JSON parse error for {holding.ticker}: {e}")
        # Fallback minimal report
        data = {
            "ticker": holding.ticker,
            "summary": "Analysis unavailable due to parsing error.",
            "sentiment": 0.0,
            "sentiment_label": "Neutral",
            "recommendation": "HOLD",
            "recommendation_context": "Insufficient data to make a recommendation.",
            "confidence": 0.1,
            "key_catalysts": [],
            "key_risks": ["Analysis error — manual review recommended"],
        }

    report = AnalystReport(
        ticker=holding.ticker,
        report_date=today,
        article_count=len(articles),
        **{k: v for k, v in data.items() if k != "ticker"},
    )
    if signals and signals.technicals:
        report.chart_daily_url = signals.technicals.chart_daily_url
        report.chart_weekly_url = signals.technicals.chart_weekly_url

    _save_report(report, portfolio_name=portfolio_name)
    logger.info(f"Analyst report: {holding.ticker} → {report.recommendation} (confidence: {report.confidence:.0%})")
    return report


async def _call_claude_async(user_prompt: str) -> str:
    """Async subprocess wrapper for claude -p, prompting via stdin to avoid ARG_MAX limits."""
    for attempt in range(5):
        try:
            cmd = [
                "claude", "-p", "-", "--model", MODEL, "--system-prompt", SYSTEM_PROMPT,
            ]
            if settings.claude_skip_permissions:
                cmd.append("--dangerously-skip-permissions")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=user_prompt.encode()),
                    timeout=180,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError("claude CLI timed out after 180s")
            if proc.returncode != 0:
                err = stderr.decode()[:500]
                raise RuntimeError(f"{'Rate limit hit' if _is_rate_limit_error(err) else 'claude CLI error'}: {err}")
            return stdout.decode().strip()
        except Exception as e:
            if attempt == 4:
                raise
            is_rate_limit = _is_rate_limit_error(str(e))
            wait = min(2 ** (attempt + 1) * (3 if is_rate_limit else 1), 60)
            wait = wait * (0.7 + random.random() * 0.6)  # ±30% jitter
            logger.warning(f"claude CLI attempt {attempt + 1} failed ({'rate limit' if is_rate_limit else 'error'}): {e} — retrying in {wait:.1f}s")
            await asyncio.sleep(wait)
    raise RuntimeError("unreachable")


async def analyze_ticker_async(
    holding: "Holding",
    articles: list,
    signals=None,
    report_date: date | None = None,
    portfolio_name: str = "default",
) -> AnalystReport:
    """Async version of analyze_ticker — use with asyncio.gather for parallel execution."""
    today = report_date or date.today()

    # DB-first: return cached summary if it exists for today
    cached = _load_summary_from_db(holding.ticker, portfolio_name, today)
    if cached is not None:
        logger.info(f"Analyst cache hit: {holding.ticker} → {cached.recommendation} (from DB)")
        return cached

    # Early-exit: no articles and no signals → emit no-data report without LLM call
    if len(articles) == 0 and signals is None:
        logger.info(f"Analyst no-data short-circuit: {holding.ticker} (no articles, no signals)")
        report = AnalystReport(
            ticker=holding.ticker,
            report_date=today,
            article_count=0,
            summary="No news articles or market signals available for this position.",
            sentiment=0.0,
            sentiment_label="Neutral",
            recommendation="HOLD",
            recommendation_context="Insufficient data — maintain current position pending new information.",
            confidence=0.1,
            key_catalysts=[],
            key_risks=["No data available for analysis"],
        )
        _save_report(report, portfolio_name=portfolio_name)
        return report

    current_price = signals.quote.current_price if signals and signals.quote else None
    user_prompt = _build_user_prompt(holding, articles, signals, current_price)

    logger.info(f"Analyst agent (async): {holding.ticker} ({len(articles)} articles)")

    try:
        raw_json = await _call_claude_async(user_prompt)
        raw_json = raw_json.strip()
        if raw_json.startswith("```"):
            raw_json = raw_json.split("```")[1]
            if raw_json.startswith("json"):
                raw_json = raw_json[4:]
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        logger.error(f"Analyst agent JSON parse error for {holding.ticker}: {e}")
        data = {
            "ticker": holding.ticker,
            "summary": "Analysis unavailable due to parsing error.",
            "sentiment": 0.0,
            "sentiment_label": "Neutral",
            "recommendation": "HOLD",
            "recommendation_context": "Insufficient data to make a recommendation.",
            "confidence": 0.1,
            "key_catalysts": [],
            "key_risks": ["Analysis error — manual review recommended"],
        }

    report = AnalystReport(
        ticker=holding.ticker,
        report_date=today,
        article_count=len(articles),
        **{k: v for k, v in data.items() if k != "ticker"},
    )
    # Attach chart URLs from Finviz technicals if available
    if signals and signals.technicals:
        report.chart_daily_url = signals.technicals.chart_daily_url
        report.chart_weekly_url = signals.technicals.chart_weekly_url
    _save_report(report, portfolio_name=portfolio_name)
    logger.info(f"Analyst report: {holding.ticker} → {report.recommendation} ({report.confidence:.0%})")
    return report


async def run_analysts_parallel(
    holdings: list,
    all_articles: dict,
    ticker_signals: dict,
    report_date: date | None = None,
    max_concurrent: int = 5,
    portfolio_name: str = "default",
) -> list[AnalystReport]:
    """Run analyst agents for all holdings concurrently, bounded by a semaphore."""
    sem = asyncio.Semaphore(max_concurrent)

    async def _bounded(holding):
        async with sem:
            return await analyze_ticker_async(
                holding,
                all_articles.get(holding.ticker, []),
                ticker_signals.get(holding.ticker),
                report_date=report_date,
                portfolio_name=portfolio_name,
            )

    return list(await asyncio.gather(*[_bounded(h) for h in holdings]))


def _load_summary_from_db(
    ticker: str, portfolio_name: str, report_date: date
) -> AnalystReport | None:
    """Return a cached AnalystReport reconstructed from the summaries table, or None."""
    if not settings.analyst_cache_enabled:
        return None
    from src.db.models import Summary
    from src.db.session import get_db

    try:
        with get_db() as db:
            row = db.query(Summary).filter_by(
                ticker=ticker,
                portfolio_name=portfolio_name,
                report_date=report_date,
            ).first()
            if row is None:
                return None
            return AnalystReport(
                ticker=row.ticker,
                report_date=row.report_date,
                article_count=row.article_count or 0,
                summary=row.summary,
                sentiment=float(row.sentiment) if row.sentiment is not None else 0.0,
                sentiment_label="Neutral",  # not stored; derive cheaply from value
                recommendation=row.recommendation or "HOLD",
                recommendation_context="",  # not stored in summaries table
                confidence=float(row.confidence) if row.confidence is not None else 0.5,
                key_catalysts=row.key_catalysts or [],
                key_risks=row.key_risks or [],
                analyst_consensus=row.analyst_consensus,
                price_target=float(row.price_target) if row.price_target is not None else None,
                technical_signal=row.technical_signal,
            )
    except Exception as e:
        logger.debug(f"DB summary cache lookup failed for {ticker}: {e}")
        return None


def _save_report(report: AnalystReport, portfolio_name: str = "default") -> None:
    from src.db.models import Summary
    from src.db.session import get_db

    with get_db() as db:
        existing = db.query(Summary).filter_by(
            ticker=report.ticker,
            report_date=report.report_date,
            portfolio_name=portfolio_name,
        ).first()

        if existing:
            existing.summary = report.summary
            existing.sentiment = report.sentiment
            existing.recommendation = report.recommendation
            existing.confidence = report.confidence
            existing.key_catalysts = report.key_catalysts
            existing.key_risks = report.key_risks
            existing.analyst_consensus = report.analyst_consensus
            existing.price_target = report.price_target
            existing.technical_signal = report.technical_signal
            existing.article_count = report.article_count
        else:
            db.add(
                Summary(
                    portfolio_name=portfolio_name,
                    ticker=report.ticker,
                    report_date=report.report_date,
                    summary=report.summary,
                    sentiment=report.sentiment,
                    recommendation=report.recommendation,
                    confidence=report.confidence,
                    key_catalysts=report.key_catalysts,
                    key_risks=report.key_risks,
                    analyst_consensus=report.analyst_consensus,
                    price_target=report.price_target,
                    technical_signal=report.technical_signal,
                    article_count=report.article_count,
                )
            )
