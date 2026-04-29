"""Nightly portfolio performance tracking — snapshots, time-series, SPY comparison.

Saves a daily P&L snapshot to SQLite. Callable from the pipeline (auto-runs after
each newsletter generation) or standalone via `financial-bytes track-performance`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import yfinance as yf
from loguru import logger
from sqlalchemy.exc import IntegrityError

from src.db.models import PortfolioPerformanceSnapshot
from src.db.session import get_db
from src.portfolio.models import PortfolioSnapshot


@dataclass
class PerformanceRecord:
    portfolio_name: str
    snapshot_date: date
    total_cost: Decimal
    total_value: Decimal
    total_pnl: Decimal
    total_pnl_pct: Decimal
    spy_price: Decimal | None
    spy_pnl_pct: Decimal | None
    position_count: int

    @property
    def vs_spy(self) -> Decimal | None:
        """Portfolio outperformance vs SPY (positive = beating market)."""
        if self.spy_pnl_pct is None:
            return None
        return self.total_pnl_pct - self.spy_pnl_pct


def _fetch_spy_data() -> tuple[Decimal | None, Decimal | None]:
    """Return (current_price, pct_change_since_year_start) for SPY.

    We use YTD return as a rough benchmark baseline since portfolio
    purchase dates vary widely. Returns (None, None) on failure.
    """
    try:
        spy = yf.Ticker("SPY")
        info = spy.info
        current_price = info.get("regularMarketPrice") or info.get("currentPrice")
        if not current_price:
            return None, None

        # YTD baseline: SPY price at start of current year
        year_start = date(date.today().year, 1, 1)
        hist = spy.history(start=year_start.isoformat(), end=date.today().isoformat(), interval="1d")
        if hist.empty:
            return Decimal(str(round(current_price, 4))), None

        ytd_start_price = float(hist["Close"].iloc[0])
        spy_pnl_pct = ((current_price - ytd_start_price) / ytd_start_price) * 100
        return (
            Decimal(str(round(current_price, 4))),
            Decimal(str(round(spy_pnl_pct, 4))),
        )
    except Exception as e:
        logger.warning(f"Could not fetch SPY benchmark data: {e}")
        return None, None


def take_snapshot(
    snapshot: PortfolioSnapshot,
    portfolio_name: str,
    snapshot_date: date | None = None,
) -> PerformanceRecord:
    """Compute today's performance record from a live PortfolioSnapshot."""
    as_of = snapshot_date or date.today()
    spy_price, spy_pnl_pct = _fetch_spy_data()

    record = PerformanceRecord(
        portfolio_name=portfolio_name,
        snapshot_date=as_of,
        total_cost=snapshot.total_cost,
        total_value=snapshot.total_value,
        total_pnl=snapshot.total_pnl,
        total_pnl_pct=snapshot.total_pnl_pct,
        spy_price=spy_price,
        spy_pnl_pct=spy_pnl_pct,
        position_count=len(snapshot.holdings),
    )
    return record


def save_snapshot(record: PerformanceRecord, upsert: bool = True) -> bool:
    """Persist a PerformanceRecord to the SQLite DB.

    If a snapshot for this portfolio+date already exists and upsert=True,
    updates it in place. Returns True if saved, False on error.
    """
    with get_db() as db:
        existing = (
            db.query(PortfolioPerformanceSnapshot)
            .filter_by(portfolio_name=record.portfolio_name, snapshot_date=record.snapshot_date)
            .first()
        )
        if existing and upsert:
            existing.total_cost = record.total_cost
            existing.total_value = record.total_value
            existing.total_pnl = record.total_pnl
            existing.total_pnl_pct = record.total_pnl_pct
            existing.spy_price = record.spy_price
            existing.spy_pnl_pct = record.spy_pnl_pct
            existing.position_count = record.position_count
            try:
                db.commit()
                logger.info(
                    f"[perf] Updated snapshot for {record.portfolio_name} on {record.snapshot_date}"
                )
                return True
            except Exception as e:
                db.rollback()
                logger.error(f"[perf] Failed to update snapshot: {e}")
                return False

        row = PortfolioPerformanceSnapshot(
            portfolio_name=record.portfolio_name,
            snapshot_date=record.snapshot_date,
            total_cost=record.total_cost,
            total_value=record.total_value,
            total_pnl=record.total_pnl,
            total_pnl_pct=record.total_pnl_pct,
            spy_price=record.spy_price,
            spy_pnl_pct=record.spy_pnl_pct,
            position_count=record.position_count,
        )
        try:
            db.add(row)
            db.commit()
            logger.info(
                f"[perf] Saved snapshot for {record.portfolio_name} on {record.snapshot_date}: "
                f"${float(record.total_value):,.0f} / {float(record.total_pnl_pct):+.1f}%"
            )
            return True
        except IntegrityError:
            db.rollback()
            logger.warning(
                f"[perf] Snapshot already exists for {record.portfolio_name} on {record.snapshot_date} "
                f"— use upsert=True to overwrite"
            )
            return False
        except Exception as e:
            db.rollback()
            logger.error(f"[perf] Failed to save snapshot: {e}")
            return False


def get_history(
    portfolio_name: str,
    days: int = 30,
) -> list[PerformanceRecord]:
    """Retrieve historical performance snapshots, oldest first."""
    cutoff = date.today() - timedelta(days=days)
    with get_db() as db:
        rows = (
            db.query(PortfolioPerformanceSnapshot)
            .filter(
                PortfolioPerformanceSnapshot.portfolio_name == portfolio_name,
                PortfolioPerformanceSnapshot.snapshot_date >= cutoff,
            )
            .order_by(PortfolioPerformanceSnapshot.snapshot_date.asc())
            .all()
        )
    return [
        PerformanceRecord(
            portfolio_name=r.portfolio_name,
            snapshot_date=r.snapshot_date,
            total_cost=r.total_cost,
            total_value=r.total_value,
            total_pnl=r.total_pnl,
            total_pnl_pct=r.total_pnl_pct,
            spy_price=r.spy_price,
            spy_pnl_pct=r.spy_pnl_pct,
            position_count=r.position_count,
        )
        for r in rows
    ]


def format_performance_section(history: list[PerformanceRecord]) -> str:
    """Format a performance time-series as a Markdown section for the newsletter."""
    if not history:
        return ""

    latest = history[-1]
    vs_spy_str = ""
    if latest.vs_spy is not None:
        sign = "+" if latest.vs_spy >= 0 else ""
        vs_spy_str = f" | vs SPY YTD: {sign}{float(latest.vs_spy):.1f}%"

    lines = [
        "## Portfolio Performance\n",
        f"**As of {latest.snapshot_date}:** "
        f"${float(latest.total_value):,.0f} "
        f"({float(latest.total_pnl_pct):+.1f}% / "
        f"${float(latest.total_pnl):+,.0f})"
        f"{vs_spy_str}\n",
    ]

    if len(history) >= 2:
        lines += ["", "| Date | Portfolio Value | P&L % | vs SPY YTD |", "|------|----------------|-------|------------|"]
        for r in history[-14:]:  # last 14 data points
            vs_spy = f"{float(r.vs_spy):+.1f}%" if r.vs_spy is not None else "—"
            lines.append(
                f"| {r.snapshot_date} "
                f"| ${float(r.total_value):>12,.0f} "
                f"| {float(r.total_pnl_pct):+.1f}% "
                f"| {vs_spy} |"
            )

    return "\n".join(lines)


def track_and_save(
    snapshot: PortfolioSnapshot,
    portfolio_name: str,
    snapshot_date: date | None = None,
) -> PerformanceRecord:
    """Convenience wrapper: take snapshot + save to DB + return record."""
    record = take_snapshot(snapshot, portfolio_name, snapshot_date)
    save_snapshot(record)
    return record
