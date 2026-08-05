"""EDGAR Form 4 Insider Cluster Buy Scanner.

Detects when 3+ distinct insiders at the same company file Form 4 open-market
purchases (transaction code "P") within a 30-day rolling window.

Academic basis: cluster insider buys predict ~6% excess return over 30 days.
(Seyhun 1998; Lakonishok & Lee 2001; Jeng, Metrick & Zeckhauser 2003)

Data source: SEC EDGAR EDGAR REST API (free, no key required).
  - Ticker → CIK:  https://www.sec.gov/files/company_tickers.json
  - Filings list:   https://data.sec.gov/submissions/CIK{10-digit}.json
  - Form 4 XML:     https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}.xml

Usage:
    from src.alerts.insider_cluster import run_insider_cluster_check

    result = run_insider_cluster_check("NVDA", lookback_days=60)
    if result:
        print(result.summary_line())
        print(result.newsletter_section())
"""
from __future__ import annotations

import gzip
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from loguru import logger

# ── EDGAR request headers (EDGAR policy requires User-Agent with contact info) ──
_EDGAR_HEADERS = {
    "User-Agent": "Dopple/1.0 nick.bossn@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}
_EDGAR_RATE_LIMIT_SLEEP = 0.12   # 10 req/s max; stay conservative at ~8/s

# ── Cluster detection parameters ─────────────────────────────────────────────
CLUSTER_WINDOW_DAYS: int = 30        # rolling window for insider buy clustering
CLUSTER_MIN_INSIDERS: int = 3        # minimum distinct insiders to flag
DEFAULT_LOOKBACK_DAYS: int = 60      # how far back to search for Form 4s
MAX_FORM4_TO_PARSE: int = 80         # cap XML fetches per ticker to limit latency

# ── Transaction codes that indicate voluntary open-market buys ────────────────
OPEN_MARKET_BUY_CODES = {"P"}   # P = open-market or private purchase

# Session-level cache: ticker → CIK (avoids re-downloading tickers.json per call)
_ticker_cik_cache: dict[str, str] = {}


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class InsiderTransaction:
    """A single Form 4 open-market purchase transaction."""
    ticker: str
    issuer_name: str
    insider_name: str
    role: str                    # e.g. "CEO", "Director", "10% Owner"
    is_officer: bool
    is_director: bool
    shares: float
    price: float | None
    transaction_date: date
    filing_date: date
    filing_url: str

    @property
    def dollar_value(self) -> float | None:
        if self.price is None:
            return None
        return self.shares * self.price

    @property
    def role_weight(self) -> int:
        """Conviction weight: officers (incl C-suite) = 2, directors = 1."""
        if self.is_officer:
            return 2
        return 1


@dataclass
class ClusterResult:
    """A detected cluster of insider buys within a rolling window."""
    ticker: str
    issuer_name: str
    window_start: date
    window_end: date
    transactions: list[InsiderTransaction] = field(default_factory=list)

    @property
    def insider_count(self) -> int:
        return len({t.insider_name for t in self.transactions})

    @property
    def total_shares(self) -> float:
        return sum(t.shares for t in self.transactions)

    @property
    def total_value(self) -> float | None:
        vals = [t.dollar_value for t in self.transactions if t.dollar_value is not None]
        return sum(vals) if vals else None

    @property
    def officer_count(self) -> int:
        return len({t.insider_name for t in self.transactions if t.is_officer})

    @property
    def signal_strength(self) -> str:
        n = self.insider_count
        officers = self.officer_count
        if officers >= 2 or n >= 5:
            return "STRONG"
        if officers >= 1 or n >= 3:
            return "NOTABLE"
        return "WEAK"

    def summary_line(self) -> str:
        val = f"~${self.total_value:,.0f}" if self.total_value else "value unknown"
        return (
            f"{self.ticker}: {self.insider_count} insiders bought "
            f"({self.officer_count} officers) | {val} | "
            f"window {self.window_start}→{self.window_end} | {self.signal_strength}"
        )

    def newsletter_section(self) -> str:
        val_str = f"~${self.total_value:,.0f}" if self.total_value else "value unspecified"
        officer_note = (
            f" including **{self.officer_count} officer(s)**" if self.officer_count else ""
        )
        lines = [
            f"**🏛 INSIDER CLUSTER BUY — {self.ticker} ({self.signal_strength})**",
            f"{self.insider_count} distinct insiders{officer_note} bought "
            f"{self.total_shares:,.0f} shares ({val_str}) "
            f"between {self.window_start} and {self.window_end}.",
            "",
            "| Insider | Role | Shares | Value | Date |",
            "|---------|------|--------|-------|------|",
        ]
        seen: set[str] = set()
        for t in sorted(self.transactions, key=lambda x: x.transaction_date, reverse=True):
            key = f"{t.insider_name}-{t.transaction_date}"
            if key in seen:
                continue
            seen.add(key)
            val = f"${t.dollar_value:,.0f}" if t.dollar_value else "—"
            lines.append(
                f"| {t.insider_name.title()} | {t.role} | "
                f"{t.shares:,.0f} | {val} | {t.transaction_date} |"
            )
        lines += [
            "",
            "_Source: SEC EDGAR Form 4. Open-market purchases only (code P)._",
        ]
        return "\n".join(lines)


# ── EDGAR helpers ─────────────────────────────────────────────────────────────

def _fetch(url: str) -> bytes:
    """Fetch a URL from EDGAR with proper headers. Returns raw bytes."""
    req = urllib.request.Request(url, headers=_EDGAR_HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    # EDGAR gzip-encodes some responses
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def _fetch_json(url: str) -> dict:
    return __import__("json").loads(_fetch(url))


def _resolve_cik(ticker: str) -> Optional[str]:
    """Return zero-padded 10-digit CIK for a ticker, or None if not found."""
    global _ticker_cik_cache
    t = ticker.upper()

    if t in _ticker_cik_cache:
        return _ticker_cik_cache[t]

    try:
        data = _fetch_json("https://www.sec.gov/files/company_tickers.json")
        for entry in data.values():
            _ticker_cik_cache[entry["ticker"]] = str(entry["cik_str"]).zfill(10)
    except Exception as e:
        logger.warning(f"insider_cluster: failed to load tickers.json: {e}")
        return None

    return _ticker_cik_cache.get(t)


def _xml_text(el: ET.Element, path: str) -> Optional[str]:
    """Safe text extraction from XML element."""
    node = el.find(path)
    return node.text.strip() if node is not None and node.text else None


def _parse_form4_xml(xml_bytes: bytes, filing_date: date, filing_url: str) -> list[InsiderTransaction]:
    """Parse a Form 4 XML document; return only open-market purchase transactions."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.debug(f"insider_cluster: XML parse error: {e}")
        return []

    ticker = _xml_text(root, "issuer/issuerTradingSymbol") or ""
    issuer_name = _xml_text(root, "issuer/issuerName") or ""

    owner_el = root.find("reportingOwner")
    if owner_el is None:
        return []

    insider_name = _xml_text(owner_el, "reportingOwnerId/rptOwnerName") or "Unknown"
    rel = owner_el.find("reportingOwnerRelationship")
    is_officer = rel is not None and _xml_text(rel, "isOfficer") == "1"
    is_director = rel is not None and _xml_text(rel, "isDirector") == "1"
    officer_title = _xml_text(rel, "officerTitle") if rel is not None else None

    role_parts = []
    if officer_title:
        role_parts.append(officer_title)
    elif is_officer:
        role_parts.append("Officer")
    if is_director and not officer_title:
        role_parts.append("Director")
    if rel is not None and _xml_text(rel, "isTenPercentOwner") == "1":
        role_parts.append("10% Owner")
    role = " / ".join(role_parts) or "Insider"

    results: list[InsiderTransaction] = []
    for txn in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _xml_text(txn, "transactionCoding/transactionCode")
        if code not in OPEN_MARKET_BUY_CODES:
            continue

        date_str = _xml_text(txn, "transactionDate/value")
        if not date_str:
            continue
        try:
            txn_date = date.fromisoformat(date_str)
        except ValueError:
            continue

        shares_str = _xml_text(txn, "transactionAmounts/transactionShares/value")
        price_str = _xml_text(txn, "transactionAmounts/transactionPricePerShare/value")

        try:
            shares = float(shares_str) if shares_str else 0.0
        except ValueError:
            shares = 0.0
        try:
            price = float(price_str) if price_str else None
        except ValueError:
            price = None

        if shares <= 0:
            continue

        results.append(InsiderTransaction(
            ticker=ticker.upper(),
            issuer_name=issuer_name,
            insider_name=insider_name,
            role=role,
            is_officer=is_officer,
            is_director=is_director,
            shares=shares,
            price=price,
            transaction_date=txn_date,
            filing_date=filing_date,
            filing_url=filing_url,
        ))

    return results


def fetch_insider_transactions(
    ticker: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[InsiderTransaction]:
    """Fetch all open-market Form 4 purchase transactions for a ticker.

    Returns a list sorted by transaction_date descending. Caps at
    MAX_FORM4_TO_PARSE filings to bound latency.
    """
    cik = _resolve_cik(ticker)
    if cik is None:
        logger.warning(f"insider_cluster: CIK not found for {ticker}")
        return []

    cutoff = date.today() - timedelta(days=lookback_days)

    # Fetch filing history
    try:
        submissions = _fetch_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    except Exception as e:
        logger.warning(f"insider_cluster: submissions fetch failed for {ticker}: {e}")
        return []

    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])

    # Collect Form 4s within the lookback window
    form4_jobs: list[tuple[str, str, date]] = []  # (xml_url, acc_nodash, filing_date)
    for i, form in enumerate(forms):
        if form != "4":
            continue
        try:
            fdate = date.fromisoformat(dates[i])
        except (ValueError, IndexError):
            continue
        if fdate < cutoff:
            break  # filings are newest-first; stop when we go past the window
        if len(form4_jobs) >= MAX_FORM4_TO_PARSE:
            break

        acc_nodash = accessions[i].replace("-", "")
        raw_doc = docs[i]
        # Strip XSLT renderer prefix if present — we want the raw XML
        if "/" in raw_doc:
            raw_doc = raw_doc.split("/")[-1]
        xml_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{raw_doc}"
        form4_jobs.append((xml_url, acc_nodash, fdate))

    logger.info(f"insider_cluster: {ticker} — {len(form4_jobs)} Form 4s to parse ({lookback_days}d window)")

    # Fetch and parse each Form 4 XML
    all_transactions: list[InsiderTransaction] = []
    for xml_url, _, filing_date in form4_jobs:
        try:
            time.sleep(_EDGAR_RATE_LIMIT_SLEEP)
            xml_bytes = _fetch(xml_url)
            txns = _parse_form4_xml(xml_bytes, filing_date, xml_url)
            all_transactions.extend(txns)
        except Exception as e:
            logger.debug(f"insider_cluster: failed to fetch/parse {xml_url}: {e}")

    all_transactions.sort(key=lambda t: t.transaction_date, reverse=True)
    logger.info(
        f"insider_cluster: {ticker} — {len(all_transactions)} open-market purchase "
        f"transaction(s) found across {len(form4_jobs)} filings"
    )
    return all_transactions


def detect_cluster(
    transactions: list[InsiderTransaction],
    window_days: int = CLUSTER_WINDOW_DAYS,
    min_insiders: int = CLUSTER_MIN_INSIDERS,
) -> Optional[ClusterResult]:
    """Find the most recent window with 3+ distinct insiders buying.

    Scans all possible 30-day windows (anchored at each unique transaction date).
    Returns the most recent qualifying window, or None.
    """
    if not transactions:
        return None

    ticker = transactions[0].ticker
    issuer_name = transactions[0].issuer_name
    best: Optional[ClusterResult] = None

    for anchor_txn in transactions:
        window_end = anchor_txn.transaction_date
        window_start = window_end - timedelta(days=window_days)

        window_txns = [
            t for t in transactions
            if window_start <= t.transaction_date <= window_end
        ]
        distinct_insiders = {t.insider_name for t in window_txns}

        if len(distinct_insiders) < min_insiders:
            continue

        candidate = ClusterResult(
            ticker=ticker,
            issuer_name=issuer_name,
            window_start=window_start,
            window_end=window_end,
            transactions=window_txns,
        )

        if best is None or candidate.window_end > best.window_end:
            best = candidate

    return best


# ── Public API ────────────────────────────────────────────────────────────────

def run_insider_cluster_check(
    ticker: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    window_days: int = CLUSTER_WINDOW_DAYS,
    min_insiders: int = CLUSTER_MIN_INSIDERS,
) -> Optional[ClusterResult]:
    """Run a full insider cluster check for a ticker.

    Returns a ClusterResult if a qualifying cluster was found, else None.

    Args:
        ticker:        Stock symbol.
        lookback_days: How many days back to search Form 4s (default 60).
        window_days:   Rolling window for cluster detection (default 30).
        min_insiders:  Minimum distinct insiders to flag (default 3).
    """
    ticker = ticker.upper().strip()
    txns = fetch_insider_transactions(ticker, lookback_days=lookback_days)
    if not txns:
        logger.info(f"insider_cluster: {ticker} — no open-market purchases found")
        return None

    result = detect_cluster(txns, window_days=window_days, min_insiders=min_insiders)
    if result:
        logger.info(f"insider_cluster: {ticker} — CLUSTER DETECTED: {result.summary_line()}")
    else:
        logger.info(
            f"insider_cluster: {ticker} — no cluster (< {min_insiders} insiders "
            f"within any {window_days}-day window)"
        )
    return result


def cluster_alert(result: ClusterResult) -> None:
    """Fire a Discord webhook alert for a cluster buy event."""
    import os
    import json as _json
    import urllib.request as _req

    from src.config import discord_webhook

    webhook = discord_webhook()
    if not webhook:
        logger.debug(
            "insider_cluster: DISCORD_WEBHOOK_URL not resolvable "
            "(checked process env and .env) — skipping alert"
        )
        return

    val_str = f"~${result.total_value:,.0f}" if result.total_value else "value unknown"
    msg = (
        f"🏛 **INSIDER CLUSTER BUY — {result.ticker} ({result.signal_strength})**\n"
        f"{result.insider_count} insiders ({result.officer_count} officers) bought "
        f"{result.total_shares:,.0f} shares ({val_str})\n"
        f"Window: {result.window_start} → {result.window_end}\n"
        f"_Source: SEC EDGAR Form 4 — open-market purchases only_"
    )
    try:
        payload = _json.dumps({"content": msg}).encode()
        r = _req.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
        with _req.urlopen(r, timeout=10):
            pass
        logger.info(f"insider_cluster: Discord alert sent for {result.ticker}")
    except Exception as e:
        logger.warning(f"insider_cluster: Discord alert failed: {e}")


def get_newsletter_cluster_section(
    ticker: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> Optional[str]:
    """Return a Markdown newsletter section if a cluster exists, else None.

    Convenience wrapper for pipeline injection.
    """
    result = run_insider_cluster_check(ticker, lookback_days=lookback_days)
    if result is None:
        return None
    return result.newsletter_section()
