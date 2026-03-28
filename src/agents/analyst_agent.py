"""Financial Analyst Agent — per-ticker article analysis using claude-haiku-4-5."""
import asyncio
import json
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
        "{{ article_count }}": str(len(articles)),
        "{{ articles_text }}": _format_articles(articles),
    }
    for key, val in replacements.items():
        prompt = prompt.replace(key, val)
    return prompt


@retry(
    wait=wait_exponential(multiplier=1, min=1, max=32),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _call_claude(user_prompt: str) -> str:
    result = subprocess.run(
        ["claude", "-p", user_prompt, "--system-prompt", SYSTEM_PROMPT],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI error: {result.stderr[:500]}")
    return result.stdout.strip()


def analyze_ticker(
    holding: Holding,
    articles: list[ScrapedArticle],
    signals: TickerSignals | None = None,
    report_date: date | None = None,
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

    _save_report(report)
    logger.info(f"Analyst report: {holding.ticker} → {report.recommendation} (confidence: {report.confidence:.0%})")
    return report


async def _call_claude_async(user_prompt: str) -> str:
    """Async subprocess wrapper for claude -p with retry."""
    for attempt in range(5):
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p", user_prompt, "--system-prompt", SYSTEM_PROMPT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError("claude CLI timed out after 180s")
            if proc.returncode != 0:
                raise RuntimeError(f"claude CLI error: {stderr.decode()[:500]}")
            return stdout.decode().strip()
        except Exception as e:
            if attempt == 4:
                raise
            wait = min(2 ** attempt, 32)
            logger.warning(f"claude CLI attempt {attempt + 1} failed: {e} — retrying in {wait}s")
            await asyncio.sleep(wait)
    raise RuntimeError("unreachable")


async def analyze_ticker_async(
    holding: "Holding",
    articles: list,
    signals=None,
    report_date: date | None = None,
) -> AnalystReport:
    """Async version of analyze_ticker — use with asyncio.gather for parallel execution."""
    today = report_date or date.today()
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
    _save_report(report)
    logger.info(f"Analyst report: {holding.ticker} → {report.recommendation} ({report.confidence:.0%})")
    return report


async def run_analysts_parallel(
    holdings: list,
    all_articles: dict,
    ticker_signals: dict,
    report_date: date | None = None,
    max_concurrent: int = 5,
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
            )

    return list(await asyncio.gather(*[_bounded(h) for h in holdings]))


def _save_report(report: AnalystReport) -> None:
    from src.db.models import Summary
    from src.db.session import get_db

    with get_db() as db:
        existing = db.query(Summary).filter_by(
            ticker=report.ticker, report_date=report.report_date
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
