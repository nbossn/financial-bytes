"""
report.py — Enrichment + final pick selection (stage 2/3 of the funnel).

Takes the universe scores from engine.py, enriches the top candidates with
fundamentals (for risk classification + revision proxy) and earnings dates (for
timing + SUE/PEAD), folds those data-dependent signals into the composite, then
selects ~20 picks spread across the risk spectrum.

Run:  python -m src.stockpicker.report
Outputs: data/stockpicker/picks.json
"""
from __future__ import annotations

import json
import warnings
from datetime import datetime, timezone, date
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import yfinance as yf

from src.stockpicker.risk import classify_risk
from src.stockpicker.confidence import IC_PRIORS

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "stockpicker"

# How many top-composite names to deep-enrich.
ENRICH_TOP = 70
# Final pick count.
N_PICKS = 20


def _z(values: dict[str, float]) -> dict[str, float]:
    """Robust cross-sectional z across the enriched candidate set."""
    arr = np.array([v for v in values.values() if v is not None and not np.isnan(v)])
    if len(arr) < 3:
        return {k: 0.0 for k in values}
    med = np.median(arr)
    mad = np.median(np.abs(arr - med))
    scale = 1.4826 * mad if mad > 0 else np.std(arr)
    out = {}
    for k, v in values.items():
        if v is None or np.isnan(v):
            out[k] = 0.0
        else:
            out[k] = float(np.clip((v - med) / scale, -3, 3)) if scale else 0.0
    return out


def enrich(tickers: list[str]) -> dict[str, dict]:
    """Fetch info + earnings per ticker. Resilient: failures yield empty dicts."""
    out: dict[str, dict] = {}
    for i, tk in enumerate(tickers):
        rec: dict = {"info": {}, "next_earnings": None, "days_to_earnings": None,
                     "last_surprise_pct": None, "days_since_earnings": None}
        try:
            yt = yf.Ticker(tk)
            try:
                rec["info"] = yt.info or {}
            except Exception:
                rec["info"] = {}
            # earnings dates: estimate vs reported + surprise
            try:
                ed = yt.get_earnings_dates(limit=12)
            except Exception:
                ed = None
            if ed is not None and len(ed):
                today = datetime.now(timezone.utc)
                idx = ed.index
                # next (future) earnings
                future = idx[idx > today]
                if len(future):
                    nd = future.min()
                    rec["next_earnings"] = str(nd.date())
                    rec["days_to_earnings"] = int((nd.date() - date.today()).days)
                # last reported surprise
                past = ed[ed.index <= today]
                if len(past):
                    last = past.iloc[0]
                    sp = last.get("Surprise(%)")
                    if sp is not None and not (isinstance(sp, float) and np.isnan(sp)):
                        rec["last_surprise_pct"] = float(sp)
                    ld = past.index.max()
                    rec["days_since_earnings"] = int((date.today() - ld.date()).days)
        except Exception as e:
            rec["error"] = str(e)
        out[tk] = rec
        if (i + 1) % 10 == 0:
            print(f"  enriched {i+1}/{len(tickers)}")
    return out


def main():
    snap = json.loads((DATA_DIR / "universe_scores.json").read_text())
    weights = snap["weights"]
    ranked = snap["ranked"]
    by_ticker = {r["ticker"]: r for r in ranked}

    top = [r["ticker"] for r in ranked[:ENRICH_TOP]]
    print(f"[report] enriching top {len(top)} candidates ...")
    enriched = enrich(top)

    # --- Build data-dependent signals across the enriched set ---
    revision_vals: dict[str, float] = {}
    sue_vals: dict[str, float] = {}
    pead_vals: dict[str, float] = {}
    for tk in top:
        info = enriched[tk]["info"]
        price = by_ticker[tk]["last_close"]
        tgt = info.get("targetMeanPrice")
        revision_vals[tk] = ((tgt - price) / price) if (tgt and price) else np.nan
        sp = enriched[tk]["last_surprise_pct"]
        dse = enriched[tk]["days_since_earnings"]
        sue_vals[tk] = sp if sp is not None else np.nan
        # PEAD: surprise with linear decay over ~30 trading days since report
        if sp is not None and dse is not None and 0 <= dse <= 45:
            pead_vals[tk] = sp * max(0.0, 1.0 - dse / 45.0)
        else:
            pead_vals[tk] = np.nan

    z_rev = _z(revision_vals)
    z_sue = _z(sue_vals)
    z_pead = _z(pead_vals)

    # --- Fold data-dependent signals into the composite ---
    # Base composite already includes the price signals. We add the
    # data-dependent contributions using their prior weights.
    enriched_scores = {}
    for tk in top:
        base = by_ticker[tk]["composite"]
        add = (weights.get("revision_proxy", IC_PRIORS["revision_proxy"]) * z_rev[tk]
               + weights.get("earnings_sue", IC_PRIORS["earnings_sue"]) * z_sue[tk]
               + weights.get("pead_drift", IC_PRIORS["pead_drift"]) * z_pead[tk])
        enriched_scores[tk] = {
            "composite_full": base + add,
            "composite_price": base,
            "z_revision": z_rev[tk],
            "z_sue": z_sue[tk],
            "z_pead": z_pead[tk],
            "revision_upside_pct": (revision_vals[tk] * 100
                                    if not np.isnan(revision_vals[tk]) else None),
            "last_surprise_pct": (sue_vals[tk] if not np.isnan(sue_vals[tk]) else None),
        }

    # --- Risk classification ---
    risk = {tk: classify_risk(tk, enriched[tk]["info"]) for tk in top}

    # --- Rank by full composite ---
    ordered = sorted(top, key=lambda t: enriched_scores[t]["composite_full"],
                     reverse=True)

    # --- Select ~20 across the risk spectrum (quota per tier) ---
    quotas = {"CONSERVATIVE": 6, "MODERATE": 6, "AGGRESSIVE": 5, "SPECULATIVE": 3}
    picks: list[str] = []
    counts = {k: 0 for k in quotas}
    for tk in ordered:
        tier = risk[tk].tier
        if counts[tier] < quotas[tier]:
            picks.append(tk)
            counts[tier] += 1
        if len(picks) >= N_PICKS:
            break
    # backfill if any tier underfilled
    if len(picks) < N_PICKS:
        for tk in ordered:
            if tk not in picks:
                picks.append(tk)
            if len(picks) >= N_PICKS:
                break

    # --- Assemble pick records ---
    pick_records = []
    for tk in picks:
        r = by_ticker[tk]
        es = enriched_scores[tk]
        rp = risk[tk]
        info = enriched[tk]["info"]
        pick_records.append({
            "ticker": tk,
            "name": info.get("shortName") or info.get("longName") or tk,
            "sector": rp.sector,
            "risk_tier": rp.tier,
            "risk_score": rp.score,
            "risk_reasons": rp.reasons,
            "composite_full": es["composite_full"],
            "composite_price": es["composite_price"],
            "last_close": r["last_close"],
            "day_change_pct": r["day_change_pct"],
            "overnight_gap_vol_pct": r["overnight_gap_vol_pct"],
            "realized_vol_30d_pct": r["realized_vol_30d_pct"],
            "price_contributions": r["contributions"],
            "raw_price_signals": r["raw_signals"],
            "z_revision": es["z_revision"],
            "z_sue": es["z_sue"],
            "z_pead": es["z_pead"],
            "revision_upside_pct": es["revision_upside_pct"],
            "last_surprise_pct": es["last_surprise_pct"],
            "next_earnings": enriched[tk]["next_earnings"],
            "days_to_earnings": enriched[tk]["days_to_earnings"],
            "trailing_pe": rp.trailing_pe,
            "forward_pe": rp.forward_pe,
            "market_cap": rp.market_cap,
            "beta": rp.beta,
            "revenue_growth": rp.revenue_growth,
            "profit_margin": rp.profit_margin,
            "short_pct_float": rp.short_pct_float,
            "dividend_yield": rp.dividend_yield,
            "target_mean_price": info.get("targetMeanPrice"),
            "recommendation_key": info.get("recommendationKey"),
        })

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of_date": snap["date_end"],
        "vix": snap["vix_last"],
        "spy": snap["spy_last"],
        "n_enriched": len(top),
        "tier_counts": counts,
        "picks": pick_records,
    }
    out_path = DATA_DIR / "picks.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"[report] wrote {out_path}")
    print(f"[report] tier counts: {counts}")
    for p in pick_records:
        print(f"  {p['ticker']:<6} {p['risk_tier']:<12} comp={p['composite_full']:+.3f} "
              f"PE={p['trailing_pe']}")
    return out


if __name__ == "__main__":
    main()
