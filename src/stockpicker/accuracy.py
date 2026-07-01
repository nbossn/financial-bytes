"""
accuracy.py — close the loop: did the picks play out, and how good was each signal?

The prediction ledger (ledger.py) stores, per run, every candidate's composite
score + per-signal contributions, and later joins the realized forward returns
(h=1/5/20). This module turns that into:

1. **Composite accuracy** — across all scored predictions: directional hit rate
   (did a positive composite actually go up?), rank IC (do higher composites earn
   higher returns?), and the long/short spread (mean return of positive-composite
   names minus negative-composite names) — the bottom-line "were the recommended
   plays right?" number.

2. **Per-signal accuracy** — for every signal/indicator: measured IC (Spearman of
   that signal's contribution vs realized return), directional hit rate, ICIR
   (consistency across dates), and sample size. This says which indicators are
   actually pulling their weight.

3. **Running weights** — feeds the measured per-signal IC into the existing
   Bayesian confidence machinery (confidence.build_confidence_matrix), which
   shrinks each measured IC toward its literature prior by sample size. With no
   data the weights equal the priors; as outcomes accrue, the data takes over.
   Persisted to data/stockpicker/running_weights.json so the next run uses them.

4. **Scorecard** — a living markdown doc (best/worst calls, signal table, running
   weights) written to the vault, plus a section injected into each nightly report.

Run:
    python -m src.stockpicker.accuracy            # build everything, write scorecard
    python -m src.stockpicker.accuracy r1          # use the 1-day horizon
"""
from __future__ import annotations

import json
import sys
import warnings
from datetime import date
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from src.stockpicker import ledger, confidence

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "stockpicker"
WEIGHTS_PATH = DATA_DIR / "running_weights.json"
VAULT_DIR = Path("/mnt/c/Users/nicky/Dopple/Projects/stock-picker")
SCORECARD_PATH = VAULT_DIR / "SCORECARD.md"

DEFAULT_HORIZON = "r5"
MIN_OBS_FOR_WEIGHTS = 20   # below this, running weights == priors (too little data)


def _scored_df(horizon: str) -> pd.DataFrame:
    rows = [r for r in ledger._read_jsonl(ledger.SCORED_PATH) if r.get(horizon) is not None]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["realized"] = df[horizon].astype(float)
    df["composite"] = df["composite"].astype(float)
    return df


# ───────────────────────────── composite accuracy ─────────────────────────────

def composite_accuracy(horizon: str = DEFAULT_HORIZON) -> dict:
    df = _scored_df(horizon)
    if df.empty:
        return {"n": 0}
    pos = df[df["composite"] > 0]["realized"]
    neg = df[df["composite"] < 0]["realized"]
    # directional hit: sign(composite) == sign(realized)
    hits = ((df["composite"] > 0) & (df["realized"] > 0)) | \
           ((df["composite"] < 0) & (df["realized"] < 0))
    rank_ic = (df["composite"].corr(df["realized"], method="spearman")
               if df["composite"].nunique() > 2 else float("nan"))
    return {
        "n": int(len(df)),
        "n_dates": int(df["as_of"].nunique()),
        "hit_rate": float(hits.mean()),
        "rank_ic": float(rank_ic),
        "mean_ret_positive": (float(pos.mean()) if len(pos) else None),
        "mean_ret_negative": (float(neg.mean()) if len(neg) else None),
        "long_short_spread": (float(pos.mean() - neg.mean())
                              if len(pos) and len(neg) else None),
        "best_call": _extreme_call(df, best=True),
        "worst_call": _extreme_call(df, best=False),
    }


def _extreme_call(df: pd.DataFrame, best: bool) -> dict | None:
    """The positive-composite pick with the best/worst realized return."""
    longs = df[df["composite"] > 0]
    if longs.empty:
        return None
    row = longs.loc[longs["realized"].idxmax() if best else longs["realized"].idxmin()]
    return {"ticker": row["ticker"], "as_of": row["as_of"],
            "composite": round(float(row["composite"]), 2),
            "realized_pct": round(float(row["realized"]) * 100, 2)}


# ───────────────────────────── per-signal accuracy ─────────────────────────────

def _signal_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Explode the per-row contrib dict into a signal-contribution matrix."""
    contrib = pd.json_normalize(df["contrib"]).fillna(0.0)
    contrib.index = df.index
    return contrib


def signal_stats(horizon: str = DEFAULT_HORIZON) -> dict[str, dict]:
    """Per-signal {ic, n, icir, hit_rate} — the input to build_confidence_matrix."""
    df = _scored_df(horizon)
    if df.empty:
        return {}
    contrib = _signal_frame(df)
    y = df["realized"]
    stats: dict[str, dict] = {}
    for col in contrib.columns:
        x = contrib[col].astype(float)
        if (x != 0).sum() < 3 or x.nunique() < 3:
            continue
        ic = float(x.corr(y, method="spearman"))
        # directional hit rate for this signal's contribution
        nz = x != 0
        hit = float((((x[nz] > 0) & (y[nz] > 0)) | ((x[nz] < 0) & (y[nz] < 0))).mean())
        # ICIR: IC per date, then mean/std (needs >=2 dates)
        ics_by_date = []
        for _, g in df.groupby("as_of"):
            gx = _signal_frame(g)[col].astype(float)
            gy = g["realized"]
            if gx.nunique() > 2:
                ics_by_date.append(gx.corr(gy, method="spearman"))
        ics_by_date = [v for v in ics_by_date if not np.isnan(v)]
        icir = (float(np.mean(ics_by_date) / np.std(ics_by_date))
                if len(ics_by_date) >= 2 and np.std(ics_by_date) > 0 else float("nan"))
        stats[col] = {"ic": ic, "n": int(nz.sum()), "icir": icir, "hit_rate": hit}
    return stats


# ───────────────────────────── running weights ─────────────────────────────

def running_weights(horizon: str = DEFAULT_HORIZON, persist: bool = True) -> dict:
    """Build running weights from measured per-signal IC, shrunk toward priors."""
    measured = signal_stats(horizon)
    total_obs = max((m["n"] for m in measured.values()), default=0)
    matrix = confidence.build_confidence_matrix(
        measured, horizon_days=ledger.HORIZONS.get(horizon, 5))
    out = {
        "generated_at": date.today().isoformat(),
        "horizon": horizon,
        "max_obs_per_signal": int(total_obs),
        "using_measured": bool(total_obs >= MIN_OBS_FOR_WEIGHTS),
        "weights": matrix.weights,
        "rows": [
            {"signal": r.name, "ic_measured": r.ic_measured, "ic_prior": r.ic_prior,
             "n": r.n_obs, "lambda_prior": r.shrink_lambda, "ic_shrunk": r.ic_shrunk,
             "hit_rate": r.hit_rate, "weight": r.weight}
            for r in sorted(matrix.rows, key=lambda x: x.weight, reverse=True)
        ],
    }
    if persist:
        WEIGHTS_PATH.write_text(json.dumps(out, indent=2, default=str))
    return out


# ───────────────────────────── scorecard markdown ─────────────────────────────

def accuracy_by_horizon() -> list[dict]:
    """Composite accuracy at every horizon — the hold-length view.

    Answers 'the plays were right short-term but the market turned — how does the
    thesis look the longer we hold?' Only horizons with resolved data appear.
    """
    out = []
    for h, days in ledger.HORIZONS.items():
        ca = composite_accuracy(h)
        if ca.get("n", 0) > 0:
            out.append({"horizon": h, "days": days, **ca})
    return out


def render_scorecard(horizon: str = DEFAULT_HORIZON) -> str:
    ca = composite_accuracy(horizon)
    rw = running_weights(horizon, persist=True)
    preds = ledger._read_jsonl(ledger.PRED_PATH)
    pred_dates = sorted({p["as_of"] for p in preds})
    L = [f"# Stock Picker — Accuracy Scorecard",
         f"\n*Updated {date.today().isoformat()} · weight horizon {horizon} "
         f"({ledger.HORIZONS.get(horizon,5)}-day forward return) · "
         f"predicted vs realized · hold-long lens (tracks out to "
         f"{max(ledger.HORIZONS.values())}d)*\n"]

    byh = accuracy_by_horizon()

    if not byh:
        # nothing resolved at ANY horizon yet
        L.append("## Status: accruing — no realized outcomes yet\n")
        L.append(f"- **{len(preds)} predictions** recorded across **{len(pred_dates)} run-date(s)**: "
                 f"{', '.join(pred_dates) if pred_dates else 'none'}")
        L.append(f"- Forward returns resolve as trading days pass "
                 f"(`ledger score` runs nightly). Shortest horizon (1d) resolves first; "
                 f"the hold-long horizons (up to {max(ledger.HORIZONS.values())}d) fill in over time.")
        L.append("\n## Running weights (currently = literature priors)\n")
        L.append(_weights_table(rw))
        L.append("\n*Weights shift from priors to measured as outcomes accrue "
                 f"(threshold: {MIN_OBS_FOR_WEIGHTS} scored obs per signal).*")
        return "\n".join(L)

    # ── hold-length view (short-term vs longer holds) — the headline now ──
    L.append("## Performance by holding period (the hold-long view)\n")
    L.append("| Hold | hit rate | rank IC | long/short spread | avg (favored) | n |")
    L.append("|------|----------|---------|-------------------|---------------|---|")
    for r in byh:
        ric = "n/a" if np.isnan(r["rank_ic"]) else f"{r['rank_ic']:+.3f}"
        ls = ("n/a" if r["long_short_spread"] is None
              else f"{r['long_short_spread']*100:+.2f}%")
        mp = ("n/a" if r["mean_ret_positive"] is None
              else f"{r['mean_ret_positive']*100:+.2f}%")
        L.append(f"| {r['days']}d | {r['hit_rate']*100:.0f}% | {ric} | {ls} | {mp} | {r['n']} |")
    resolved = {r["days"] for r in byh}
    pending = [f"{d}d" for d in ledger.HORIZONS.values() if d not in resolved]
    pend_note = (f" Still resolving: {', '.join(pending)} — **the hold-long thesis is "
                 f"judged there**, not on the 1–5d rows." if pending else "")
    L.append(f"\n*A short-term drawdown at 1–5d can coexist with a positive longer hold — "
             f"read the trend down the table, not any single row.{pend_note}*\n")

    if ca["n"] == 0:
        L.append(f"> ⏳ The weight horizon (**{horizon}**, {ledger.HORIZONS.get(horizon,5)}d) "
                 f"hasn't resolved yet, so running weights below remain prior-dominated. "
                 f"The table above shows the horizons that *have* resolved.\n")
        L.append("## Running weights (currently = literature priors)\n")
        L.append(_weights_table(rw))
        return "\n".join(L)

    # ── headline accuracy (weight horizon) ──
    L.append(f"## How the recommended plays performed ({horizon}, "
             f"{ledger.HORIZONS.get(horizon,5)}d)\n")
    L.append(f"- **{ca['n']} scored predictions** over **{ca['n_dates']} run-date(s)**")
    L.append(f"- **Directional hit rate:** {ca['hit_rate']*100:.1f}% "
             f"(composite sign matched realized move)")
    if not np.isnan(ca["rank_ic"]):
        L.append(f"- **Rank IC (composite → return):** {ca['rank_ic']:+.3f} "
                 f"(higher composite → higher realized return)")
    if ca["long_short_spread"] is not None:
        L.append(f"- **Long/short spread:** {ca['long_short_spread']*100:+.2f}% "
                 f"(positive-composite avg {ca['mean_ret_positive']*100:+.2f}% "
                 f"vs negative-composite avg {ca['mean_ret_negative']*100:+.2f}%)")
    if ca.get("best_call"):
        b = ca["best_call"]
        L.append(f"- **Best call:** {b['ticker']} ({b['as_of']}) comp {b['composite']:+.2f} "
                 f"→ {b['realized_pct']:+.2f}%")
    if ca.get("worst_call"):
        w = ca["worst_call"]
        L.append(f"- **Worst call:** {w['ticker']} ({w['as_of']}) comp {w['composite']:+.2f} "
                 f"→ {w['realized_pct']:+.2f}%")

    # ── per-signal table ──
    L.append("\n## Signal accuracy (which indicators are working)\n")
    L.append("| Signal | IC (meas) | hit% | n | weight |")
    L.append("|--------|-----------|------|---|--------|")
    for r in rw["rows"]:
        ic = "n/a" if (r["ic_measured"] is None or np.isnan(r["ic_measured"])) else f"{r['ic_measured']:+.3f}"
        hit = "n/a" if (r["hit_rate"] is None or np.isnan(r["hit_rate"])) else f"{r['hit_rate']*100:.0f}%"
        L.append(f"| {r['signal']} | {ic} | {hit} | {r['n']} | {r['weight']*100:.1f}% |")

    L.append(f"\n## Running weights ({'MEASURED' if rw['using_measured'] else 'still prior-dominated'})\n")
    L.append(_weights_table(rw))
    L.append(f"\n*Source: {WEIGHTS_PATH}. Used by the next pipeline run. Weights = "
             f"Bayesian shrink of measured IC toward literature priors by sample size.*")
    return "\n".join(L)


def _weights_table(rw: dict) -> str:
    rows = sorted(rw["rows"], key=lambda r: r["weight"], reverse=True)
    out = ["| Signal | weight | IC* (shrunk) | λ→prior | n |",
           "|--------|--------|--------------|---------|---|"]
    for r in rows:
        out.append(f"| {r['signal']} | {r['weight']*100:.1f}% | {r['ic_shrunk']:+.4f} "
                   f"| {r['lambda_prior']:.2f} | {r['n']} |")
    return "\n".join(out)


def write_scorecard(horizon: str = DEFAULT_HORIZON) -> Path:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    SCORECARD_PATH.write_text(render_scorecard(horizon))
    return SCORECARD_PATH


if __name__ == "__main__":
    h = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HORIZON
    print(render_scorecard(h))
    p = write_scorecard(h)
    print(f"\n[accuracy] scorecard -> {p}")
    print(f"[accuracy] running weights -> {WEIGHTS_PATH}")
