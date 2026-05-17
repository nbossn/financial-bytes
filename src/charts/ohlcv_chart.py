"""Interactive OHLCV candlestick chart builder using Plotly.

Generates a self-contained HTML+JS fragment with:
  - Candlestick chart (OHLCV, 1Y of data from yfinance)
  - Time range selector: 1D · 1W · 1M · 3M · 1Y · ALL
  - Signal overlay toggle dropdown: 50-DMA, ATR stop, 52W levels
  - Volume bar subplot
  - Signal summary table below the chart

Usage:
    from src.charts.ohlcv_chart import build_ticker_chart_html

    html_fragment, signal_summary = build_ticker_chart_html("NVDA")
    # Embed html_fragment inside a <div> in your HTML template.
    # signal_summary is a plain-text dict with signal interpretations.
"""
from __future__ import annotations

import warnings
from datetime import date
from typing import Optional

import pandas as pd
import yfinance as yf
from loguru import logger

warnings.filterwarnings("ignore", category=FutureWarning)

# ── Plot dimensions ───────────────────────────────────────────────────────────
CHART_HEIGHT = 520          # px — main chart + volume subplot
LOOKBACK_PERIOD = "365d"    # yfinance period string — 1 full year of candles


# ── Internal data fetch ───────────────────────────────────────────────────────

def _fetch_ohlcv(ticker: str) -> Optional[pd.DataFrame]:
    """Fetch 1Y OHLCV + compute overlays. Returns None on failure."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = yf.Ticker(ticker).history(period=LOOKBACK_PERIOD, auto_adjust=True)

        if df is None or len(df) < 20:
            return None

        df.index = pd.to_datetime(df.index)
        # Ensure tz-naive for Plotly compatibility
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        # Computed overlays
        df["SMA50"] = df["Close"].rolling(50).mean()
        df["SMA20"] = df["Close"].rolling(20).mean()

        # ATR(14) for dynamic stop
        prev_close = df["Close"].shift(1)
        true_range = pd.concat([
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        df["ATR14"] = true_range.rolling(14).mean()
        current_price = float(df["Close"].iloc[-1])
        atr_val = float(df["ATR14"].iloc[-1]) if not pd.isna(df["ATR14"].iloc[-1]) else None
        df["ATR_Stop"] = df["Close"] - 5.0 * df["ATR14"]  # 5× ATR trailing stop

        # ADX(14)
        up_move = df["High"].diff()
        down_move = -df["Low"].diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        alpha = 1.0 / 14
        atr14_ewm = true_range.ewm(alpha=alpha, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr14_ewm.replace(0, float("nan"))
        minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr14_ewm.replace(0, float("nan"))
        di_sum = (plus_di + minus_di).replace(0, float("nan"))
        dx = 100 * (plus_di - minus_di).abs() / di_sum
        adx_series = dx.ewm(alpha=alpha, adjust=False).mean()
        adx_val = float(adx_series.iloc[-1]) if not pd.isna(adx_series.iloc[-1]) else None

        # RSI(14)
        delta = df["Close"].diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, float("nan"))
        df["RSI14"] = 100 - (100 / (1 + rs))
        rsi_val = float(df["RSI14"].iloc[-1]) if not pd.isna(df["RSI14"].iloc[-1]) else None

        # 52-week high/low (from rolling data)
        high_52w = float(df["Close"].max())
        low_52w = float(df["Close"].min())
        pct_from_high = (high_52w - current_price) / high_52w if high_52w > 0 else None
        pct_from_low = (current_price - low_52w) / low_52w if low_52w > 0 else None

        # 50-DMA position
        sma50_latest = float(df["SMA50"].iloc[-1]) if not pd.isna(df["SMA50"].iloc[-1]) else None
        above_50dma = current_price > sma50_latest if sma50_latest else None

        df.attrs.update({
            "ticker": ticker,
            "current_price": current_price,
            "adx": adx_val,
            "rsi": rsi_val,
            "atr": atr_val,
            "high_52w": high_52w,
            "low_52w": low_52w,
            "sma50": sma50_latest,
            "above_50dma": above_50dma,
            "pct_from_high": pct_from_high,
            "pct_from_low": pct_from_low,
        })
        return df

    except Exception as e:
        logger.warning(f"ohlcv_chart: data fetch failed for {ticker}: {e}")
        return None


def _signal_summary(df: pd.DataFrame, recommendation: str = "HOLD") -> dict:
    """Return a dict of signal interpretations for the summary table."""
    a = df.attrs
    adx = a.get("adx")
    rsi = a.get("rsi")
    above_50dma = a.get("above_50dma")
    pct_from_high = a.get("pct_from_high")
    pct_from_low = a.get("pct_from_low")
    is_long = recommendation in ("BUY",)

    signals = {}

    # ADX
    if adx is not None:
        if adx >= 25:
            signals["ADX"] = {"value": f"{adx:.1f}", "label": "Confirmed Trend",
                              "color": "#16a34a",
                              "impact": "reinforces" if is_long else "strengthens momentum context"}
        elif adx >= 20:
            signals["ADX"] = {"value": f"{adx:.1f}", "label": "Weak Trend",
                              "color": "#d97706", "impact": "neutral — momentum signals less reliable"}
        else:
            signals["ADX"] = {"value": f"{adx:.1f}", "label": "Choppy / Sideways",
                              "color": "#dc2626",
                              "impact": "weakens stop signals — suppress momentum tightening"}

    # RSI
    if rsi is not None:
        if rsi >= 70:
            signals["RSI(14)"] = {"value": f"{rsi:.1f}", "label": "Overbought",
                                  "color": "#dc2626",
                                  "impact": "weakens long entry" if is_long else "supports short"}
        elif rsi <= 30:
            signals["RSI(14)"] = {"value": f"{rsi:.1f}", "label": "Oversold",
                                  "color": "#16a34a",
                                  "impact": "reinforces long entry" if is_long else "mean reversion risk"}
        else:
            signals["RSI(14)"] = {"value": f"{rsi:.1f}", "label": "Neutral",
                                  "color": "#64748b", "impact": "no directional bias"}

    # 50-DMA
    if above_50dma is not None:
        sma = a.get("sma50")
        price = a.get("current_price")
        pct = abs(price - sma) / sma * 100 if sma else 0
        if above_50dma:
            signals["50-DMA"] = {"value": f"{pct:.1f}% above",
                                 "label": "Bullish",
                                 "color": "#16a34a",
                                 "impact": "reinforces long" if is_long else "resistance to short"}
        else:
            signals["50-DMA"] = {"value": f"{pct:.1f}% below",
                                 "label": "Bearish Momentum",
                                 "color": "#dc2626",
                                 "impact": "weakens long" if is_long else "confirms short momentum"}

    # 52W position
    if pct_from_high is not None and pct_from_low is not None:
        if pct_from_high <= 0.05:
            signals["52W Range"] = {"value": f"{pct_from_high*100:.1f}% below high",
                                    "label": "Near 52W High",
                                    "color": "#d97706",
                                    "impact": "resistance zone — anchoring bias, tighten stop"}
        elif pct_from_low <= 0.10:
            signals["52W Range"] = {"value": f"{pct_from_low*100:.1f}% above low",
                                    "label": "Near 52W Low",
                                    "color": "#7c3aed",
                                    "impact": "underreaction zone — George & Hwang upward bias (looser stop)"}
        else:
            mid_pct = (1 - pct_from_high) * 100
            signals["52W Range"] = {"value": f"{mid_pct:.0f}% of 52W range",
                                    "label": "Mid-Range",
                                    "color": "#64748b", "impact": "neutral positioning"}

    return signals


def _build_plotly_html(df: pd.DataFrame, ticker: str) -> str:
    """Build and return Plotly chart as HTML string (no full page, cdn Plotly.js)."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        logger.warning("ohlcv_chart: plotly not installed — chart skipped")
        return ""

    dates = df.index

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.72, 0.28],
        # No subplot_titles — they land at y≈1.0 and collide with rangeselector + modebar.
        # Ticker label is injected as a layout annotation below instead.
    )

    # ── Candlestick ──────────────────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=dates,
        open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"],
        name="Price",
        increasing=dict(line=dict(color="#22c55e"), fillcolor="#bbf7d0"),
        decreasing=dict(line=dict(color="#ef4444"), fillcolor="#fecaca"),
        showlegend=False,
    ), row=1, col=1)

    # ── 50-DMA ────────────────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=dates, y=df["SMA50"],
        name="50-DMA", mode="lines",
        line=dict(color="#f59e0b", width=1.8, dash="dot"),
    ), row=1, col=1)

    # ── 20-DMA ────────────────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=dates, y=df["SMA20"],
        name="20-DMA", mode="lines",
        line=dict(color="#a78bfa", width=1.2, dash="dot"),
        visible="legendonly",
    ), row=1, col=1)

    # ── ATR Trailing Stop (5× ATR) ────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=dates, y=df["ATR_Stop"],
        name="ATR Stop (5×)", mode="lines",
        line=dict(color="#ef4444", width=1.2, dash="dash"),
    ), row=1, col=1)

    # ── 52W High ─────────────────────────────────────────────────────────────
    high_52w = df.attrs.get("high_52w")
    low_52w = df.attrs.get("low_52w")
    if high_52w:
        fig.add_hline(
            y=high_52w, line=dict(color="#6366f1", width=1, dash="longdash"),
            annotation_text=f"52W High ${high_52w:.2f}",
            annotation_position="top right",
            row=1, col=1,
        )
    if low_52w:
        fig.add_hline(
            y=low_52w, line=dict(color="#8b5cf6", width=1, dash="longdash"),
            annotation_text=f"52W Low ${low_52w:.2f}",
            annotation_position="bottom right",
            row=1, col=1,
        )

    # ── Volume bars ───────────────────────────────────────────────────────────
    colors = [
        "#22c55e" if row["Close"] >= row["Open"] else "#ef4444"
        for _, row in df.iterrows()
    ]
    fig.add_trace(go.Bar(
        x=dates, y=df["Volume"],
        name="Volume", marker_color=colors, showlegend=False,
        opacity=0.6,
    ), row=2, col=1)

    # ── Layout ────────────────────────────────────────────────────────────────
    # Top margin = 100px so the rangeselector (y=1.06), ticker annotation, and
    # Plotly modebar (always top-right, ~28px) all have dedicated vertical space
    # without overlapping. Legend lives INSIDE the plot area to stay clear of
    # the modebar entirely.
    fig.update_layout(
        height=CHART_HEIGHT,
        margin=dict(l=60, r=50, t=100, b=30),
        paper_bgcolor="#ffffff",
        plot_bgcolor="#f8fafc",
        font=dict(family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif", size=11),
        # Legend inside the plot — top-left corner, well away from modebar
        legend=dict(
            orientation="v",
            x=0.01, y=0.98,
            xanchor="left", yanchor="top",
            bgcolor="rgba(255,255,255,0.88)",
            bordercolor="#e2e8f0", borderwidth=1,
            font=dict(size=11),
            tracegroupgap=4,
        ),
        # Ticker + chart label as annotation (replaces subplot_titles)
        annotations=[
            dict(
                text=f"<b>{ticker}</b>  ·  Price & Signals",
                x=0, y=1.0,
                xref="paper", yref="paper",
                xanchor="left", yanchor="bottom",
                showarrow=False,
                font=dict(size=12, color="#1e293b",
                          family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"),
            ),
            dict(
                text="Volume",
                x=0, y=0.26,
                xref="paper", yref="paper",
                xanchor="left", yanchor="bottom",
                showarrow=False,
                font=dict(size=11, color="#64748b",
                          family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"),
            ),
        ],
        xaxis=dict(
            rangeselector=dict(
                buttons=[
                    dict(count=1,  label="1D",  step="day",   stepmode="backward"),
                    dict(count=5,  label="1W",  step="day",   stepmode="backward"),
                    dict(count=1,  label="1M",  step="month", stepmode="backward"),
                    dict(count=3,  label="3M",  step="month", stepmode="backward"),
                    dict(count=1,  label="1Y",  step="year",  stepmode="backward"),
                    dict(step="all", label="ALL"),
                ],
                bgcolor="#f1f5f9",
                activecolor="#3b82f6",
                bordercolor="#cbd5e1",
                borderwidth=1,
                font=dict(size=11, color="#1e293b"),
                # x=0.5 centres the buttons; y=1.07 sits above the chart area and
                # the ticker annotation, below the figure top (modebar space)
                x=0.5, xanchor="center",
                y=1.07, yanchor="bottom",
            ),
            rangeslider=dict(visible=False),
            showgrid=True, gridcolor="#e2e8f0", gridwidth=0.5,
        ),
        xaxis2=dict(showgrid=True, gridcolor="#e2e8f0"),
        yaxis=dict(
            showgrid=True, gridcolor="#e2e8f0",
            tickprefix="$", tickformat=",.2f",
        ),
        yaxis2=dict(
            showgrid=False,
            tickformat=".2s",
        ),
        hovermode="x unified",
        dragmode="pan",
    )

    fig.update_traces(xaxis="x1")

    div_id = f"chart_{ticker.lower().replace('-', '_')}"
    return fig.to_html(
        include_plotlyjs=False,   # injected once per page in template
        full_html=False,
        div_id=div_id,
        config={
            "displayModeBar": True,
            "scrollZoom": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "displaylogo": False,
        },
    )


def _signal_summary_html(signals: dict) -> str:
    """Render signal summary as an HTML table fragment."""
    if not signals:
        return ""

    rows = []
    for name, info in signals.items():
        color = info.get("color", "#64748b")
        rows.append(f"""
        <tr>
          <td style="padding:5px 10px;font-weight:600;color:#374151;font-size:12px;">{name}</td>
          <td style="padding:5px 10px;font-weight:700;color:{color};font-size:12px;">{info['label']}</td>
          <td style="padding:5px 10px;font-size:11.5px;color:#374151;">{info['value']}</td>
          <td style="padding:5px 10px;font-size:11px;color:#64748b;font-style:italic;">{info['impact']}</td>
        </tr>""")

    return f"""<div style="margin-top:12px;border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;">
  <div style="background:#f1f5f9;padding:7px 12px;font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#475569;">
    Signal Summary
  </div>
  <table style="width:100%;border-collapse:collapse;">
    <thead>
      <tr style="border-bottom:1px solid #e2e8f0;">
        <th style="padding:5px 10px;text-align:left;font-size:10px;color:#94a3b8;font-weight:600;">Signal</th>
        <th style="padding:5px 10px;text-align:left;font-size:10px;color:#94a3b8;font-weight:600;">Reading</th>
        <th style="padding:5px 10px;text-align:left;font-size:10px;color:#94a3b8;font-weight:600;">Value</th>
        <th style="padding:5px 10px;text-align:left;font-size:10px;color:#94a3b8;font-weight:600;">Impact on Play</th>
      </tr>
    </thead>
    <tbody>{"".join(rows)}
    </tbody>
  </table>
</div>"""


# ── Public API ────────────────────────────────────────────────────────────────

def build_ticker_chart_html(ticker: str, recommendation: str = "HOLD") -> str:
    """Build an interactive Plotly candlestick chart for a ticker.

    Returns a self-contained HTML fragment (chart div + signal summary).
    Requires Plotly.js to be loaded on the page (add the CDN script once).

    The fragment includes:
    - Interactive candlestick chart with 1Y of OHLCV data
    - Time window selector: 1D / 1W / 1M / 3M / 1Y / ALL
    - Signal overlays: 50-DMA, 20-DMA, ATR trailing stop, 52W high/low
    - Signal summary table: ADX regime, RSI, 50-DMA position, 52W position
    - Impact column: how each signal reinforces or weakens the play

    Args:
        ticker:         Stock symbol.
        recommendation: "BUY" | "HOLD" | "SELL" — affects impact text in summary.

    Returns:
        HTML string. Empty string on data fetch failure.
    """
    ticker = ticker.upper().strip()
    logger.info(f"ohlcv_chart: building chart for {ticker}…")

    df = _fetch_ohlcv(ticker)
    if df is None:
        logger.warning(f"ohlcv_chart: no data for {ticker} — skipping chart")
        return ""

    chart_html = _build_plotly_html(df, ticker)
    if not chart_html:
        return ""

    signals = _signal_summary(df, recommendation)
    summary_html = _signal_summary_html(signals)

    return f"""<div class="interactive-chart" style="margin-top:16px;">
  {chart_html}
  {summary_html}
</div>"""


PLOTLY_CDN_SCRIPT = (
    '<script src="https://cdn.plot.ly/plotly-3.1.0.min.js" '
    'charset="utf-8" referrerpolicy="no-referrer"></script>'
)
