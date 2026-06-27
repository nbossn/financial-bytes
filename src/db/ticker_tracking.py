"""ticker_tracking.py — Portfolio ticker change detection and data-cache staleness.

Two concerns solved here:
  1. PortfolioTickerLog: diff the current holdings vs. the previous DB snapshot
     to detect adds, removes, increases, decreases. Prevents silent wrong-CSV bugs
     and provides a position-change feed for the newsletter.

  2. TickerDataCache: record when each data type (news / signals / analyst) was
     last fetched so the pipeline can skip stale-but-fresh fetches.

Public API
----------
    log_ticker_changes(holdings, portfolio_name, snapshot_date) -> TickerChangeSummary
    mark_fetched(ticker, portfolio_name, data_type, status, article_count, error)
    is_stale(ticker, portfolio_name, data_type, max_age_hours) -> bool
    get_cache_summary(portfolio_name) -> list[dict]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from loguru import logger
from sqlalchemy import text

from src.db.session import get_db
from src.db.models import PortfolioTickerLog, TickerDataCache


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Data types ────────────────────────────────────────────────────────────────

DATA_TYPE_NEWS = "news"
DATA_TYPE_SIGNALS = "signals"
DATA_TYPE_ANALYST = "analyst"

# Default staleness windows — override per call-site as needed
DEFAULT_STALENESS_HOURS = {
    DATA_TYPE_NEWS: 6,       # re-scrape if news is older than 6h
    DATA_TYPE_SIGNALS: 4,    # re-fetch price/technicals if older than 4h
    DATA_TYPE_ANALYST: 23,   # re-run LLM if analyst report older than 23h (daily cadence)
}


@dataclass
class TickerChange:
    ticker: str
    action: str           # 'added' | 'removed' | 'held' | 'increased' | 'decreased'
    shares_prev: Optional[Decimal] = None
    shares_current: Optional[Decimal] = None
    cost_basis_prev: Optional[Decimal] = None
    cost_basis_current: Optional[Decimal] = None


@dataclass
class TickerChangeSummary:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    increased: list[str] = field(default_factory=list)
    decreased: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    changes: list[TickerChange] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.increased or self.decreased)

    def summary_line(self) -> str:
        parts = []
        if self.added:
            parts.append(f"+{len(self.added)} added ({', '.join(self.added[:5])}{'…' if len(self.added) > 5 else ''})")
        if self.removed:
            parts.append(f"-{len(self.removed)} removed ({', '.join(self.removed[:5])}{'…' if len(self.removed) > 5 else ''})")
        if self.increased:
            parts.append(f"↑{len(self.increased)} increased")
        if self.decreased:
            parts.append(f"↓{len(self.decreased)} decreased")
        if not parts:
            return f"{len(self.held)} positions unchanged"
        return " | ".join(parts)


# ── Ticker change detection ───────────────────────────────────────────────────

def _load_prev_snapshot(portfolio_name: str, snapshot_date: date) -> dict[str, dict]:
    """Load the most recent prior snapshot from portfolio_ticker_log."""
    with get_db() as db:
        # Find the most recent date before snapshot_date
        row = db.execute(
            text(
                "SELECT MAX(snapshot_date) FROM portfolio_ticker_log "
                "WHERE portfolio_name = :pname AND snapshot_date < :dt"
            ),
            {"pname": portfolio_name, "dt": snapshot_date},
        ).scalar()

        if not row:
            return {}

        rows = db.execute(
            text(
                "SELECT ticker, shares_current, cost_basis_current "
                "FROM portfolio_ticker_log "
                "WHERE portfolio_name = :pname AND snapshot_date = :dt "
                "AND action != 'removed'"
            ),
            {"pname": portfolio_name, "dt": row},
        ).fetchall()

        return {
            r.ticker: {"shares": r.shares_current, "cost_basis": r.cost_basis_current}
            for r in rows
        }


def log_ticker_changes(
    holdings: list,
    portfolio_name: str,
    snapshot_date: date | None = None,
) -> TickerChangeSummary:
    """Diff current holdings against the previous DB snapshot and write changes.

    Args:
        holdings: list of Holding objects (current portfolio state)
        portfolio_name: e.g. 'nbossn_fidelity'
        snapshot_date: defaults to today

    Returns:
        TickerChangeSummary — what changed vs. prior snapshot
    """
    today = snapshot_date or date.today()
    prev = _load_prev_snapshot(portfolio_name, today)
    summary = TickerChangeSummary()

    current_tickers = {h.ticker for h in holdings}
    prev_tickers = set(prev.keys())

    changes: list[TickerChange] = []

    for holding in holdings:
        ticker = holding.ticker
        cost = Decimal(str(holding.cost_basis)) if holding.cost_basis else Decimal("0")
        shares = Decimal(str(holding.shares)) if holding.shares else Decimal("0")

        if ticker not in prev:
            action = "added"
            summary.added.append(ticker)
            ch = TickerChange(ticker=ticker, action=action,
                              shares_current=shares, cost_basis_current=cost)
        else:
            prev_shares = Decimal(str(prev[ticker]["shares"])) if prev[ticker]["shares"] else Decimal("0")
            prev_cost = Decimal(str(prev[ticker]["cost_basis"])) if prev[ticker]["cost_basis"] else Decimal("0")
            share_delta = shares - prev_shares

            if abs(share_delta) < Decimal("0.0001"):
                action = "held"
                summary.held.append(ticker)
            elif share_delta > 0:
                action = "increased"
                summary.increased.append(ticker)
            else:
                action = "decreased"
                summary.decreased.append(ticker)

            ch = TickerChange(
                ticker=ticker, action=action,
                shares_prev=prev_shares, shares_current=shares,
                cost_basis_prev=prev_cost, cost_basis_current=cost,
            )
        changes.append(ch)

    # Tickers in prev but not in current = removed
    for ticker in prev_tickers - current_tickers:
        prev_data = prev[ticker]
        ch = TickerChange(
            ticker=ticker, action="removed",
            shares_prev=Decimal(str(prev_data["shares"])) if prev_data["shares"] else None,
            cost_basis_prev=Decimal(str(prev_data["cost_basis"])) if prev_data["cost_basis"] else None,
        )
        changes.append(ch)
        summary.removed.append(ticker)

    summary.changes = changes

    # Write to DB — upsert on (portfolio_name, snapshot_date, ticker)
    with get_db() as db:
        for ch in changes:
            existing = db.query(PortfolioTickerLog).filter_by(
                portfolio_name=portfolio_name,
                snapshot_date=today,
                ticker=ch.ticker,
            ).first()

            if existing:
                existing.action = ch.action
                existing.shares_prev = ch.shares_prev
                existing.shares_current = ch.shares_current
                existing.cost_basis_prev = ch.cost_basis_prev
                existing.cost_basis_current = ch.cost_basis_current
            else:
                db.add(PortfolioTickerLog(
                    portfolio_name=portfolio_name,
                    snapshot_date=today,
                    ticker=ch.ticker,
                    action=ch.action,
                    shares_prev=ch.shares_prev,
                    shares_current=ch.shares_current,
                    cost_basis_prev=ch.cost_basis_prev,
                    cost_basis_current=ch.cost_basis_current,
                ))

    if summary.has_changes:
        logger.info(f"[ticker_log] {portfolio_name}: {summary.summary_line()}")
    else:
        logger.debug(f"[ticker_log] {portfolio_name}: no position changes vs. prior snapshot")

    return summary


# ── Data cache staleness ──────────────────────────────────────────────────────

def is_stale(
    ticker: str,
    portfolio_name: str,
    data_type: str,
    max_age_hours: float | None = None,
) -> bool:
    """Return True if the cached data is missing or older than max_age_hours.

    If True → caller should re-fetch. If False → use cached DB data.
    """
    if max_age_hours is None:
        max_age_hours = DEFAULT_STALENESS_HOURS.get(data_type, 24)

    with get_db() as db:
        row = db.query(TickerDataCache).filter_by(
            ticker=ticker,
            portfolio_name=portfolio_name,
            data_type=data_type,
        ).first()

        if row is None or row.last_fetch_status not in ("ok", "no_data"):
            return True  # never fetched or last fetch errored

        age_hours = ((_now() - row.last_fetched_at).total_seconds()) / 3600
        return age_hours >= max_age_hours


def mark_fetched(
    ticker: str,
    portfolio_name: str,
    data_type: str,
    status: str = "ok",
    article_count: int | None = None,
    error_detail: str | None = None,
) -> None:
    """Record a completed fetch in the cache table. Call after every scrape/signal/analyst run."""
    now = _now()
    with get_db() as db:
        row = db.query(TickerDataCache).filter_by(
            ticker=ticker,
            portfolio_name=portfolio_name,
            data_type=data_type,
        ).first()

        if row:
            row.last_fetched_at = now
            row.last_fetch_status = status
            row.article_count = article_count
            row.error_detail = error_detail
            row.updated_at = now
        else:
            db.add(TickerDataCache(
                ticker=ticker,
                portfolio_name=portfolio_name,
                data_type=data_type,
                last_fetched_at=now,
                last_fetch_status=status,
                article_count=article_count,
                error_detail=error_detail,
            ))


def get_cache_summary(portfolio_name: str) -> list[dict]:
    """Return freshness summary for all tickers in a portfolio — useful for diagnostics."""
    with get_db() as db:
        rows = db.query(TickerDataCache).filter_by(portfolio_name=portfolio_name).all()
        now = _now()
        return [
            {
                "ticker": r.ticker,
                "data_type": r.data_type,
                "last_fetched_at": r.last_fetched_at.isoformat() if r.last_fetched_at else None,
                "age_hours": round((now - r.last_fetched_at).total_seconds() / 3600, 1) if r.last_fetched_at else None,
                "status": r.last_fetch_status,
                "article_count": r.article_count,
            }
            for r in sorted(rows, key=lambda r: (r.ticker, r.data_type))
        ]
