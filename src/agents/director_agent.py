"""Financial Director Agent — portfolio synthesis using claude-sonnet-4-6."""
import json
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path

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


def _extract_prior_newsletter_summary(html_path: Path) -> str | None:
    """Extract a short plain-text summary from a previous newsletter HTML file."""
    if not html_path.exists():
        return None
    try:
        from bs4 import BeautifulSoup
        html = html_path.read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "lxml")
        # Pull the five-min summary and action items sections if present
        chunks = []
        for tag in soup.select(".five-min-summary, .action-items, .section"):
            text = tag.get_text(separator=" ", strip=True)
            if len(text) > 40:
                chunks.append(text[:400])
            if len(chunks) >= 4:
                break
        if not chunks:
            # Fallback: first 600 chars of body text
            body_text = soup.get_text(separator=" ", strip=True)
            return body_text[:600] if len(body_text) > 100 else None
        return "\n\n".join(chunks)
    except Exception as e:
        logger.debug(f"Could not parse prior newsletter: {e}")
        return None


def _load_analyst_summaries_from_db(
    portfolio_name: str,
    report_date: "date",
    tickers: list[str] | None = None,
) -> list[dict]:
    """Return compact analyst summary dicts from the summaries table.

    Each dict has: ticker, recommendation, confidence, sentiment, summary,
    key_catalysts, key_risks, analyst_consensus, price_target, technical_signal.
    """
    from src.db.models import Summary
    from src.db.session import get_db

    with get_db() as db:
        q = db.query(Summary).filter_by(
            portfolio_name=portfolio_name,
            report_date=report_date,
        )
        if tickers:
            q = q.filter(Summary.ticker.in_(tickers))
        # Build dicts inside the session context to avoid DetachedInstanceError
        return [
            {
                "ticker": r.ticker,
                "recommendation": r.recommendation or "HOLD",
                "confidence": float(r.confidence) if r.confidence is not None else 0.5,
                "sentiment": float(r.sentiment) if r.sentiment is not None else 0.0,
                "summary": r.summary,
                "key_catalysts": r.key_catalysts or [],
                "key_risks": r.key_risks or [],
                "analyst_consensus": r.analyst_consensus,
                "price_target": float(r.price_target) if r.price_target is not None else None,
                "technical_signal": r.technical_signal,
            }
            for r in q.all()
        ]


def _build_user_prompt(
    snapshot: PortfolioSnapshot,
    analyst_reports: "list[AnalystReport] | list[dict]",
    global_context: str,
    prior_newsletter_summary: str | None = None,
) -> str:
    template = Template(USER_PROMPT_TEMPLATE)

    total_pnl = snapshot.total_pnl
    total_pnl_pct = snapshot.total_pnl_pct
    tax = snapshot.tax_summary

    return template.render(
        report_date=date.today().strftime("%A, %B %d, %Y"),
        total_cost=f"{snapshot.total_cost:,.2f}",
        total_value=f"{snapshot.total_value:,.2f}",
        total_pnl_pct=f"{total_pnl_pct:+.1f}",
        total_pnl_dollars=f"{total_pnl:+,.2f}",
        global_market_context=global_context,
        analyst_reports=analyst_reports,
        tax_summary=tax,
        prior_newsletter_summary=prior_newsletter_summary,
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
    """Call the claude CLI, piping the prompt via stdin to avoid OS ARG_MAX limits.

    Large prompts (e.g. 300+ analyst reports) exceed Linux's per-argument size
    cap when passed as a -p argument. Passing '-p -' and writing to stdin is
    unbounded and uses the same CLI session auth as analyst agents.
    """
    cmd = ["claude", "-p", "-", "--model", MODEL, "--system-prompt", SYSTEM_PROMPT]
    if settings.claude_skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    result = subprocess.run(
        cmd,
        input=user_prompt,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        err = result.stderr[:500]
        if _is_rate_limit_error(err):
            raise RuntimeError(f"Rate limit hit: {err}")
        raise RuntimeError(f"claude CLI error (rc={result.returncode}): {err}")
    return result.stdout.strip()


def synthesize_portfolio(
    snapshot: PortfolioSnapshot,
    analyst_reports: list[AnalystReport] | None = None,   # None = read from DB
    report_date: date | None = None,
    portfolio_name: str = "default",
    prior_newsletter_path: Path | None = None,
) -> DirectorReport:
    """Run the director agent to synthesize all analyst reports into a portfolio brief.

    When analyst_reports is None (or empty), summaries are loaded from the DB for the given
    portfolio_name/report_date.  This allows the director to run without any in-memory
    analyst results — critical for the DB-first resumable pipeline.
    """
    today = report_date or date.today()
    global_context = _get_global_market_context()

    prior_summary: str | None = None
    if prior_newsletter_path:
        prior_summary = _extract_prior_newsletter_summary(prior_newsletter_path)
        if prior_summary:
            logger.info(f"Director: loaded prior newsletter context from {prior_newsletter_path.name}")

    # Resolve analyst data: prefer DB when in-memory list is absent/empty
    reports_for_prompt: list[AnalystReport] | list[dict]
    if analyst_reports:
        reports_for_prompt = analyst_reports
        logger.info(f"Director agent: synthesizing {len(analyst_reports)} analyst reports (in-memory)")
    else:
        tickers = [h.ticker for h in snapshot.holdings]
        db_summaries = _load_analyst_summaries_from_db(portfolio_name, today, tickers)
        if db_summaries:
            reports_for_prompt = db_summaries
            logger.info(
                f"Director agent: synthesizing {len(db_summaries)} analyst reports "
                f"(from DB — {portfolio_name}/{today})"
            )
        else:
            # No in-memory reports and no DB summaries — proceed with empty list
            # (director prompt handles gracefully; signals a cold run with no analyst data)
            reports_for_prompt = []
            logger.warning(
                f"Director agent: no analyst reports available "
                f"(in-memory or DB) for {portfolio_name}/{today}"
            )

    user_prompt = _build_user_prompt(snapshot, reports_for_prompt, global_context, prior_summary)

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

    _save_report(report, portfolio_name=portfolio_name)
    logger.info(f"Director report complete: theme='{report.market_theme[:60]}...' sentiment={report.overall_sentiment}")
    return report


def _save_report(report: DirectorReport, portfolio_name: str = "default") -> None:
    from src.db.models import Recommendation
    from src.db.session import get_db

    with get_db() as db:
        existing = db.query(Recommendation).filter_by(
            report_date=report.report_date,
            portfolio_name=portfolio_name,
        ).first()
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
            db.add(Recommendation(report_date=report.report_date, portfolio_name=portfolio_name, **data))
