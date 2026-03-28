"""Financial Director Agent — portfolio synthesis using claude-sonnet-4-6."""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import subprocess

from jinja2 import Template
from loguru import logger
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from src.agents.analyst_agent import AnalystReport
from src.config import settings
from src.portfolio.models import PortfolioSnapshot

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "director_system.txt").read_text()
USER_PROMPT_TEMPLATE = (Path(__file__).parent / "prompts" / "director_user.txt").read_text()

MODEL = "claude-sonnet-4-6"


class StockSignal(BaseModel):
    ticker: str
    signal: str | None = None       # BUY/HOLD/SELL for opportunities
    risk: str | None = None         # risk description
    rationale: str | None = None
    short_term: str | None = None
    long_term: str | None = None
    severity: str | None = None
    mitigation: str | None = None


class DirectorReport(BaseModel):
    report_date: date
    market_theme: str
    five_min_summary: str
    portfolio_summary: str
    global_market_context: str
    top_opportunities: list[StockSignal] = Field(default_factory=list)
    top_risks: list[StockSignal] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    overall_sentiment: float = Field(ge=-1.0, le=1.0)
    overall_recommendation: str


def _get_global_market_context() -> str:
    """Fetch global market context: Asia close, London open, US futures.
    Falls back to a note if data unavailable."""
    try:
        from src.api.massive_client import MassiveClient
        from src.api.endpoints import MassiveEndpoints

        with MassiveClient() as client:
            endpoints = MassiveEndpoints(client)
            # Key indices as proxies
            context_parts = []
            for symbol, name in [("SPY", "S&P 500 ETF"), ("QQQ", "Nasdaq ETF"), ("IWM", "Russell 2000 ETF")]:
                quote = endpoints.get_quote(symbol)
                if quote and quote.day_change_pct is not None:
                    direction = "▲" if quote.day_change_pct >= 0 else "▼"
                    context_parts.append(f"{name}: {direction}{abs(quote.day_change_pct):.2f}%")

            if context_parts:
                return "US Premarket/Early Trading: " + " | ".join(context_parts)
    except Exception as e:
        logger.debug(f"Could not fetch market context: {e}")

    return "Global market context unavailable — refer to your brokerage for pre-market data."


def _build_user_prompt(
    snapshot: PortfolioSnapshot,
    analyst_reports: list[AnalystReport],
    global_context: str,
) -> str:
    template = Template(USER_PROMPT_TEMPLATE)

    total_pnl = snapshot.total_pnl
    total_pnl_pct = snapshot.total_pnl_pct

    return template.render(
        report_date=date.today().strftime("%A, %B %d, %Y"),
        total_cost=f"{snapshot.total_cost:,.2f}",
        total_value=f"{snapshot.total_value:,.2f}",
        total_pnl_pct=f"{total_pnl_pct:+.1f}",
        total_pnl_dollars=f"{total_pnl:+,.2f}",
        global_market_context=global_context,
        analyst_reports=analyst_reports,
    )


_RATE_LIMIT_SIGNALS = ("rate limit", "429", "overloaded", "529")


def _is_rate_limit_error(stderr: str) -> bool:
    low = stderr.lower()
    return any(s in low for s in _RATE_LIMIT_SIGNALS)


@retry(
    wait=wait_exponential(multiplier=2, min=5, max=120),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _call_claude(user_prompt: str) -> str:
    result = subprocess.run(
        [
            "claude", "-p", user_prompt,
            "--model", MODEL,
            "--system-prompt", SYSTEM_PROMPT,
            "--dangerously-skip-permissions",
        ],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        err = result.stderr[:500]
        if _is_rate_limit_error(err):
            raise RuntimeError(f"Rate limit hit: {err}")
        raise RuntimeError(f"claude CLI error: {err}")
    return result.stdout.strip()


def synthesize_portfolio(
    snapshot: PortfolioSnapshot,
    analyst_reports: list[AnalystReport],
    report_date: date | None = None,
) -> DirectorReport:
    """Run the director agent to synthesize all analyst reports into a portfolio brief."""
    today = report_date or date.today()
    global_context = _get_global_market_context()
    user_prompt = _build_user_prompt(snapshot, analyst_reports, global_context)

    logger.info(f"Director agent: synthesizing {len(analyst_reports)} analyst reports")

    try:
        raw_json = _call_claude(user_prompt)
        raw_json = raw_json.strip()
        if raw_json.startswith("```"):
            raw_json = raw_json.split("```")[1]
            if raw_json.startswith("json"):
                raw_json = raw_json[4:]
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        logger.error(f"Director agent JSON parse error: {e}")
        data = {
            "market_theme": "Analysis unavailable",
            "five_min_summary": "Director synthesis failed — please review individual stock reports below.",
            "portfolio_summary": f"Portfolio value: ${snapshot.total_value:,.2f}",
            "global_market_context": global_context,
            "top_opportunities": [],
            "top_risks": [],
            "action_items": ["Manual review recommended — director synthesis error"],
            "overall_sentiment": 0.0,
            "overall_recommendation": "HOLD — insufficient data for recommendation",
        }

    # Parse nested objects
    opportunities = [StockSignal(**o) for o in data.get("top_opportunities", [])]
    risks = [StockSignal(**r) for r in data.get("top_risks", [])]

    report = DirectorReport(
        report_date=today,
        market_theme=data.get("market_theme", ""),
        five_min_summary=data.get("five_min_summary", ""),
        portfolio_summary=data.get("portfolio_summary", ""),
        global_market_context=data.get("global_market_context", global_context),
        top_opportunities=opportunities,
        top_risks=risks,
        action_items=data.get("action_items", []),
        overall_sentiment=float(data.get("overall_sentiment", 0.0)),
        overall_recommendation=data.get("overall_recommendation", ""),
    )

    _save_report(report)
    logger.info(f"Director report complete: theme='{report.market_theme[:60]}...' sentiment={report.overall_sentiment}")
    return report


def _save_report(report: DirectorReport) -> None:
    from src.db.models import Recommendation
    from src.db.session import get_db

    with get_db() as db:
        existing = db.query(Recommendation).filter_by(report_date=report.report_date).first()
        data = dict(
            market_theme=report.market_theme,
            five_min_summary=report.five_min_summary,
            portfolio_summary=report.portfolio_summary,
            global_market_context=report.global_market_context,
            action_items=report.action_items,
            top_opportunities=[o.model_dump() for o in report.top_opportunities],
            top_risks=[r.model_dump() for r in report.top_risks],
            overall_sentiment=report.overall_sentiment,
        )
        if existing:
            for k, v in data.items():
                setattr(existing, k, v)
        else:
            db.add(Recommendation(report_date=report.report_date, **data))
