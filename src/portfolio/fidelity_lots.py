"""Per-lot acquisition dates from Fidelity's Tax Loss Harvesting page.

Fidelity's positions export (Portfolio_Positions_*.csv) carries no lot dates. That
left `purchase_date` unknown for almost every holding, which meant:

  * the tax engine reported a 24%-41% rate band instead of 23.8% LTCG vs ordinary,
    and on a position like NVDA's ~$70k embedded gain that band is worth ~$12k; and
  * the loader stamped `date.today()` to have *something*, which the analyst agent
    then read as the acquisition date and wrote up as fact.

The TLH page renders each position followed by one row per tax lot:

    Select lot acquired on Jun-08-2026 / Jun-08-2026 60 shares

which is the missing data. Note the scope limit: the TLH tool only lists positions
currently at a loss, so this resolves holding period exactly where the harvest
decision lives and not for winners.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from loguru import logger

# "Jun-08-2026" as Fidelity renders lot dates
_LOT_DATE = re.compile(r"acquired on\s+([A-Z][a-z]{2}-\d{2}-\d{4})", re.I)
_SHARES = re.compile(r"([\d,]+(?:\.\d+)?)\s+shares", re.I)
_MONEY = re.compile(r"\$([\d,]+(?:\.\d+)?)")
# A position row's symbol cell reads "MRVL\nMARVELL TE"
_SYMBOL = re.compile(r"^([A-Z][A-Z.\-]{0,9})(?:\n|$)")


def _num(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def parse_tlh_lot_rows(rows: list[list[str]]) -> dict[str, list[dict]]:
    """Turn the TLH table into {ticker: [{shares, cost_basis, purchase_date}]}.

    Lot rows carry no ticker of their own — they inherit the position row above
    them, so order matters. Rows that cannot be parsed are dropped rather than
    guessed at: a wrong acquisition date is worse than a missing one, because it
    silently reclassifies short-term as long-term.
    """
    out: dict[str, list[dict]] = {}
    current: str | None = None

    for row in rows:
        cells = [c for c in (row or []) if c and c.strip()]
        if not cells:
            continue

        joined = " ".join(cells)

        # ── Lot row ──────────────────────────────────────────────────────────
        if "acquired on" in joined.lower():
            if current is None:
                logger.debug("TLH lot row before any position row — skipping")
                continue

            date_m = _LOT_DATE.search(joined)
            shares_m = _SHARES.search(joined)
            if not date_m or not shares_m:
                continue

            try:
                acquired = datetime.strptime(date_m.group(1), "%b-%d-%Y").date()
            except ValueError:
                logger.debug(f"Unparseable TLH lot date {date_m.group(1)!r} — skipping")
                continue

            shares = _num(shares_m.group(1))
            if shares is None or shares <= 0:
                continue

            cost_basis = None
            for cell in cells[1:]:
                money = _MONEY.search(cell)
                if money:
                    cost_basis = _num(money.group(1))
                    break

            out.setdefault(current, []).append({
                "shares": shares,
                "cost_basis": cost_basis,
                "purchase_date": acquired.isoformat(),
            })
            continue

        # ── Position row: sets the ticker subsequent lot rows belong to ──────
        for cell in cells[:3]:
            m = _SYMBOL.match(cell.strip())
            if m and m.group(1) not in {"Symbol", "Select"}:
                current = m.group(1)
                break

    # A position with no lot rows yields no dates — drop the empty shell.
    return {k: v for k, v in out.items() if v}


def filter_lots_to_holdings(
    lots: dict[str, list[dict]],
    holding_shares: dict[str, float],
    tolerance: float = 0.01,
) -> tuple[dict[str, list[dict]], list[str], list[tuple[str, float, float]]]:
    """Restrict scraped lots to tickers the target portfolio actually holds.

    The TLH table has no account column, but Nick's login spans the personal
    account and two trust sub-accounts. Without this, a ticker held in both would
    write trust lots into the personal purchase history.

    Returns (kept, dropped_tickers, mismatches) where each mismatch is
    (ticker, lot_share_total, portfolio_shares). Mismatches are reported rather
    than dropped — a partial lot list is still useful, but the caller should say
    so out loud rather than treat it as a complete picture.
    """
    kept: dict[str, list[dict]] = {}
    dropped: list[str] = []
    mismatches: list[tuple[str, float, float]] = []

    for ticker, ticker_lots in lots.items():
        held = holding_shares.get(ticker)
        if held is None:
            dropped.append(ticker)
            continue

        kept[ticker] = ticker_lots
        lot_total = sum(l.get("shares") or 0 for l in ticker_lots)
        if abs(lot_total - held) > tolerance:
            mismatches.append((ticker, lot_total, held))

    if dropped:
        logger.warning(
            f"Dropped {len(dropped)} scraped ticker(s) not held in this portfolio "
            f"(likely another account on the same login): {sorted(dropped)}"
        )
    for ticker, lot_total, held in mismatches:
        logger.warning(
            f"{ticker}: scraped lots total {lot_total:,.3f} shares but the portfolio "
            f"holds {held:,.3f} — lots may be partial or span another account"
        )

    return kept, sorted(dropped), mismatches


def merge_lot_history(existing: dict, scraped: dict[str, list[dict]]) -> dict:
    """Merge scraped lots into a purchase_history dict, preferring what's there.

    Hand-curated entries (Nick reconciled several against Fidelity's lot detail)
    are authoritative — scraping must never silently overwrite them. Comment keys
    (`_comment`, `_note`) are carried through untouched.
    """
    merged = dict(existing)
    added = []
    for ticker, lots in scraped.items():
        if ticker in merged and merged[ticker]:
            continue
        merged[ticker] = lots
        added.append(ticker)

    if added:
        logger.info(f"Added lot history for {len(added)} ticker(s): {sorted(added)}")
    return merged


def write_purchase_history(path: str | Path, scraped: dict[str, list[dict]]) -> dict:
    """Merge scraped lots into the purchase-history JSON at `path` and save."""
    p = Path(path)
    existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    merged = merge_lot_history(existing, scraped)
    p.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    logger.info(f"Wrote purchase history → {p}")
    return merged
