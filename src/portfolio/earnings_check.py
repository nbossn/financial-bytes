"""April 29 earnings decision engine.

Maps reported segment metrics to pre-commit action rules for GOOG/GOOGL, MSFT, and AMZN.
Segment revenue isn't available via yfinance; user provides the one number per company.

Usage (CLI):
    financial-bytes earnings-check                           # interactive
    financial-bytes earnings-check --goog 18.5              # GOOG cloud revenue $18.5B
    financial-bytes earnings-check --azure 39.2             # MSFT Azure growth rate 39.2%
    financial-bytes earnings-check --aws 29.8               # AMZN AWS revenue $29.8B
    financial-bytes earnings-check --goog 18.5 --azure 39.2 --aws 29.8  # all at once

Where to get the numbers (~5 PM ET on April 29):
    GOOG  — Search "Alphabet earnings Google Cloud revenue Q1 2026" → earnings press release
    MSFT  — "Azure growth constant currency" from MSFT earnings call / press release
    AMZN  — "AWS revenue Q1 2026" from Amazon earnings press release
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ── Decision rules ──────────────────────────────────────────────────────────

@dataclass
class PortfolioAction:
    nbossn: str
    lilich: str


@dataclass
class Threshold:
    min_val: Optional[float]   # None means "below the previous threshold" (catch-all low)
    max_val: Optional[float]   # None means "above the previous threshold" (catch-all high)
    label: str
    emoji: str
    nbossn: str
    lilich: str


GOOG_RULES: list[Threshold] = [
    Threshold(
        min_val=19.0, max_val=None,
        label="Beat", emoji="🟢",
        nbossn="Hold all 900sh. Raise mental stop to +45% P&L.",
        lilich="Hold. Consider adding on any post-earnings dip.",
    ),
    Threshold(
        min_val=18.0, max_val=19.0,
        label="In-line", emoji="🟡",
        nbossn="Hold all 900sh. No action.",
        lilich="Hold. No action.",
    ),
    Threshold(
        min_val=17.5, max_val=18.0,
        label="Slight miss", emoji="🟡",
        nbossn="Hold. Monitor Q2 guidance language — trim only if guidance is cut.",
        lilich="Hold.",
    ),
    Threshold(
        min_val=17.0, max_val=17.5,
        label="Miss", emoji="🔴",
        nbossn="Trim 100–150sh GOOG + 100–150sh GOOGL. Redeploy into META (RSI ~61).",
        lilich="Trim 150–200sh GOOG ST lots first (highest tax cost per dollar).",
    ),
    Threshold(
        min_val=None, max_val=17.0,
        label="Miss + guidance cut", emoji="🔴🔴",
        nbossn="Trim 200sh each (GOOG + GOOGL). Bring combined to ~60% of portfolio gains.",
        lilich="Aggressive trim — 250–300sh ST lots. Consider timing vs. LTCG threshold.",
    ),
]

MSFT_RULES: list[Threshold] = [
    Threshold(
        min_val=39.0, max_val=None,
        label="Beat (≥39%)", emoji="🟢",
        nbossn="No MSFT in nbossn (harvested). No action.",
        lilich="Hold all lots. May 1 blackout opens — clean window for LTCG trim if you want concentration reduction.",
    ),
    Threshold(
        min_val=37.0, max_val=39.0,
        label="In-line (37–39%)", emoji="🟡",
        nbossn="No action.",
        lilich="Hold. Re-evaluate May 1 window for voluntary concentration reduction.",
    ),
    Threshold(
        min_val=35.0, max_val=37.0,
        label="Slight miss (35–37%)", emoji="🟡",
        nbossn="No action.",
        lilich="Hold but plan May 1 trim. Start with Lot 1 + Lot 9 or 10 (~$94/sh gain each).",
    ),
    Threshold(
        min_val=None, max_val=35.0,
        label="Miss (<35%)", emoji="🔴",
        nbossn="No action.",
        lilich="Trim 3–4 lots starting May 1 (post-blackout). Lots 1, 9, 10 first. Target: MSFT < 60% of lilich portfolio.",
    ),
]

AMZN_RULES: list[Threshold] = [
    Threshold(
        min_val=30.0, max_val=None,
        label="Beat (>$30B)", emoji="🟢",
        nbossn="No AMZN in nbossn. No action.",
        lilich="Hold. Confirm lilich position size with fresh Fidelity data.",
    ),
    Threshold(
        min_val=28.5, max_val=30.0,
        label="In-line ($28.5–30B)", emoji="🟡",
        nbossn="No action.",
        lilich="Hold. No action.",
    ),
    Threshold(
        min_val=27.5, max_val=28.5,
        label="Miss ($27.5–28.5B)", emoji="🟡",
        nbossn="No action.",
        lilich="Evaluate once Fidelity CSV is refreshed. Hold if conviction intact.",
    ),
    Threshold(
        min_val=None, max_val=27.5,
        label="Miss + margin compression", emoji="🔴",
        nbossn="No action.",
        lilich="Consider trimming based on lilich concentration. Flag for Nick's review.",
    ),
]

COMPANIES: dict[str, dict] = {
    "GOOG": {
        "name": "Alphabet (GOOG/GOOGL)",
        "metric_name": "Google Cloud Revenue",
        "unit": "$B",
        "expected_time": "~4:30 PM ET",
        "where_to_find": (
            "Google 'Alphabet Q1 2026 earnings' → press release PDF, "
            "'Google Cloud' segment line"
        ),
        "rules": GOOG_RULES,
        "consensus": 18.4,
        "consensus_note": "Street consensus ~$18.4B (+28% YoY)",
    },
    "MSFT": {
        "name": "Microsoft",
        "metric_name": "Azure Growth (constant currency)",
        "unit": "%",
        "expected_time": "~5:30 PM ET",
        "where_to_find": (
            "Microsoft earnings press release or call transcript — "
            "look for 'Azure and other cloud services grew X% in constant currencies'"
        ),
        "rules": MSFT_RULES,
        "consensus": 37.5,
        "consensus_note": "Street consensus ~37–38% CC growth",
    },
    "AMZN": {
        "name": "Amazon",
        "metric_name": "AWS Revenue",
        "unit": "$B",
        "expected_time": "~4:30 PM ET",
        "where_to_find": (
            "Amazon Q1 2026 press release → 'Net Sales by Segment' table, "
            "AWS row"
        ),
        "rules": AMZN_RULES,
        "consensus": 29.0,
        "consensus_note": "Street consensus ~$29.0B (+18% YoY)",
    },
}


# ── Rule matching ────────────────────────────────────────────────────────────

def match_rule(value: float, rules: list[Threshold]) -> Threshold:
    """Return the matching threshold rule for a given metric value."""
    for rule in rules:
        if rule.min_val is None:
            # Catch-all low: value < max_val
            if rule.max_val is not None and value < rule.max_val:
                return rule
        elif rule.max_val is None:
            # Catch-all high: value >= min_val
            if value >= rule.min_val:
                return rule
        else:
            if rule.min_val <= value < rule.max_val:
                return rule
    # Fallback: return the last rule (lowest threshold)
    return rules[-1]


# ── Report generation ────────────────────────────────────────────────────────

def _fmt_value(value: float, unit: str) -> str:
    if unit == "$B":
        return f"${value:.1f}B"
    elif unit == "%":
        return f"{value:.1f}%"
    return str(value)


def generate_report(results: dict[str, float]) -> str:
    """Generate a plain-text decision report from provided metric values.

    results: dict mapping company key ("GOOG", "MSFT", "AMZN") to reported value.
    """
    lines: list[str] = [
        "=" * 60,
        "  APRIL 29 EARNINGS — DECISION REPORT",
        "=" * 60,
        "",
    ]

    for company_key, value in results.items():
        company = COMPANIES[company_key]
        rule = match_rule(value, company["rules"])
        reported_str = _fmt_value(value, company["unit"])
        consensus_str = _fmt_value(company["consensus"], company["unit"])
        vs_consensus = value - company["consensus"]
        vs_str = f"+{vs_consensus:.1f}" if vs_consensus >= 0 else f"{vs_consensus:.1f}"

        lines += [
            f"── {company['name']} ──────────────────────────────",
            f"  {company['metric_name']}: {reported_str}  "
            f"(vs consensus {consensus_str}, {vs_str}{company['unit']})",
            f"  Verdict: {rule.emoji} {rule.label}",
            "",
            f"  nbossn: {rule.nbossn}",
            f"  lilich: {rule.lilich}",
            "",
        ]

    lines += [
        "=" * 60,
        "Sector watch: if ANY company mentions 'AI capex ROI uncertainty'",
        "or 'infrastructure spending re-evaluated', treat all hold rules",
        "as trim rules. That language means sector repricing, not stock-specific miss.",
        "=" * 60,
    ]

    return "\n".join(lines)


def print_lookup_guide() -> None:
    """Print where to find each metric when earnings land."""
    print("\n📋 WHERE TO GET THE NUMBERS (~5 PM ET)\n")
    for key, company in COMPANIES.items():
        print(f"  {key} ({company['metric_name']}):")
        print(f"    Reports: {company['expected_time']}")
        print(f"    Find it: {company['where_to_find']}")
        print(f"    Consensus: {company['consensus_note']}")
        print()
