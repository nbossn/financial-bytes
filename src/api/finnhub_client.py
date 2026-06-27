"""
finnhub_client.py — Finnhub data client (consensus EPS, surprises, recommendations, news).

Finnhub's free tier covers the exact gaps the picker has:
- consensus EPS estimates + earnings surprise history  → measured `earnings_sue`
- analyst recommendation trends (revision momentum)     → measured `revision_proxy`
- company news + sentiment                              → measured `sentiment`

Requires a free API key: set FINNHUB_KEY in .env (finnhub.io/register).
This client degrades gracefully (returns None) when the key is absent so the
picker keeps running on finviz + yfinance.
"""
from __future__ import annotations

import os

import requests

BASE = "https://finnhub.io/api/v1"


def _key() -> str | None:
    return os.getenv("FINNHUB_KEY") or os.getenv("FINNHUB_API_KEY")


def available() -> bool:
    return bool(_key())


def _get(path: str, params: dict) -> dict | list | None:
    key = _key()
    if not key:
        return None
    params = {**params, "token": key}
    try:
        r = requests.get(f"{BASE}/{path}", params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def earnings_surprises(ticker: str) -> list[dict] | None:
    """Historical actual vs estimate EPS surprises."""
    return _get("stock/earnings", {"symbol": ticker})


def recommendation_trends(ticker: str) -> list[dict] | None:
    """Analyst buy/hold/sell counts over time (revision momentum)."""
    return _get("stock/recommendation", {"symbol": ticker})


def company_news(ticker: str, frm: str, to: str) -> list[dict] | None:
    return _get("company-news", {"symbol": ticker, "from": frm, "to": to})


def basic_financials(ticker: str) -> dict | None:
    """Key metrics: margins, returns, valuation, 52w, beta, etc."""
    return _get("stock/metric", {"symbol": ticker, "metric": "all"})


def quote(ticker: str) -> dict | None:
    """Real-time-ish quote (c=current, pc=prev close)."""
    return _get("quote", {"symbol": ticker})


if __name__ == "__main__":
    import sys
    tk = sys.argv[1] if len(sys.argv) > 1 else "MU"
    if not available():
        print("FINNHUB_KEY not set — client built, needs a free key to run.")
        raise SystemExit(0)
    print("quote:", quote(tk))
    print("surprises:", (earnings_surprises(tk) or [])[:2])
    print("recom:", (recommendation_trends(tk) or [])[:1])
