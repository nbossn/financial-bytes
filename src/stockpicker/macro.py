"""
macro.py — Global market momentum overlay (US / Europe / Asia / Middle-East).

A top-down macro read, not a per-stock signal. Answers: is the global tape
risk-on or risk-off, and which region is leading? Asia and Europe trade before
the US open, so their recent move is a leading indicator for the US session.

Per Nick's direction: Europe and Asia at full weight, Middle-East included but
DOWN-WEIGHTED (0.3x) so it informs without dominating.

Indices (all free via yfinance):
  US:      ^GSPC, ^NDX, ^RUT
  Europe:  ^STOXX50E, ^GDAXI, ^FTSE
  Asia:    ^N225, ^HSI, 000001.SS, ^KS11
  M-East:  ^TASI.SR, ^TA125.TA   (weight 0.3)
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import yfinance as yf

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "stockpicker"

REGIONS = {
    "US":         (["^GSPC", "^NDX", "^RUT"], 1.0),
    "Europe":     (["^STOXX50E", "^GDAXI", "^FTSE"], 1.0),
    "Asia":       (["^N225", "^HSI", "000001.SS", "^KS11"], 1.0),
    "MiddleEast": (["^TASI.SR", "^TA125.TA"], 0.3),
}


def _ret(close, idx, n):
    try:
        s = close[idx].dropna()
        if len(s) <= n:
            return None
        return float(s.iloc[-1] / s.iloc[-1 - n] - 1.0)
    except Exception:
        return None


def scan() -> dict:
    all_idx = [t for v in REGIONS.values() for t in v[0]]
    dl = yf.download(all_idx, period="2mo", auto_adjust=True, progress=False)
    close = dl["Close"]

    regions = {}
    for region, (idxs, weight) in REGIONS.items():
        per = {}
        for ix in idxs:
            per[ix] = {"r1d": _ret(close, ix, 1), "r5d": _ret(close, ix, 5),
                       "r20d": _ret(close, ix, 20)}
        def agg(key):
            vals = [per[ix][key] for ix in idxs if per[ix][key] is not None]
            return float(np.mean(vals)) if vals else None
        regions[region] = {
            "weight": weight,
            "indices": per,
            "r1d": agg("r1d"), "r5d": agg("r5d"), "r20d": agg("r20d"),
        }

    # Weighted global momentum (ME at 0.3x).
    def weighted(key):
        num = den = 0.0
        for region, r in regions.items():
            if r[key] is not None:
                num += r["weight"] * r[key]; den += r["weight"]
        return (num / den) if den else None
    global_5d = weighted("r5d")
    global_1d = weighted("r1d")

    # Breadth: fraction of regions positive on 5d (ME counted lightly).
    pos = sum(r["weight"] for r in regions.values() if (r["r5d"] or 0) > 0)
    tot = sum(r["weight"] for r in regions.values() if r["r5d"] is not None)
    breadth = (pos / tot) if tot else 0.5

    # Risk regime label.
    if global_5d is None:
        regime = "unknown"
    elif global_5d > 0.01 and breadth >= 0.6:
        regime = "risk_on"
    elif global_5d < -0.01 and breadth <= 0.4:
        regime = "risk_off"
    else:
        regime = "neutral"

    # Lead-lag: Asia+Europe recent 1d (they close before US open).
    lead = []
    for r in ("Asia", "Europe"):
        if regions[r]["r1d"] is not None:
            lead.append(regions[r]["r1d"])
    lead_signal = float(np.mean(lead)) if lead else None

    payload = {
        "regions": regions,
        "global_momentum_5d": global_5d,
        "global_momentum_1d": global_1d,
        "breadth": breadth,
        "regime": regime,
        "lead_lag_signal_1d": lead_signal,   # Asia/Europe read-through to US open
    }
    (DATA_DIR / "macro_scan.json").write_text(json.dumps(payload, indent=2, default=str))
    return payload


def summary_text(p: dict) -> str:
    lines = [f"GLOBAL MACRO  —  regime: {p['regime'].upper()}  "
             f"(breadth {p['breadth']*100:.0f}% regions up)", "-" * 60]
    for region, r in p["regions"].items():
        w = f"(weight {r['weight']})" if r["weight"] != 1.0 else ""
        r5 = "n/a" if r["r5d"] is None else f"{r['r5d']*100:+.2f}%"
        r1 = "n/a" if r["r1d"] is None else f"{r['r1d']*100:+.2f}%"
        lines.append(f"  {region:<11} 5d {r5:<9} 1d {r1:<9} {w}")
    if p["lead_lag_signal_1d"] is not None:
        lines.append(f"  Asia/Europe lead-lag (1d): {p['lead_lag_signal_1d']*100:+.2f}% "
                     f"→ read-through to US open")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary_text(scan()))
