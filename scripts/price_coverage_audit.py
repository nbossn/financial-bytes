#!/usr/bin/env python3
"""Does every holding Nick is actually sent get a *market* price?

BRKB did not, for 41 days, and nothing failed. The reason it survived is worth
stating plainly: every surface that could have noticed was fail-open.

  - yfinance answers an unknown symbol with an EMPTY FRAME, not an exception.
  - the fetch loop's empty branch had no log line at all.
  - `PortfolioSnapshot.total_value` substituted `cost_basis`, which is exactly
    the value that makes unrealized P&L $0.00 — a normal-looking answer.
  - the newsletter rendered `+$0.00` and the analyst agent wrote prose around
    it ("BRKB ($0) ... near-inert").

The pipeline now logs missing prices. A log line is not a consumer that can
fail, so this is that consumer: run it and it exits non-zero when a delivered
holding cannot be priced at market.

    .venv/bin/python scripts/price_coverage_audit.py

  exit 0  every holding in every *delivered* portfolio priced at market
  exit 1  at least one holding could not be priced  (the BRKB condition)
  exit 2  the audit could not measure anything — no portfolios, no holdings,
          or the positive control failed. Distinct from 0 on purpose: a
          checker that passes on zero inputs is indistinguishable from one
          that passes on good inputs.

Scope is deliberately the portfolios in `portfolios.json` — the ones with
recipients. The DB also carries a `default` portfolio that is configured
nowhere and delivered to nobody; auditing it would produce a permanent red
over a holding no one receives, and a permanently red audit is
indistinguishable from no audit.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

REPO = Path(__file__).resolve().parent.parent
PORTFOLIOS_JSON = REPO / "portfolios.json"
DB = REPO / "financial_bytes.db"

# Audit the tree this script ships in, not whatever `src` the interpreter
# happens to resolve.
#
# Run as `python scripts/price_coverage_audit.py`, sys.path[0] is *scripts/*,
# not the repo root — so `import src.api...` fell through to the venv's
# `financial_bytes.pth`, which points at /home/nboss/financial-bytes
# unconditionally. A copy of this script placed in a pre-fix tree therefore
# measured the FIXED code and reported a clean pass, which is precisely the
# false control an audit must not have. Verified, not assumed: the same file
# resolved to /home/nboss/... when run as a script and to the local tree when
# run via `-c`, because `-c` puts cwd on the path first.
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# A ticker that must price, and one that must not. Without both, a zero
# finding carries no information — see the module docstring.
CONTROL_GOOD = "SPY"
CONTROL_BAD = "ZZZZNOTATICKER"


def delivered_portfolios() -> list[str]:
    """Portfolio names that have at least one recipient."""
    if not PORTFOLIOS_JSON.exists():
        return []
    cfg = json.loads(PORTFOLIOS_JSON.read_text())
    names = []
    for p in cfg:
        recipients = p.get("recipients") or p.get("email_recipients") or []
        if p.get("name") and recipients:
            names.append(p["name"])
    return names


def holdings_for(names: list[str]) -> dict[str, list[str]]:
    """{portfolio_name: [ticker, ...]} straight from the live DB."""
    if not DB.exists() or not names:
        return {}
    out: dict[str, list[str]] = {}
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as conn:
        for name in names:
            rows = conn.execute(
                "select distinct ticker from portfolio where portfolio_name = ? order by ticker",
                (name,),
            ).fetchall()
            if rows:
                out[name] = [r[0] for r in rows]
    return out


def prices_at_market(ticker: str) -> bool:
    """True when the ticker resolves to real market data.

    Goes through `get_quote_yfinance`, which applies the broker→Yahoo symbol
    map — so this measures what the pipeline will actually get, not what a
    raw yfinance call would.
    """
    from src.api.yfinance_client import get_quote_yfinance

    try:
        quote = get_quote_yfinance(ticker)
    except Exception:
        return False
    return bool(quote and quote.current_price)


def main() -> int:
    names = delivered_portfolios()
    if not names:
        print("VACUOUS: no portfolio in portfolios.json has a recipient", file=sys.stderr)
        return 2

    held = holdings_for(names)
    if not held:
        print(f"VACUOUS: no holdings found for {names}", file=sys.stderr)
        return 2

    # Positive control first. If the good control cannot price, the network or
    # the provider is down and every "FAIL" below would be an artefact.
    if not prices_at_market(CONTROL_GOOD):
        print(f"VACUOUS: positive control {CONTROL_GOOD} did not price — "
              "cannot distinguish a real finding from an outage", file=sys.stderr)
        return 2
    # Negative control: prove the check is capable of saying no.
    if prices_at_market(CONTROL_BAD):
        print(f"VACUOUS: negative control {CONTROL_BAD} priced — "
              "the check cannot fail, so a clean result means nothing", file=sys.stderr)
        return 2

    print("Price coverage audit")
    print("=" * 62)
    print(f"controls: {CONTROL_GOOD} prices, {CONTROL_BAD} does not — check is live\n")

    unpriced: list[tuple[str, str]] = []
    checked = 0
    for name in sorted(held):
        bad = []
        for ticker in held[name]:
            checked += 1
            if not prices_at_market(ticker):
                bad.append(ticker)
                unpriced.append((name, ticker))
        status = "OK" if not bad else f"FAIL — {', '.join(bad)}"
        print(f"  [{'OK  ' if not bad else 'FAIL'}] {name:20} {len(held[name]):3} ticker(s)  {status if bad else ''}")

    print("-" * 62)
    print(f"{checked} holding(s) across {len(held)} delivered portfolio(s)")

    if unpriced:
        print(f"\n{len(unpriced)} holding(s) CANNOT be priced at market. Each will be "
              f"valued at cost basis and report unrealized P&L of exactly $0.00:")
        for name, ticker in unpriced:
            print(f"    {name:20} {ticker}")
        print("\nIf this is a broker/Yahoo symbol-convention mismatch, add a "
              "verified entry to src/api/symbols.py. Resolve it against Yahoo "
              "and confirm longName first — a wrong mapping fabricates another "
              "company's price.")
        return 1

    print("\nverdict: every delivered holding prices at market")
    return 0


if __name__ == "__main__":
    sys.exit(main())
