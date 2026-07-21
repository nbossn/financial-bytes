"""
ledger.py — the prediction ledger that makes the picker's weights empirical.

The confidence matrix (confidence.py) currently shrinks measured IC toward
literature priors. Until we have a record of *what the model predicted* vs *what
actually happened*, the event signals stay prior-dominated. This module closes
that loop:

1. `record(records, as_of)` — append every candidate's composite + per-signal
   contributions for a run to data/stockpicker/ledger/predictions.jsonl. One row
   per (date, ticker). Idempotent per date (re-running a date overwrites it).

2. `score(min_age_days=5)` — for past prediction dates old enough to have realized
   returns, fetch forward returns (h=1, h=5, h=20) via yfinance and join them onto
   the predictions. Writes data/stockpicker/ledger/scored.jsonl.

3. `signal_ic(horizon="r5")` — rank correlation (Spearman) between each stored
   signal contribution and realized forward return, across all scored rows. This
   is the empirical IC that confidence.py can eventually consume instead of priors.

Run:
    python -m src.stockpicker.ledger score        # backfill realized returns
    python -m src.stockpicker.ledger ic r5         # show measured IC per signal
"""
from __future__ import annotations

import json
import sys
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "stockpicker"
LEDGER_DIR = DATA_DIR / "ledger"
LEDGER_DIR.mkdir(parents=True, exist_ok=True)
PRED_PATH = LEDGER_DIR / "predictions.jsonl"
SCORED_PATH = LEDGER_DIR / "scored.jsonl"

# Forward-return horizons (trading days). Longer horizons support the
# "hold long" thesis — a short-term market downturn shouldn't invalidate a
# multi-week/multi-month hold, so we track out to ~3 months (r60).
HORIZONS = {"r1": 1, "r5": 5, "r10": 10, "r20": 20, "r60": 60}


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r, default=str) for r in rows) + ("\n" if rows else ""))


def record(records: list[dict], as_of: str) -> int:
    """Append per-candidate predictions for `as_of`. Overwrites that date if re-run."""
    rows = [r for r in _read_jsonl(PRED_PATH) if r.get("as_of") != as_of]
    for r in records:
        rows.append({
            "as_of": as_of,
            "ticker": r["ticker"],
            "composite": r.get("composite"),
            "last_close": r.get("last_close"),
            "risk_tier": r.get("risk_tier"),
            "contrib": r.get("contrib", {}),
        })
    rows.sort(key=lambda x: (x["as_of"], -(x.get("composite") or 0)))
    _write_jsonl(PRED_PATH, rows)
    return len(records)


def _forward_returns(tickers: list[str], start: str, max_h: int = max(HORIZONS.values())) -> dict:
    """Realized forward returns from `start` close, per ticker, at each horizon."""
    import yfinance as yf
    start_d = date.fromisoformat(start)
    end_d = start_d + timedelta(days=max_h * 2 + 10)  # calendar buffer for trading days
    dl = yf.download(tickers, start=start, end=end_d.isoformat(),
                     auto_adjust=True, progress=False)
    if dl.empty:
        return {}
    close = dl["Close"] if "Close" in dl else dl
    out: dict[str, dict] = {}
    for t in tickers:
        try:
            s = close[t].dropna() if t in close.columns else None
        except Exception:
            s = None
        if s is None or len(s) < 2:
            continue
        base = float(s.iloc[0])
        rets = {}
        for name, h in HORIZONS.items():
            if len(s) > h and base:
                rets[name] = float(s.iloc[h] / base - 1.0)
        if rets:
            out[t] = rets
    return out


def _complete(row: dict) -> bool:
    """True once every horizon has a resolved (non-null) return."""
    return all(row.get(k) is not None for k in HORIZONS)


def score(min_age_days: int = 5) -> int:
    """Join realized forward returns onto predictions, backfilling horizons as they mature.

    Runs as an *upsert*, not append-once. A prediction row stays "open" until every
    horizon (r1..r60) has resolved; on each run we re-fetch the still-open rows and
    fill in whichever longer horizons have since matured. This fixes the prior bug
    where a row scored at ~5 calendar days captured only r1 (too few trading days of
    forward data existed yet) and was never revisited — so r5..r60 stayed permanently
    null and the running weights could never learn from realized outcomes.
    """
    preds = _read_jsonl(PRED_PATH)
    if not preds:
        print("[ledger] no predictions recorded yet"); return 0
    today = date.today()
    by_date: dict[str, list[dict]] = {}
    for p in preds:
        by_date.setdefault(p["as_of"], []).append(p)

    scored_by_key = {(r["as_of"], r["ticker"]): r for r in _read_jsonl(SCORED_PATH)}
    changed = 0
    for as_of, rows in sorted(by_date.items()):
        age = (today - date.fromisoformat(as_of)).days
        if age < min_age_days:
            continue
        # Re-score any row that is new OR still has an unresolved horizon.
        pending = [r for r in rows
                   if not _complete(scored_by_key.get((as_of, r["ticker"]), {}))]
        if not pending:
            continue
        fr = _forward_returns([r["ticker"] for r in pending], as_of)
        for r in pending:
            rets = fr.get(r["ticker"])
            if not rets:
                continue
            key = (as_of, r["ticker"])
            existing = scored_by_key.get(key)
            merged = dict(existing) if existing else {**r, **{k: None for k in HORIZONS}}
            filled = 0
            for k in HORIZONS:
                if merged.get(k) is None and rets.get(k) is not None:
                    merged[k] = rets[k]
                    filled += 1
            if existing is None or filled:
                scored_by_key[key] = merged
                changed += 1
        print(f"[ledger] scored {as_of} (age {age}d): {len(fr)} names")
    out = list(scored_by_key.values())
    _write_jsonl(SCORED_PATH, out)
    print(f"[ledger] {changed} rows updated (total {len(out)})")
    return changed


def signal_ic(horizon: str = "r5") -> dict:
    """Spearman rank IC between each signal contribution and realized return."""
    rows = [r for r in _read_jsonl(SCORED_PATH) if r.get(horizon) is not None]
    if len(rows) < 10:
        print(f"[ledger] only {len(rows)} scored rows — need >=10 for a stable IC")
        return {}
    df = pd.DataFrame(rows)
    # Keep NaN: a signal added later has no observation on older rows, and
    # filling 0.0 would fabricate a run of zeros that drags its IC toward zero.
    contrib = pd.json_normalize(df["contrib"])
    y = df[horizon].astype(float)
    ics = {}
    for col in contrib.columns:
        x = contrib[col].astype(float)
        if x.nunique(dropna=True) < 3 or x.notna().sum() < 10:
            continue
        ics[col] = float(x.corr(y, method="spearman"))
    # composite IC too
    ics["__composite__"] = float(df["composite"].astype(float).corr(y, method="spearman"))
    return dict(sorted(ics.items(), key=lambda kv: -abs(kv[1])))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "score"
    if cmd == "score":
        score()
    elif cmd == "ic":
        h = sys.argv[2] if len(sys.argv) > 2 else "r5"
        ic = signal_ic(h)
        print(f"\nMeasured IC ({h}) — Spearman, signal contribution vs realized return:")
        for k, v in ic.items():
            print(f"  {k:<20} {v:+.3f}")
    else:
        print("usage: python -m src.stockpicker.ledger [score|ic r5]")
