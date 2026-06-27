"""
risk.py — Per-name risk classification.

Places each candidate on a risk spectrum from Conservative to Speculative using
fundamental + market characteristics. This lets the final report recommend a
SPECTRUM of plays (the user asked for plays across P/E, revenue, speculation).

Risk tiers
----------
CONSERVATIVE : profitable, reasonable valuation, low beta, large cap, dividend
MODERATE     : quality growth, moderate valuation/beta
AGGRESSIVE   : high growth or high beta or rich valuation, still fundamentally real
SPECULATIVE  : unprofitable / extreme valuation / high short interest / micro-mid cap
                or no fundamentals available

The classifier is a transparent point system so a human (or Fable) can see exactly
why a name landed in a tier. Each factor contributes points; the total maps to a tier.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RiskProfile:
    ticker: str
    tier: str                       # CONSERVATIVE | MODERATE | AGGRESSIVE | SPECULATIVE
    score: float                    # 0 (safest) .. 100 (most speculative)
    reasons: list[str] = field(default_factory=list)
    # raw inputs (for transparency / the report)
    trailing_pe: float | None = None
    forward_pe: float | None = None
    market_cap: float | None = None
    beta: float | None = None
    revenue_growth: float | None = None
    profit_margin: float | None = None
    short_pct_float: float | None = None
    dividend_yield: float | None = None
    sector: str | None = None


def _b(x) -> float | None:
    try:
        if x is None:
            return None
        xf = float(x)
        if xf != xf:  # nan
            return None
        return xf
    except (TypeError, ValueError):
        return None


def classify_risk(ticker: str, info: dict) -> RiskProfile:
    """Classify one ticker from its yfinance `info` dict into a risk tier.

    Higher score = more speculative. Each factor adds points with a short reason.
    """
    pe = _b(info.get("trailingPE"))
    fpe = _b(info.get("forwardPE"))
    mcap = _b(info.get("marketCap"))
    beta = _b(info.get("beta"))
    rev_growth = _b(info.get("revenueGrowth"))
    margin = _b(info.get("profitMargins"))
    short_pct = _b(info.get("shortPercentOfFloat"))
    div_yield = _b(info.get("dividendYield"))
    sector = info.get("sector")

    score = 0.0
    reasons: list[str] = []

    # --- Valuation (P/E) ---
    eff_pe = pe if pe is not None else fpe
    if eff_pe is None:
        score += 22
        reasons.append("no P/E (unprofitable or no earnings)")
    elif eff_pe < 0:
        score += 25
        reasons.append(f"negative earnings (P/E {eff_pe:.0f})")
    elif eff_pe > 60:
        score += 20
        reasons.append(f"very rich valuation (P/E {eff_pe:.0f})")
    elif eff_pe > 35:
        score += 12
        reasons.append(f"elevated valuation (P/E {eff_pe:.0f})")
    elif eff_pe > 22:
        score += 5
        reasons.append(f"moderate valuation (P/E {eff_pe:.0f})")
    else:
        reasons.append(f"reasonable valuation (P/E {eff_pe:.0f})")

    # --- Profitability ---
    if margin is None:
        score += 8
    elif margin < 0:
        score += 18
        reasons.append(f"unprofitable (margin {margin*100:.0f}%)")
    elif margin < 0.05:
        score += 8
        reasons.append(f"thin margin ({margin*100:.0f}%)")
    elif margin > 0.20:
        reasons.append(f"strong margin ({margin*100:.0f}%)")

    # --- Market cap (size) ---
    if mcap is None:
        score += 10
    elif mcap < 2e9:
        score += 20
        reasons.append("small/micro cap (<$2B)")
    elif mcap < 1e10:
        score += 10
        reasons.append("mid cap ($2-10B)")
    elif mcap < 5e10:
        score += 4
        reasons.append("large cap ($10-50B)")
    else:
        reasons.append("mega cap (>$50B)")

    # --- Beta (volatility vs market) ---
    if beta is None:
        score += 3
    elif beta > 1.8:
        score += 14
        reasons.append(f"high beta ({beta:.1f})")
    elif beta > 1.3:
        score += 7
        reasons.append(f"elevated beta ({beta:.1f})")
    elif beta < 0.8:
        reasons.append(f"low beta ({beta:.1f})")

    # --- Revenue growth (reward growth slightly reduces risk score) ---
    if rev_growth is not None:
        if rev_growth > 0.30:
            reasons.append(f"high revenue growth ({rev_growth*100:.0f}%)")
            score -= 4
        elif rev_growth < 0:
            score += 8
            reasons.append(f"declining revenue ({rev_growth*100:.0f}%)")

    # --- Short interest (crowding / squeeze risk) ---
    if short_pct is not None and short_pct > 0.10:
        score += 12
        reasons.append(f"high short interest ({short_pct*100:.0f}% of float)")

    # --- Dividend (reduces risk slightly) ---
    if div_yield is not None and div_yield > 0.015:
        score -= 4
        reasons.append(f"pays dividend ({div_yield*100:.1f}%)")

    score = max(0.0, min(100.0, score))

    if score < 18:
        tier = "CONSERVATIVE"
    elif score < 35:
        tier = "MODERATE"
    elif score < 55:
        tier = "AGGRESSIVE"
    else:
        tier = "SPECULATIVE"

    return RiskProfile(
        ticker=ticker, tier=tier, score=score, reasons=reasons,
        trailing_pe=pe, forward_pe=fpe, market_cap=mcap, beta=beta,
        revenue_growth=rev_growth, profit_margin=margin,
        short_pct_float=short_pct, dividend_yield=div_yield, sector=sector,
    )
