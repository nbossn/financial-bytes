"""Stop-loss alert system.

Reads portfolio.csv (expects optional stop_loss_pct column), fetches current prices
via yfinance, and fires Discord webhook alerts for positions that have breached their
configured threshold.

portfolio.csv format (stop_loss_pct is optional):
    ticker,shares,cost_basis,purchase_date,stop_loss_pct
    GOOG,450,249.51,2025-03-19,-0.15
    MSFT,100,497.20,2025-06-30,-0.20

stop_loss_pct: negative decimal (e.g. -0.15 = alert when price is 15% below cost basis).
If the column is missing or the cell is empty, that position is skipped.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import requests
import yfinance as yf
from loguru import logger


DISCORD_WEBHOOK_URL = os.getenv(
    "DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1497193787900825711/09tGLG_ZzAhtzrXSl3zJpHCRbxlsWYyFWoaEzAYEpRKoi8FSBP1Y40vazPjfyRDzqMFZ",
)


@dataclass
class StopLossCheck:
    ticker: str
    shares: Decimal
    cost_basis: Decimal
    stop_loss_pct: Decimal
    current_price: Decimal

    @property
    def threshold_price(self) -> Decimal:
        return self.cost_basis * (1 + self.stop_loss_pct)

    @property
    def current_pnl_pct(self) -> Decimal:
        return (self.current_price - self.cost_basis) / self.cost_basis * 100

    @property
    def is_triggered(self) -> bool:
        return self.current_price <= self.threshold_price

    @property
    def total_loss(self) -> Decimal:
        return self.shares * (self.current_price - self.cost_basis)


def _load_stop_loss_positions(csv_path: Path) -> list[dict]:
    """Read portfolio.csv rows that have a stop_loss_pct set."""
    positions = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = {c.strip().lower() for c in (reader.fieldnames or [])}
        has_stop_loss_col = "stop_loss_pct" in fieldnames

        for row in reader:
            if not has_stop_loss_col:
                break
            raw = row.get("stop_loss_pct", "").strip()
            if not raw:
                continue
            try:
                stop_pct = Decimal(raw)
                if stop_pct >= 0:
                    logger.warning(f"{row['ticker']}: stop_loss_pct should be negative, got {stop_pct}")
                    continue
                positions.append({
                    "ticker": row["ticker"].strip().upper(),
                    "shares": Decimal(row["shares"].strip()),
                    "cost_basis": Decimal(row["cost_basis"].strip()),
                    "stop_loss_pct": stop_pct,
                })
            except Exception as e:
                logger.warning(f"Skipping row for {row.get('ticker', '?')}: {e}")
    return positions


def _fetch_prices(tickers: list[str]) -> dict[str, Decimal]:
    """Fetch latest close prices via yfinance."""
    prices: dict[str, Decimal] = {}
    for ticker in tickers:
        try:
            hist = yf.Ticker(ticker).history(period="2d")
            if not hist.empty:
                prices[ticker] = Decimal(str(round(hist["Close"].iloc[-1], 4)))
        except Exception as e:
            logger.warning(f"Could not fetch price for {ticker}: {e}")
    return prices


def _send_discord_alert(checks: list[StopLossCheck], portfolio_name: str) -> None:
    """Post stop-loss alert to Discord webhook."""
    if not checks:
        return

    lines = [f"🔴 **Stop-Loss Alert — {portfolio_name}**\n"]
    for c in checks:
        threshold_pct = float(c.stop_loss_pct * 100)
        lines.append(
            f"**{c.ticker}** hit stop-loss threshold\n"
            f"  Cost: ${float(c.cost_basis):.2f} | Current: ${float(c.current_price):.2f} "
            f"({float(c.current_pnl_pct):.1f}%) | Threshold: {threshold_pct:.0f}%\n"
            f"  Position loss: ${float(c.total_loss):,.0f}\n"
        )

    lines.append("\nReview positions — no auto-action taken.")
    content = "\n".join(lines)

    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": content},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"Discord stop-loss alert sent ({len(checks)} triggers)")
    except Exception as e:
        logger.error(f"Discord alert failed: {e}")


def run_stop_loss_check(
    csv_path: str | Path,
    portfolio_name: str = "portfolio",
    send_alert: bool = True,
) -> list[StopLossCheck]:
    """Run stop-loss check for all configured positions.

    Returns:
        List of StopLossCheck objects that triggered (may be empty if none breached).
    """
    csv_path = Path(csv_path)
    positions = _load_stop_loss_positions(csv_path)

    if not positions:
        logger.info("No positions with stop_loss_pct configured — nothing to check")
        return []

    tickers = [p["ticker"] for p in positions]
    prices = _fetch_prices(tickers)

    triggered: list[StopLossCheck] = []
    for pos in positions:
        ticker = pos["ticker"]
        price = prices.get(ticker)
        if price is None:
            logger.warning(f"No price for {ticker} — skipping stop-loss check")
            continue

        check = StopLossCheck(
            ticker=ticker,
            shares=pos["shares"],
            cost_basis=pos["cost_basis"],
            stop_loss_pct=pos["stop_loss_pct"],
            current_price=price,
        )
        status = "TRIGGERED" if check.is_triggered else "ok"
        logger.info(
            f"{ticker}: price=${float(price):.2f}, threshold=${float(check.threshold_price):.2f} "
            f"({float(check.stop_loss_pct*100):.0f}%) → {status}"
        )
        if check.is_triggered:
            triggered.append(check)

    if triggered and send_alert:
        _send_discord_alert(triggered, portfolio_name)
    elif not triggered:
        logger.info("No stop-loss triggers fired")

    return triggered
