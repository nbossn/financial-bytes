"""Broker ticker → Yahoo Finance symbol.

US brokerages (Fidelity, Schwab) render dual-class share tickers with no
separator — ``BRKB``, ``BFB``. Yahoo Finance requires a hyphen — ``BRK-B``,
``BF-B``. Handed the broker form, yfinance returns an **empty frame rather
than raising**, so the failure is silent at every call site: no exception, no
log line, just a ticker that quietly fails to appear in the results dict.

Nick holds ``BRKB`` in two portfolios. It was invisible to all 27 market-data
call sites, and ``PortfolioSnapshot.total_value`` then substituted cost basis
for the missing price — the one value that makes unrealized P&L exactly zero.
Ten consecutive newsletters described a $62K equity position as "near-inert",
grouped with the money-market cash line.

**Why this is an explicit map and not a pattern.** The broker form of a
dual-class ticker is not distinguishable by shape from an ordinary one:
``NVDA`` and ``BRKB`` are both four letters ending in a class letter. A regex
that inserted a hyphen before the last character would send ``NVDA`` to
``NVD-A``. And a *wrong* mapping is strictly worse than a missing one — it
fabricates another company's price instead of no price. So every entry below
was resolved against Yahoo on 2026-08-05 and checked to return the right
company name:

===========  ==========  ================================
broker form  Yahoo form  longName returned
===========  ==========  ================================
BRKA         BRK-A       Berkshire Hathaway Inc.
BRKB         BRK-B       Berkshire Hathaway Inc.
BFA          BF-A        Brown-Forman Corporation
BFB          BF-B        Brown-Forman Corporation
LENB         LEN-B       Lennar Corporation
HEIA         HEI-A       HEICO Corporation
PBRA         PBR-A       Petróleo Brasileiro S.A.
===========  ==========  ================================

In every case the broker form returned **0 rows** and the Yahoo form returned
5, so each entry is load-bearing rather than cosmetic.

``LGFA``/``LGFB`` (Lionsgate) were candidates and are **deliberately absent**:
``LGF-A`` and ``LGF-B`` both returned 0 rows too, so there is no verified
target to map them to. An unverified guess is the failure mode this map exists
to avoid.

Adding an entry: resolve it against Yahoo first and confirm ``longName``
matches the company you mean. Do not add one from memory.
"""
from __future__ import annotations

# Broker/Fidelity ticker -> Yahoo Finance symbol. Keys are upper-case.
BROKER_TO_YAHOO: dict[str, str] = {
    "BRKA": "BRK-A",
    "BRKB": "BRK-B",
    "BFA": "BF-A",
    "BFB": "BF-B",
    "LENB": "LEN-B",
    "HEIA": "HEI-A",
    "PBRA": "PBR-A",
}


def to_yahoo_symbol(ticker: str | None) -> str | None:
    """Return the symbol Yahoo Finance knows this ticker by.

    Unmapped tickers are returned **byte-identical** — callers may already be
    passing a Yahoo-specific symbol (``BTC-USD``, ``^VIX``) that must not be
    touched.
    """
    if not ticker:
        return ticker
    return BROKER_TO_YAHOO.get(ticker.upper(), ticker)


def resolve_symbol(ticker: str | None) -> tuple[str | None, bool]:
    """Return ``(yahoo_symbol, was_rewritten)``.

    Call sites use the flag to log the translation, so a mapping that starts
    resolving to nothing is attributable rather than mysterious.
    """
    resolved = to_yahoo_symbol(ticker)
    return resolved, resolved != ticker
