"""Portfolio-weighted treemap — Finviz Map-style visual, sized by portfolio
weight (not market cap) and colored by day change %.

Nick asked for this directly (Discord, 2026-09-05): "similar visuals in the
daily reports specific to the stocks that I have in my portfolio and their
relative sizes to portfolio percentage." Local data only — no scraping,
reuses the same Plotly embedding convention as src.charts.ohlcv_chart (CDN
script injected once per page; each fragment uses include_plotlyjs=False).

Usage:
    from src.charts.portfolio_treemap import build_portfolio_treemap_html
    html_fragment = build_portfolio_treemap_html(snapshot)
"""
from __future__ import annotations

import warnings
from typing import Optional

import plotly.graph_objects as go
import yfinance as yf
from loguru import logger

from src.portfolio.models import PortfolioSnapshot

TREEMAP_HEIGHT = 480


def _day_change_pct(tickers: list[str]) -> dict[str, float]:
    """Batch-fetch today's % change per ticker (last close vs prior close).

    Best-effort: a ticker that fails to resolve is left out of the returned
    dict rather than defaulted to 0 — zeroing it would misreport "unknown"
    as "flat," which is a real color on this chart, not an absence of one.
    """
    out: dict[str, float] = {}
    if not tickers:
        return out
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            data = yf.download(tickers, period="5d", progress=False,
                                group_by="ticker", auto_adjust=True, threads=True)
    except Exception as e:
        logger.warning(f"portfolio_treemap: batch day-change fetch failed: {e}")
        return out

    for ticker in tickers:
        try:
            closes = (data[ticker]["Close"] if len(tickers) > 1 else data["Close"]).dropna()
            if len(closes) < 2:
                continue
            prev, last = closes.iloc[-2], closes.iloc[-1]
            if prev:
                out[ticker] = float((last - prev) / prev * 100)
        except Exception:
            continue
    return out


def build_portfolio_treemap_html(snapshot: PortfolioSnapshot,
                                  day_changes: Optional[dict[str, float]] = None) -> str:
    """Build a treemap sized by portfolio weight (position value / total
    portfolio value) and colored by day change %, matching Finviz's Map page
    but scoped to Nick's actual holdings instead of the whole market.

    Pass `day_changes` (ticker -> % change) to skip the live yfinance fetch —
    e.g. when the caller already pulled today's quotes elsewhere in the
    pipeline, avoiding a redundant network round-trip.
    """
    holdings = [h for h in snapshot.holdings if h.ticker in snapshot.prices]
    if not holdings:
        logger.warning("portfolio_treemap: no priced holdings to plot")
        return ""

    values = {h.ticker: float(h.current_value(snapshot.prices[h.ticker])) for h in holdings}
    total_value = sum(values.values())
    if total_value <= 0:
        return ""

    if day_changes is None:
        day_changes = _day_change_pct(list(values.keys()))

    tickers = list(values.keys())
    weights_pct = [values[t] / total_value * 100 for t in tickers]
    changes = [day_changes.get(t) for t in tickers]

    # Diverging scale hand-centered at 0 rather than trusting Plotly's
    # auto-range — a portfolio that's all-green (or all-red) on a given day
    # would otherwise get its midpoint auto-scaled away from 0, making every
    # tile's color relative-to-today's-spread instead of an absolute read.
    known = [c for c in changes if c is not None]
    bound = max(1.0, max((abs(c) for c in known), default=1.0))
    color_values = [c if c is not None else 0.0 for c in changes]
    text = [f"{w:.1f}% of book<br>{'n/a' if c is None else f'{c:+.2f}%'}"
            for w, c in zip(weights_pct, changes)]

    fig = go.Figure(go.Treemap(
        labels=tickers,
        parents=[""] * len(tickers),
        values=weights_pct,
        text=text,
        textinfo="label+text",
        hoverinfo="label+text",
        textfont=dict(size=14),
        marker=dict(
            colors=color_values,
            colorscale="RdYlGn",
            cmid=0,
            cmin=-bound,
            cmax=bound,
            showscale=True,
            colorbar=dict(title="Day %", len=0.6),
        ),
    ))
    fig.update_layout(
        height=TREEMAP_HEIGHT,
        margin=dict(l=4, r=4, t=24, b=4),
        title=dict(text="Portfolio — sized by weight, colored by day change", font=dict(size=14)),
    )

    chart_html = fig.to_html(include_plotlyjs=False, full_html=False)
    return f'<div class="portfolio-treemap" style="margin-top:16px;">\n  {chart_html}\n</div>'
