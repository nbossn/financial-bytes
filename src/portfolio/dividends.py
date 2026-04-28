"""Dividend and income tracking for portfolio positions.

Fetches dividend yield, ex-dividend dates, and annual income projections
from yfinance for all holdings. Used by the newsletter pipeline to surface
income-related signals.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import yfinance as yf
from loguru import logger

from src.portfolio.models import Holding


@dataclass
class DividendInfo:
    ticker: str
    annual_dividend_per_share: Decimal
    dividend_yield_pct: Decimal
    ex_dividend_date: date | None
    days_to_ex_div: int | None
    annual_income: Decimal

    @property
    def ex_div_soon(self) -> bool:
        """Returns True if ex-dividend date is within 14 days."""
        return self.days_to_ex_div is not None and 0 <= self.days_to_ex_div <= 14


def fetch_dividend_info(holding: Holding, current_price: Decimal) -> DividendInfo | None:
    """Fetch dividend data from yfinance for a single holding.

    Returns None if the ticker pays no dividend or data is unavailable.
    """
    try:
        ticker = yf.Ticker(holding.ticker)
        info = ticker.info

        # yfinance returns dividendRate in dollars per share (annual)
        div_rate = info.get("dividendRate") or 0.0
        ex_div_ts = info.get("exDividendDate")

        if div_rate == 0 or float(current_price) == 0:
            return None

        annual_div = Decimal(str(round(div_rate, 4)))
        # Calculate yield from rate/price (avoids yfinance inconsistency in dividendYield field)
        yield_pct = (annual_div / current_price * 100).quantize(Decimal("0.001"))
        annual_income = holding.shares * annual_div

        ex_div_date: date | None = None
        days_to_ex: int | None = None
        if ex_div_ts:
            try:
                import datetime as dt
                ex_div_date = dt.datetime.fromtimestamp(ex_div_ts).date()
                days_to_ex = (ex_div_date - date.today()).days
            except Exception:
                pass

        return DividendInfo(
            ticker=holding.ticker,
            annual_dividend_per_share=annual_div,
            dividend_yield_pct=yield_pct,
            ex_dividend_date=ex_div_date,
            days_to_ex_div=days_to_ex,
            annual_income=annual_income,
        )

    except Exception as e:
        logger.warning(f"Could not fetch dividend data for {holding.ticker}: {e}")
        return None


def fetch_portfolio_dividends(
    holdings: list[Holding],
    prices: dict[str, Decimal],
) -> list[DividendInfo]:
    """Fetch dividend info for all holdings that pay dividends.

    Returns only holdings with a non-zero dividend. Sorted by annual income (desc).
    """
    results = []
    for holding in holdings:
        price = prices.get(holding.ticker, holding.cost_basis)
        info = fetch_dividend_info(holding, price)
        if info is not None:
            results.append(info)
            logger.debug(
                f"{holding.ticker}: ${float(info.annual_dividend_per_share):.2f}/sh "
                f"({float(info.dividend_yield_pct):.2f}%), "
                f"annual income=${float(info.annual_income):,.0f}"
            )

    results.sort(key=lambda d: d.annual_income, reverse=True)
    return results


def format_dividend_section(dividend_infos: list[DividendInfo]) -> str:
    """Format dividend data as a Markdown section for the newsletter."""
    if not dividend_infos:
        return ""

    total_annual = sum(d.annual_income for d in dividend_infos)
    total_monthly = total_annual / 12

    lines = [
        "## Dividend & Income Summary\n",
        f"**Projected annual income:** ${float(total_annual):,.0f} "
        f"(${float(total_monthly):,.0f}/mo)\n",
        "",
        "| Ticker | Div/sh | Yield | Ex-Div Date | Annual Income |",
        "|--------|--------|-------|-------------|---------------|",
    ]

    for d in dividend_infos:
        ex_str = ""
        if d.ex_dividend_date:
            ex_str = d.ex_dividend_date.strftime("%b %d")
            if d.ex_div_soon:
                ex_str += " ⚡"
        else:
            ex_str = "—"

        lines.append(
            f"| {d.ticker} | ${float(d.annual_dividend_per_share):.2f} "
            f"| {float(d.dividend_yield_pct):.1f}% "
            f"| {ex_str} "
            f"| ${float(d.annual_income):,.0f} |"
        )

    upcoming = [d for d in dividend_infos if d.ex_div_soon]
    if upcoming:
        lines.append("")
        lines.append("**⚡ Ex-dividend dates within 14 days:**")
        for d in upcoming:
            lines.append(
                f"- **{d.ticker}** ex-div {d.ex_dividend_date} "
                f"({d.days_to_ex_div} days) — ${float(d.annual_dividend_per_share):.2f}/sh"
            )

    return "\n".join(lines)
