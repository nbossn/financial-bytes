"""Managing Director Agent — synthesizes all analysis into actionable trade plays."""
from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from src.agents.analyst_agent import AnalystReport
from src.agents.quant_agent import QuantReport
from src.api.models import FinvizAnalystRating, FinvizFundamentals, InsiderTrade
from src.config import settings

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "md_system.txt").read_text()
USER_PROMPT_TEMPLATE = (Path(__file__).parent / "prompts" / "md_user.txt").read_text()

MODEL = "claude-sonnet-4-6"


class TradePlay(BaseModel):
    play_type: str
    time_horizon: str                   # short-term / swing / position
    thesis: str
    entry: str
    target: str
    stop_loss: str
    position_size: str
    specific_structure: str
    risk_reward: str
    conviction: str                     # High / Medium / Low


class KeyLevels(BaseModel):
    strong_support: str | None = None
    resistance: str | None = None
    breakout_trigger: str | None = None


class MDReport(BaseModel):
    ticker: str
    report_date: date
    md_thesis: str
    overall_stance: str                 # Aggressive Long .. Aggressive Short
    conviction: str                     # High / Medium / Low
    plays: list[TradePlay] = Field(default_factory=list)
    key_levels: KeyLevels = Field(default_factory=KeyLevels)
    macro_considerations: str
    insider_warning: str | None = None
    position_management: str


def _fmt_analyst_ratings(ratings: list[FinvizAnalystRating]) -> str:
    if not ratings:
        return "No recent analyst ratings."
    lines = []
    for r in ratings[:10]:
        pt = f" → ${r.price_target:.0f}" if r.price_target else ""
        lines.append(f"  {r.date}: {r.analyst} — {r.action} | {r.rating_change}{pt}")
    return "\n".join(lines)


def _fmt_insider_trades(trades: list[InsiderTrade]) -> str:
    if not trades:
        return "No insider trade data."
    lines = []
    for t in trades[:15]:
        if t.shares and t.cost:
            val = f"${t.value_usd:,.0f}" if t.value_usd else ""
            lines.append(f"  {t.date}: {t.name} ({t.relationship}) — {t.transaction} {t.shares:,.0f} shares @ ${t.cost:.2f} {val}")
        else:
            lines.append(f"  {t.date}: {t.name} — {t.transaction}")
    return "\n".join(lines)


def _fmt_fundamentals(f: FinvizFundamentals | None) -> str:
    if not f:
        return "No fundamental data."
    parts = []
    for label, attr in [
        ("P/S", "ps_ratio"), ("P/B", "pb_ratio"), ("EV/Sales", "ev_sales"),
        ("Gross Margin", "gross_margin"), ("Oper Margin", "oper_margin"),
        ("Net Margin", "profit_margin"), ("ROE", "roe"), ("ROIC", "roic"),
        ("Revenue Growth", "sales_yoy_ttm"), ("EPS Growth", "eps_yoy_ttm"),
        ("Debt/Eq", "debt_eq"), ("Cash/Sh", "cash_per_share"),
    ]:
        val = getattr(f, attr, None)
        if val is not None:
            pct = "%" if attr not in ("ps_ratio", "pb_ratio", "ev_sales", "debt_eq", "cash_per_share") else ""
            parts.append(f"{label}: {val:+.1f}{pct}" if pct else f"{label}: {val:.2f}")
    return " | ".join(parts) or "No fundamental data."


def _build_prompt(
    ticker: str,
    analyst_report: AnalystReport,
    quant_report: QuantReport,
    fundamentals: FinvizFundamentals | None,
    finviz_ratings: list[FinvizAnalystRating],
    insider_trades: list[InsiderTrade],
) -> str:
    f = fundamentals

    def _s(v, default="N/A") -> str:
        return str(v) if v is not None else default

    prompt = USER_PROMPT_TEMPLATE
    replacements = {
        "{{ ticker }}": ticker,
        "{{ company_name }}": ticker,
        "{{ current_price }}": _s(f.current_price_raw if f else None),
        "{{ low_52w }}": _s(f.low_52w if f else None),
        "{{ high_52w }}": _s(f.high_52w if f else None),
        "{{ market_cap }}": _s(f.market_cap_text if f else None),
        "{{ avg_volume }}": _s(f.avg_volume_text if f else None),
        "{{ options_available }}": _s(f.option_short if f else None, "Unknown"),
        "{{ shortable }}": _s(f.option_short if f else None, "Unknown"),
        "{{ analyst_ratings_text }}": _fmt_analyst_ratings(finviz_ratings),
        "{{ risk_profile }}": quant_report.risk_profile,
        "{{ return_quality }}": quant_report.return_quality,
        "{{ beta }}": _s(quant_report.beta),
        "{{ alpha }}": _s(quant_report.alpha_annualized),
        "{{ sharpe }}": _s(quant_report.sharpe_ratio),
        "{{ sortino }}": _s(quant_report.sortino_ratio),
        "{{ max_drawdown }}": _s(quant_report.max_drawdown),
        "{{ current_drawdown }}": _s(quant_report.max_drawdown),
        "{{ momentum_signal }}": quant_report.momentum_signal,
        "{{ short_squeeze_risk }}": quant_report.short_squeeze_risk,
        "{{ insider_signal }}": quant_report.insider_signal,
        "{{ quant_flags_text }}": "\n".join(f"- {f}" for f in quant_report.key_quant_flags),
        "{{ analyst_summary }}": analyst_report.summary,
        "{{ analyst_recommendation }}": analyst_report.recommendation,
        "{{ analyst_confidence }}": f"{analyst_report.confidence:.0%}",
        "{{ catalysts_text }}": ", ".join(analyst_report.key_catalysts[:4]),
        "{{ risks_text }}": ", ".join(analyst_report.key_risks[:4]),
        "{{ fundamentals_text }}": _fmt_fundamentals(fundamentals),
        "{{ insider_text }}": _fmt_insider_trades(insider_trades),
        "{{ ratings_text }}": _fmt_analyst_ratings(finviz_ratings),
    }
    for k, v in replacements.items():
        prompt = prompt.replace(k, str(v))
    return prompt


@retry(wait=wait_exponential(multiplier=2, min=5, max=60), stop=stop_after_attempt(4), reraise=True)
def _call_claude(user_prompt: str) -> str:
    cmd = ["claude", "-p", user_prompt, "--model", MODEL, "--system-prompt", SYSTEM_PROMPT]
    if settings.claude_skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI error: {result.stderr[:500]}")
    return result.stdout.strip()


def run_md_agent(
    ticker: str,
    analyst_report: AnalystReport,
    quant_report: QuantReport,
    fundamentals: FinvizFundamentals | None = None,
    finviz_ratings: list[FinvizAnalystRating] | None = None,
    insider_trades: list[InsiderTrade] | None = None,
    report_date: date | None = None,
) -> MDReport:
    """Run the Managing Director agent and return trade plays."""
    today = report_date or date.today()
    finviz_ratings = finviz_ratings or []
    insider_trades = insider_trades or []
    logger.info(f"MD agent: generating plays for {ticker}")

    user_prompt = _build_prompt(
        ticker, analyst_report, quant_report, fundamentals, finviz_ratings, insider_trades
    )

    try:
        raw = _call_claude(user_prompt).strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
    except (json.JSONDecodeError, RuntimeError) as e:
        logger.error(f"MD agent failed for {ticker}: {e}")
        data = {
            "md_thesis": f"Analysis unavailable: {e}",
            "overall_stance": "Neutral",
            "conviction": "Low",
            "plays": [],
            "key_levels": {},
            "macro_considerations": "N/A",
            "insider_warning": None,
            "position_management": "Hold current position pending further data.",
        }

    plays = [TradePlay(**p) for p in data.pop("plays", [])]
    key_levels = KeyLevels(**data.pop("key_levels", {}))

    report = MDReport(
        ticker=ticker,
        report_date=today,
        plays=plays,
        key_levels=key_levels,
        **{k: v for k, v in data.items() if k not in ("ticker",)},
    )
    logger.info(
        f"MD report: {ticker} — stance={report.overall_stance}, "
        f"conviction={report.conviction}, plays={len(report.plays)}"
    )
    return report
