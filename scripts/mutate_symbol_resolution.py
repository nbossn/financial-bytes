#!/usr/bin/env python3
"""Mutation harness for the symbol-resolution / missing-price fix.

Each mutation is a literal (file, before, after) substitution. The harness
copies the repo to a temp tree, applies exactly one mutation, and runs
tests/test_symbol_resolution.py. A mutation that leaves the suite green is a
test gap.

⚠️ Every mutation asserts its anchor actually applied. A substitution whose
`before` string is absent silently mutates nothing and the run then reports
"all caught" — which is the harness lying about its own coverage.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_PY = REPO / ".venv" / "bin" / "python"
TEST = "tests/test_symbol_resolution.py"

# (id, description, relative path, before, after)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    ("M01", "drop the BRKB mapping entirely",
     "src/api/symbols.py", '    "BRKB": "BRK-B",\n', ""),
    ("M02", "to_yahoo_symbol never rewrites",
     "src/api/symbols.py",
     "    return BROKER_TO_YAHOO.get(ticker.upper(), ticker)",
     "    return ticker"),
    ("M03", "lookup is case-sensitive",
     "src/api/symbols.py",
     "BROKER_TO_YAHOO.get(ticker.upper(), ticker)",
     "BROKER_TO_YAHOO.get(ticker, ticker)"),
    ("M04", "resolve_symbol always reports 'not rewritten'",
     "src/api/symbols.py",
     "    return resolved, resolved != ticker",
     "    return resolved, False"),
    ("M05", "to_yahoo_symbol rewrites an unmapped ticker by pattern",
     "src/api/symbols.py",
     "    return BROKER_TO_YAHOO.get(ticker.upper(), ticker)",
     "    return BROKER_TO_YAHOO.get(ticker.upper(), ticker[:-1] + '-' + ticker[-1])"),
    ("M06", "missing_prices inverts its membership test",
     "src/portfolio/models.py",
     "return [h.ticker for h in self.holdings if h.ticker not in self.prices]",
     "return [h.ticker for h in self.holdings if h.ticker in self.prices]"),
    ("M07", "missing_prices always empty",
     "src/portfolio/models.py",
     "return [h.ticker for h in self.holdings if h.ticker not in self.prices]",
     "return []"),
    ("M08", "has_complete_prices always True",
     "src/portfolio/models.py",
     "        return not self.missing_prices",
     "        return True"),
    ("M09", "missing_prices loses holding order",
     "src/portfolio/models.py",
     "return [h.ticker for h in self.holdings if h.ticker not in self.prices]",
     "return sorted({h.ticker for h in self.holdings if h.ticker not in self.prices})"),
    ("M10", "total_value stops falling back to cost basis",
     "src/portfolio/models.py",
     "return sum(h.current_value(self.prices.get(h.ticker, h.cost_basis)) for h in self.holdings)",
     "return sum(h.current_value(self.prices[h.ticker]) for h in self.holdings if h.ticker in self.prices)"),
    ("M11", "_fetch_prices asks Yahoo for the broker symbol",
     "src/alerts/stop_loss.py",
     "            hist = yf.Ticker(symbol).history(period=\"2d\")",
     "            hist = yf.Ticker(ticker).history(period=\"2d\")"),
    ("M12", "_fetch_prices keys results by the Yahoo symbol",
     "src/alerts/stop_loss.py",
     "                prices[ticker] = Decimal(str(round(hist[\"Close\"].iloc[-1], 4)))",
     "                prices[symbol] = Decimal(str(round(hist[\"Close\"].iloc[-1], 4)))"),
    ("M13", "_fetch_prices goes silent again on an empty frame",
     "src/alerts/stop_loss.py",
     "                logger.warning(\n                    f\"No price data for {ticker}\"",
     "                logger.debug(\n                    f\"No price data for {ticker}\""),
    ("M14", "get_quote_yfinance asks for the broker symbol",
     "src/api/yfinance_client.py", "        t = yf.Ticker(symbol)", "        t = yf.Ticker(ticker)"),
    ("M15", "get_quote_yfinance keys the snapshot by the Yahoo symbol",
     "src/api/yfinance_client.py", "            ticker=ticker,\n            current_price=price_d,",
     "            ticker=symbol,\n            current_price=price_d,"),
    ("M16", "batch download uses broker symbols",
     "src/api/yfinance_client.py",
     "            tickers=\" \".join(symbol_of[t] for t in tickers),",
     "            tickers=\" \".join(tickers),"),
    ("M17", "batch looks up the broker symbol in the frame",
     "src/api/yfinance_client.py",
     "            symbol = symbol_of[ticker]\n            if symbol not in close.columns:",
     "            symbol = symbol_of[ticker]\n            if ticker not in close.columns:"),
    ("M18", "batch keys results by the Yahoo symbol",
     "src/api/yfinance_client.py",
     "            results[ticker] = QuoteSnapshot(\n                ticker=ticker,",
     "            results[symbol] = QuoteSnapshot(\n                ticker=ticker,"),
    ("M19", "restore the unconditional to_frame (the pre-existing single-ticker bug)",
     "src/api/yfinance_client.py",
     "        if not hasattr(close, \"columns\"):",
     "        if len(tickers) == 1:"),
    ("M20", "a mapping value collides with another mapping's key",
     "src/api/symbols.py", '    "BFA": "BF-A",', '    "BFA": "BRKB",'),
]


def run_one(mut) -> tuple[str, bool, str]:
    mid, desc, rel, before, after = mut
    with tempfile.TemporaryDirectory(prefix=f"mut-{mid}-") as td:
        tree = Path(td) / "repo"
        subprocess.run(
            ["git", "-C", str(REPO), "worktree", "list"], capture_output=True, check=False
        )
        shutil.copytree(
            REPO, tree,
            ignore=shutil.ignore_patterns(
                ".venv", ".git", "__pycache__", "htmlcov", "data", "newsletters",
                "logs", "*.db", ".pytest_cache",
            ),
        )
        target = tree / rel
        src = target.read_text()
        if before not in src:
            return mid, False, f"ANCHOR MISSING in {rel} — mutation did not apply"
        if src.count(before) != 1:
            return mid, False, f"ANCHOR AMBIGUOUS in {rel} ({src.count(before)} matches)"
        target.write_text(src.replace(before, after, 1))

        proc = subprocess.run(
            [str(VENV_PY), "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider", "--no-cov"],
            cwd=tree, capture_output=True, text=True, timeout=300,
        )
        caught = proc.returncode != 0
        tail = (proc.stdout or "").strip().splitlines()
        summary = tail[-1] if tail else "(no output)"
        return mid, caught, summary


def main() -> int:
    caught = survived = refused = 0
    print(f"Mutation run — {len(MUTATIONS)} mutations against {TEST}\n")
    for mut in MUTATIONS:
        mid, ok, summary = run_one(mut)
        desc = mut[1]
        if summary.startswith("ANCHOR"):
            refused += 1
            print(f"  {mid} ⚠ REFUSED  {desc}\n         {summary}")
        elif ok:
            caught += 1
            print(f"  {mid} ✓ caught   {desc}")
        else:
            survived += 1
            print(f"  {mid} ✗ SURVIVED {desc}\n         {summary}")
    print(f"\n{caught} caught · {survived} survived · {refused} refused "
          f"(anchor never applied) of {len(MUTATIONS)}")
    return 0 if (survived == 0 and refused == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
