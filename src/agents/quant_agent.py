"""Quantitative Analyst Agent — statistical analysis: beta, alpha, Sharpe, Sortino, etc."""
from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from src.api.models import FinvizFundamentals, InsiderTrade, QuantMetrics
from src.config import settings

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "quant_system.txt").read_text()
USER_PROMPT_TEMPLATE = (Path(__file__).parent / "prompts" / "quant_user.txt").read_text()

MODEL = "claude-sonnet-4-6"


class QuantReport(BaseModel):
    ticker: str
    report_date: date
    quant_summary: str
    risk_profile: str              # Very Low / Low / Moderate / High / Very High
    risk_profile_rationale: str
    return_quality: str            # Exceptional / Strong / Moderate / Weak / Poor
    beta_interpretation: str
    alpha_interpretation: str
    momentum_signal: str           # Strong Uptrend / Uptrend / Neutral / Downtrend / Strong Downtrend
    momentum_rationale: str
    drawdown_assessment: str
    insider_signal: str            # Bullish / Neutral / Bearish
    short_squeeze_risk: str        # Low / Moderate / High
    key_quant_flags: list[str] = Field(default_factory=list)
    fair_value_note: str = ""
    # Raw metrics passthrough for reference
    beta: float | None = None
    alpha_annualized: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    annualized_return: float | None = None
    annualized_volatility: float | None = None
    max_drawdown: float | None = None
    momentum_1m: float | None = None
    momentum_3m: float | None = None
    momentum_6m: float | None = None
    rsi_14: float | None = None


def _format_fundamentals(f: FinvizFundamentals | None) -> str:
    if not f:
        return "No fundamental data available."
    parts = []
    if f.market_cap_text:
        parts.append(f"Market Cap: {f.market_cap_text}")
    if f.enterprise_value_text:
        parts.append(f"EV: {f.enterprise_value_text}")
    if f.ps_ratio is not None:
        parts.append(f"P/S: {f.ps_ratio:.2f}")
    if f.pb_ratio is not None:
        parts.append(f"P/B: {f.pb_ratio:.2f}")
    if f.pe_ratio is not None:
        parts.append(f"P/E: {f.pe_ratio:.1f}")
    if f.forward_pe is not None:
        parts.append(f"Fwd P/E: {f.forward_pe:.1f}")
    if f.ev_ebitda is not None:
        parts.append(f"EV/EBITDA: {f.ev_ebitda:.1f}")
    if f.ev_sales is not None:
        parts.append(f"EV/Sales: {f.ev_sales:.2f}")
    if f.profit_margin is not None:
        parts.append(f"Net Margin: {f.profit_margin:.1f}%")
    if f.gross_margin is not None:
        parts.append(f"Gross Margin: {f.gross_margin:.1f}%")
    if f.oper_margin is not None:
        parts.append(f"Oper Margin: {f.oper_margin:.1f}%")
    if f.roe is not None:
        parts.append(f"ROE: {f.roe:.1f}%")
    if f.roa is not None:
        parts.append(f"ROA: {f.roa:.1f}%")
    if f.roic is not None:
        parts.append(f"ROIC: {f.roic:.1f}%")
    if f.sales_yoy_ttm is not None:
        parts.append(f"Revenue Growth YoY: {f.sales_yoy_ttm:+.1f}%")
    if f.eps_yoy_ttm is not None:
        parts.append(f"EPS Growth YoY: {f.eps_yoy_ttm:+.1f}%")
    if f.sales_qoq is not None:
        parts.append(f"Revenue QoQ: {f.sales_qoq:+.1f}%")
    if f.eps_next_5y is not None:
        parts.append(f"EPS Growth next 5Y est: {f.eps_next_5y:+.1f}%")
    if f.debt_eq is not None:
        parts.append(f"Debt/Equity: {f.debt_eq:.2f}")
    if f.current_ratio is not None:
        parts.append(f"Current Ratio: {f.current_ratio:.2f}")
    if f.cash_per_share is not None:
        parts.append(f"Cash/Share: ${f.cash_per_share:.2f}")
    return " | ".join(parts) or "No fundamental data available."


def _format_insider_summary(trades: list[InsiderTrade]) -> str:
    if not trades:
        return "No insider trades data."
    buys = [t for t in trades if t.transaction and "Buy" in t.transaction]
    sales = [t for t in trades if t.transaction and "Sale" in t.transaction]
    buy_value = sum(t.value_usd or 0 for t in buys)
    sell_value = sum(t.value_usd or 0 for t in sales)
    recent = trades[:5]
    lines = [f"Buys: {len(buys)} (${ buy_value:,.0f}), Sales: {len(sales)} (${sell_value:,.0f}) in look-back period"]
    for t in recent:
        lines.append(f"  {t.date}: {t.name} ({t.relationship}) — {t.transaction} {t.shares:,.0f} shares @ ${t.cost:.2f}" if t.shares and t.cost else f"  {t.date}: {t.name} — {t.transaction}")
    return "\n".join(lines)


def _format_short_summary(f: FinvizFundamentals | None) -> str:
    if not f:
        return "No short data."
    parts = []
    if f.short_float is not None:
        parts.append(f"Short Float: {f.short_float:.1f}%")
    if f.short_ratio is not None:
        parts.append(f"Short Ratio / Days-to-Cover: {f.short_ratio:.1f}")
    if f.short_interest_text:
        parts.append(f"Short Interest: {f.short_interest_text}")
    return " | ".join(parts) or "No short data."


def _build_prompt(
    ticker: str,
    quant: QuantMetrics,
    fundamentals: FinvizFundamentals | None,
    insider_trades: list[InsiderTrade],
) -> str:
    def _fmt(v, suffix="", default="N/A") -> str:
        return f"{v}{suffix}" if v is not None else default

    prompt = USER_PROMPT_TEMPLATE
    replacements = {
        "{{ ticker }}": ticker,
        "{{ period_days }}": str(quant.period_days),
        "{{ benchmark }}": quant.benchmark,
        "{{ annualized_return }}": _fmt(quant.annualized_return),
        "{{ annualized_volatility }}": _fmt(quant.annualized_volatility),
        "{{ beta }}": _fmt(quant.beta),
        "{{ alpha }}": _fmt(quant.alpha_annualized),
        "{{ r_squared }}": _fmt(quant.r_squared),
        "{{ correlation }}": _fmt(quant.correlation),
        "{{ sharpe_ratio }}": _fmt(quant.sharpe_ratio),
        "{{ sortino_ratio }}": _fmt(quant.sortino_ratio),
        "{{ max_drawdown }}": _fmt(quant.max_drawdown),
        "{{ current_drawdown }}": _fmt(quant.current_drawdown),
        "{{ rsi_14 }}": _fmt(quant.rsi_14),
        "{{ momentum_1m }}": _fmt(quant.momentum_1m),
        "{{ momentum_3m }}": _fmt(quant.momentum_3m),
        "{{ momentum_6m }}": _fmt(quant.momentum_6m),
        "{{ fundamentals_text }}": _format_fundamentals(fundamentals),
        "{{ insider_summary }}": _format_insider_summary(insider_trades),
        "{{ short_summary }}": _format_short_summary(fundamentals),
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


def run_quant_agent(
    ticker: str,
    quant_metrics: QuantMetrics,
    fundamentals: FinvizFundamentals | None = None,
    insider_trades: list[InsiderTrade] | None = None,
    report_date: date | None = None,
) -> QuantReport:
    """Run the quantitative analyst agent and return a QuantReport."""
    today = report_date or date.today()
    insider_trades = insider_trades or []
    logger.info(f"Quant agent: analyzing {ticker}")

    user_prompt = _build_prompt(ticker, quant_metrics, fundamentals, insider_trades)

    try:
        raw = _call_claude(user_prompt).strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
    except (json.JSONDecodeError, RuntimeError) as e:
        logger.error(f"Quant agent failed for {ticker}: {e}")
        data = {
            "quant_summary": "Quantitative analysis unavailable.",
            "risk_profile": "Unknown",
            "risk_profile_rationale": "Analysis error.",
            "return_quality": "Unknown",
            "beta_interpretation": "N/A",
            "alpha_interpretation": "N/A",
            "momentum_signal": "Neutral",
            "momentum_rationale": "N/A",
            "drawdown_assessment": "N/A",
            "insider_signal": "Neutral",
            "short_squeeze_risk": "Low",
            "key_quant_flags": [str(e)],
            "fair_value_note": "",
        }

    report = QuantReport(
        ticker=ticker,
        report_date=today,
        beta=quant_metrics.beta,
        alpha_annualized=quant_metrics.alpha_annualized,
        sharpe_ratio=quant_metrics.sharpe_ratio,
        sortino_ratio=quant_metrics.sortino_ratio,
        annualized_return=quant_metrics.annualized_return,
        annualized_volatility=quant_metrics.annualized_volatility,
        max_drawdown=quant_metrics.max_drawdown,
        momentum_1m=quant_metrics.momentum_1m,
        momentum_3m=quant_metrics.momentum_3m,
        momentum_6m=quant_metrics.momentum_6m,
        rsi_14=quant_metrics.rsi_14,
        **{k: v for k, v in data.items() if k != "ticker"},
    )
    logger.info(
        f"Quant report: {ticker} — risk={report.risk_profile}, "
        f"momentum={report.momentum_signal}, short_squeeze={report.short_squeeze_risk}"
    )
    return report
