"""
run.py — The on-demand stock-picker pipeline (what the `stock-picker` skill calls).

Light by design. Replaces the old "score all 330 names" approach with a top-down
funnel that mirrors how Nick reads the finviz map:

    macro overlay (global regime)
        -> sector scan (which sectors are moving)
            -> deviation (which names break from their sector) + volatility movers
                -> ~55 candidates
                    -> enrich ONLY those (fundamentals + earnings)
                        -> composite score (cached confidence weights)
                            -> ranked picks across the risk spectrum
                                -> markdown report

Heavy IC backtest is decoupled: weights are loaded from cache
(data/stockpicker/universe_scores.json). Re-run engine.py to recalibrate weights.

Run:  python -m src.stockpicker.run
Output: data/stockpicker/final_picks.json + Projects/stock-picker/REPORT-<date>.md
"""
from __future__ import annotations

import json
import warnings
from datetime import datetime, timezone, date
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import yfinance as yf

from src.stockpicker import sectors, macro, finviz_data, options_data, ledger
from src.stockpicker.engine import compute_price_signals_at, cross_sectional_z, PRICE_SIGNALS
from src.stockpicker.confidence import build_confidence_matrix, IC_PRIORS
from src.stockpicker.risk import classify_risk
from src.stockpicker.report import enrich, _z

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "stockpicker"
VAULT_REPORT_DIR = Path("/mnt/c/Users/nicky/Dopple/Projects/stock-picker")
N_PICKS = 20


def load_weights() -> dict:
    """Load cached confidence weights; fall back to priors if not yet calibrated."""
    p = DATA_DIR / "universe_scores.json"
    if p.exists():
        try:
            return json.loads(p.read_text())["weights"]
        except Exception:
            pass
    # priors-only fallback
    total = sum(IC_PRIORS.values())
    return {k: v / total for k, v in IC_PRIORS.items()}


def main(write_report: bool = True) -> dict:
    print("[run] global macro overlay ...")
    mac = macro.scan()
    print(macro.summary_text(mac))

    print("\n[run] sector scan + candidate selection ...")
    scan = sectors.scan()
    print(sectors.heatmap_text(scan))
    cand_tickers = scan["candidate_tickers"]
    print(f"\n[run] {len(cand_tickers)} candidates from sector deviation + volatility")

    # --- price signals on candidates (need ~1y history) ---
    dl = yf.download(cand_tickers, period="1y", auto_adjust=True, progress=False)
    close = dl["Close"]
    open_ = dl["Open"]
    good = [t for t in cand_tickers if t in close.columns and close[t].notna().sum() > 60]
    close, open_ = close[good], open_[good]
    t_now = len(close) - 1
    cur = compute_price_signals_at(close, open_, t_now)
    z_price = {k: cross_sectional_z(v) for k, v in cur.items()}

    weights = load_weights()

    # --- enrich candidates (fundamentals + earnings) ---
    print(f"[run] enriching {len(good)} candidates ...")
    enriched = enrich(good)

    last_close = close.iloc[-1]
    cand_by_t = {c["ticker"]: c for c in scan["candidates"]}

    # data-dependent signals across candidate set.
    # DATA-QUALITY GUARD: implausible earnings-surprise % (small-EPS-denominator
    # artifacts, e.g. INTC +2109%, DCH +1154%) are excluded from the SUE/PEAD
    # inputs so they neither inflate that name's composite nor skew the
    # cross-sectional z-score of everyone else. They are flagged on the record.
    SURPRISE_CAP = 300.0  # |surprise %| above this is treated as a data artifact
    rev_vals, sue_vals, pead_vals = {}, {}, {}
    dq_flags: dict[str, list] = {}
    for t in good:
        info = enriched[t]["info"]; price = float(last_close[t])
        flags = []
        tgt = info.get("targetMeanPrice")
        rev_vals[t] = ((tgt - price) / price) if (tgt and price) else np.nan
        sp = enriched[t]["last_surprise_pct"]; dse = enriched[t]["days_since_earnings"]
        if sp is not None and abs(sp) > SURPRISE_CAP:
            flags.append(f"implausible surprise {sp:+.0f}% (excluded)")
            sp = None  # drop the artifact from the signal
        if price < 10:
            flags.append("sub-$10 (low data quality)")
        sue_vals[t] = sp if sp is not None else np.nan
        pead_vals[t] = (sp * max(0.0, 1 - dse / 45.0)
                        if (sp is not None and dse is not None and 0 <= dse <= 45) else np.nan)
        dq_flags[t] = flags
    z_rev, z_sue, z_pead = _z(rev_vals), _z(sue_vals), _z(pead_vals)

    # --- FINVIZ enrichment (full 72-field snapshot, squeeze, signals) ---
    # No API key. Validated live vs Nick's MU screenshots. Every snapshot is
    # persisted to build the correlation dataset ("track all values").
    print(f"[run] finviz enrichment for {len(good)} candidates (throttled) ...")
    fv = finviz_data.enrich_batch(good)
    finviz_data.persist_snapshots(fv, as_of=scan["date_end"])
    n_ok = sum(1 for t in good if fv[t].get("ok"))
    print(f"[run] finviz coverage: {n_ok}/{len(good)} candidates")

    # --- OPTIONS enrichment (yfinance chain: ATM IV, put/call, OI) ---
    print(f"[run] options/IV enrichment for {len(good)} candidates ...")
    opt = options_data.enrich_batch(good)
    n_opt = sum(1 for t in good if opt[t].get("ok"))
    print(f"[run] options coverage: {n_opt}/{len(good)} candidates")
    osig = lambda key: {t: options_data.signals(opt[t]).get(key) for t in good}
    z_pc = _z({t: (v if v is not None else np.nan) for t, v in osig("opt_pc_sentiment").items()})

    # finviz-derived cross-sectional signals
    sq_vals = {t: (fv[t]["squeeze"]["score"] if fv[t].get("ok") else np.nan) for t in good}
    fsig = lambda key: {t: (fv[t]["signals"].get(key) if fv[t].get("ok") else None) for t in good}
    z_squeeze = _z({t: (v if v is not None else np.nan) for t, v in sq_vals.items()})
    z_recom = _z({t: (x if x is not None else np.nan) for t, x in fsig("fv_recom").items()})
    z_quality = _z({t: (x if x is not None else np.nan) for t, x in fsig("fv_quality").items()})
    z_short = _z({t: (x if x is not None else np.nan) for t, x in fsig("fv_short_pressure").items()})
    fv_target = fsig("fv_target_upside")

    # --- composite ---
    records = []
    for t in good:
        comp = 0.0; contrib = {}
        for name in PRICE_SIGNALS:
            c = weights.get(name, 0.0) * float(z_price[name].reindex([t]).iloc[0])
            contrib[name] = c; comp += c
        comp += (weights.get("revision_proxy", IC_PRIORS["revision_proxy"]) * z_rev[t]
                 + weights.get("earnings_sue", IC_PRIORS["earnings_sue"]) * z_sue[t]
                 + weights.get("pead_drift", IC_PRIORS["pead_drift"]) * z_pead[t])
        # finviz contributions (short weighted heavily per Nick's directive)
        comp += (IC_PRIORS["short_squeeze"] * z_squeeze[t]
                 + IC_PRIORS["short_pressure"] * z_short[t]
                 + IC_PRIORS["analyst_recom"] * z_recom[t]
                 + IC_PRIORS["quality"] * z_quality[t])
        contrib["short_squeeze"] = IC_PRIORS["short_squeeze"] * z_squeeze[t]
        contrib["analyst_recom"] = IC_PRIORS["analyst_recom"] * z_recom[t]
        contrib["quality"] = IC_PRIORS["quality"] * z_quality[t]
        # options positioning (put/call); IV level is risk-only, not directional
        comp += IC_PRIORS["opt_pc_sentiment"] * z_pc[t]
        contrib["opt_pc_sentiment"] = IC_PRIORS["opt_pc_sentiment"] * z_pc[t]
        rp = classify_risk(t, enriched[t]["info"])
        info = enriched[t]["info"]
        sq = fv[t].get("squeeze", {}) if fv[t].get("ok") else {}
        records.append({
            "ticker": t, "name": info.get("shortName") or t,
            "sector": rp.sector, "risk_tier": rp.tier, "risk_score": rp.score,
            "risk_reasons": rp.reasons, "composite": comp,
            "data_quality_flags": dq_flags.get(t, []),
            "selection_reason": cand_by_t.get(t, {}).get("reason"),
            "deviation_z": cand_by_t.get(t, {}).get("deviation_z"),
            "sector_ret_5d_pct": cand_by_t.get(t, {}).get("sector_ret_5d_pct"),
            "last_close": float(last_close[t]),
            "overnight_gap_vol_pct": cand_by_t.get(t, {}).get("overnight_gap_vol_pct"),
            "revision_upside_pct": (rev_vals[t] * 100 if not np.isnan(rev_vals[t]) else None),
            "last_surprise_pct": (sue_vals[t] if not np.isnan(sue_vals[t]) else None),
            "next_earnings": enriched[t]["next_earnings"],
            "days_to_earnings": enriched[t]["days_to_earnings"],
            "trailing_pe": rp.trailing_pe, "forward_pe": rp.forward_pe,
            "beta": rp.beta, "revenue_growth": rp.revenue_growth,
            "market_cap": rp.market_cap, "dividend_yield": rp.dividend_yield,
            "target_mean_price": info.get("targetMeanPrice"),
            # finviz fields
            "squeeze_score": sq.get("score"),
            "squeeze_label": sq.get("label"),
            "short_float_pct": sq.get("short_float"),
            "short_ratio": sq.get("short_ratio"),
            "finviz_target_upside_pct": fv_target.get(t),
            "finviz_recom": (fv[t]["snapshot"].get("analyst_recom") if fv[t].get("ok") else None),
            "finviz_roe": (fv[t]["snapshot"].get("roe") if fv[t].get("ok") else None),
            "finviz_roic": (fv[t]["snapshot"].get("roic") if fv[t].get("ok") else None),
            # options fields
            "atm_iv_pct": (round(opt[t]["atm_iv"] * 100, 1) if opt[t].get("atm_iv") else None),
            "iv_event_premium": opt[t].get("iv_slope"),
            "put_call_vol": opt[t].get("put_call_vol"),
            "put_call_oi": opt[t].get("put_call_oi"),
            "options_oi": opt[t].get("total_oi"),
            "contrib": {k: round(float(v), 4) for k, v in contrib.items()},
        })

    records.sort(key=lambda r: r["composite"], reverse=True)

    # prediction ledger — record ALL candidates (not just picks) so signal IC can
    # be measured later against realized forward returns.
    try:
        n_rec = ledger.record(records, as_of=scan["date_end"])
        print(f"[run] ledger: recorded {n_rec} predictions for {scan['date_end']}")
    except Exception as e:
        print(f"[run] ledger record skipped: {e}")

    # Spread across risk spectrum. Prefer POSITIVE-composite names; only backfill
    # with negative-composite names if a tier can't be filled otherwise (and they
    # stay flagged by their negative score in the report).
    quotas = {"CONSERVATIVE": 6, "MODERATE": 6, "AGGRESSIVE": 5, "SPECULATIVE": 3}
    counts = {k: 0 for k in quotas}
    picks = []
    positives = [r for r in records if r["composite"] > 0]
    for r in positives:
        if counts[r["risk_tier"]] < quotas[r["risk_tier"]]:
            picks.append(r); counts[r["risk_tier"]] += 1
        if len(picks) >= N_PICKS:
            break
    # backfill remaining slots, positives first then negatives
    if len(picks) < N_PICKS:
        for r in records:
            if len(picks) >= N_PICKS:
                break
            if r not in picks:
                picks.append(r)
                counts[r["risk_tier"]] = counts.get(r["risk_tier"], 0) + 1

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of_date": scan["date_end"],
        "macro": mac, "sectors": scan["sectors"],
        "n_candidates": len(good), "tier_counts": counts, "picks": picks,
    }
    (DATA_DIR / "final_picks.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"\n[run] tier counts: {counts}")
    for p in picks:
        print(f"  {p['ticker']:<6} {p['risk_tier']:<12} comp={p['composite']:+.2f} "
              f"{p['selection_reason']}")

    if write_report:
        md = render_markdown(out)
        rp_path = VAULT_REPORT_DIR / f"REPORT-{date.today().isoformat()}.md"
        rp_path.write_text(md)
        print(f"\n[run] report -> {rp_path}")
    return out


def render_markdown(out: dict) -> str:
    mac = out["macro"]
    L = []
    L.append(f"# Stock Picker — {out['as_of_date']}\n")
    L.append(f"*Generated {out['generated_at'][:16]}Z · sector-driven · "
             f"{out['n_candidates']} candidates from sector deviation + volatility*\n")
    L.append(f"\n## Global macro — regime: **{mac['regime'].upper()}** "
             f"(breadth {mac['breadth']*100:.0f}% regions up)\n")
    for region, r in mac["regions"].items():
        r5 = "n/a" if r["r5d"] is None else f"{r['r5d']*100:+.2f}%"
        w = f" _(weight {r['weight']})_" if r["weight"] != 1.0 else ""
        L.append(f"- **{region}** 5d {r5}{w}")
    if mac["lead_lag_signal_1d"] is not None:
        L.append(f"- _Asia/Europe lead-lag (1d): {mac['lead_lag_signal_1d']*100:+.2f}% "
                 f"→ read-through to US open_")
    L.append("\n## Sector heatmap (5-day, cap-weighted)\n")
    L.append("| Sector | 5d | 1d | 20d | n |")
    L.append("|--------|----|----|-----|---|")
    for s in out["sectors"]:
        L.append(f"| {s['sector']} | {s['ret_5d']*100:+.2f}% | {s['ret_1d']*100:+.2f}% "
                 f"| {s['ret_20d']*100:+.2f}% | {s['n']} |")
    tiers = ["CONSERVATIVE", "MODERATE", "AGGRESSIVE", "SPECULATIVE"]
    emoji = {"CONSERVATIVE": "🟢", "MODERATE": "🟡", "AGGRESSIVE": "🟠", "SPECULATIVE": "🔴"}
    L.append("\n## Picks across the risk spectrum\n")
    for tier in tiers:
        names = [p for p in out["picks"] if p["risk_tier"] == tier]
        if not names:
            continue
        L.append(f"\n### {emoji[tier]} {tier}\n")
        for p in names:
            pe = p["trailing_pe"]; fpe = p["forward_pe"]
            earn = (f"reports in {p['days_to_earnings']}d ({p['next_earnings']})"
                    if p["days_to_earnings"] is not None else "no date")
            up = (f"{p['revision_upside_pct']:+.0f}% target upside"
                  if p["revision_upside_pct"] is not None else "no target")
            L.append(f"- **{p['ticker']}** ({p['name']}) — comp **{p['composite']:+.2f}**, "
                     f"${p['last_close']:.2f} · {p['sector']}")
            L.append(f"  - selected as *{p['selection_reason']}* (sector dev "
                     f"{p['deviation_z']:+.2f}); P/E {pe} → fwd {fpe}, beta {p['beta']}, "
                     f"onVol {p.get('overnight_gap_vol_pct') or 0:.1f}%")
            sq = p.get("squeeze_score")
            sqline = ""
            if sq is not None:
                sqline = (f"squeeze {sq:.0f}/100 ({p.get('squeeze_label')}), "
                          f"short float {p.get('short_float_pct')}% / {p.get('short_ratio')}d-cover; ")
            recom = p.get("finviz_recom")
            L.append(f"  - {sqline}finviz recom {recom if recom is not None else 'n/a'} "
                     f"(1=buy), ROE {p.get('finviz_roe')}% / ROIC {p.get('finviz_roic')}%")
            iv = p.get("atm_iv_pct")
            if iv is not None:
                ep = p.get("iv_event_premium")
                ep_txt = (f", front-loaded IV (event premium {ep:+.3f})"
                          if (ep is not None and ep > 0.01) else "")
                L.append(f"  - options: ATM IV {iv:.0f}%{ep_txt}; put/call "
                         f"{p.get('put_call_vol')} (vol) / {p.get('put_call_oi')} (OI), "
                         f"OI {p.get('options_oi')}")
            L.append(f"  - {up}; last surprise "
                     f"{('%+.0f%%' % p['last_surprise_pct']) if p['last_surprise_pct'] is not None else 'n/a'}; "
                     f"{earn}")
            if p.get("data_quality_flags"):
                L.append(f"  - ⚠️ **data-quality:** {'; '.join(p['data_quality_flags'])}")
            if p["composite"] < 0:
                L.append(f"  - ⚠️ **negative composite** — model does not favor this; "
                         f"shown only to fill the {p['risk_tier'].lower()} slot")
    L.append("\n---\n*Methodology: [[Projects/stock-picker/METHODOLOGY]] · "
             "Data plan: [[Projects/stock-picker/DATA-SOURCES]] · "
             "Analysis only — not a trade recommendation.*")
    return "\n".join(L)


if __name__ == "__main__":
    main()
