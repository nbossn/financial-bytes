"""
sectors.py — Top-down sector scan + deviation-based candidate selection.

This is the "finviz map" layer. Instead of scoring all ~330 names flat (too
heavy), we read the market the way Nick does: by sector first, then drill into
the names that are MOVING or DEVIATING from their sector. Those become the small
candidate set the composite engine then scores.

Pipeline
--------
1. Load the cached sector map (data/stockpicker/sector_map.json).
2. Batch-download recent prices for the universe (one fast call).
3. Compute per-sector aggregate returns (1d / 5d / 20d), market-cap weighted
   — this is the sector heatmap (size = cap, color = performance).
4. Compute each stock's DEVIATION from its sector (within-sector z-score of
   return) + its volatility (realized + overnight-gap).
5. Select candidates:
     - sector leaders/breakouts (high positive deviation in a moving sector)
     - sector laggards (high negative deviation — potential reversals)
     - high-volatility movers (large |deviation| or high realized vol)
   capped per sector so no single sector dominates.

The output is a ~40-60 name candidate list + the sector heatmap, both far
lighter than scoring the whole universe.
"""
from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import yfinance as yf

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "stockpicker"
SECTOR_MAP_PATH = DATA_DIR / "sector_map.json"

# Candidate-selection knobs.
PER_SECTOR_LEADERS = 3      # top positive deviators per sector
PER_SECTOR_LAGGARDS = 1     # top negative deviator per sector (reversal candidate)
N_VOL_MOVERS = 12           # extra high-volatility names across the whole universe
MAX_CANDIDATES = 55         # hard cap on the candidate set


@dataclass
class SectorReading:
    sector: str
    ret_1d: float
    ret_5d: float
    ret_20d: float
    n: int
    total_cap: float
    leaders: list[str] = field(default_factory=list)
    laggards: list[str] = field(default_factory=list)


def load_sector_map() -> dict[str, dict]:
    return json.loads(SECTOR_MAP_PATH.read_text())


def _wmean(values: np.ndarray, weights: np.ndarray) -> float:
    w = np.nan_to_num(weights, nan=0.0)
    v = np.array(values, dtype=float)
    mask = ~np.isnan(v) & (w > 0)
    if not mask.any():
        return float(np.nanmean(v)) if len(v) else float("nan")
    return float(np.average(v[mask], weights=w[mask]))


def scan(period: str = "3mo") -> dict:
    """Run the full sector scan + candidate selection. Returns a dict payload."""
    smap = load_sector_map()
    tickers = [t for t, d in smap.items() if d.get("sector")]

    dl = yf.download(tickers, period=period, auto_adjust=True, progress=False)
    close = dl["Close"]
    open_ = dl["Open"]
    good = [t for t in tickers if t in close.columns and close[t].notna().sum() > 25]
    close = close[good]
    open_ = open_[good]

    last = close.iloc[-1]
    r1 = close.iloc[-1] / close.iloc[-2] - 1.0
    r5 = close.iloc[-1] / close.iloc[-6] - 1.0 if len(close) > 6 else r1
    r20 = close.iloc[-1] / close.iloc[-21] - 1.0 if len(close) > 21 else r5
    # 30d realized daily vol + overnight-gap vol
    rvol = close.pct_change().iloc[-30:].std()
    gaps = open_.values[-30:] / close.values[-31:-1] - 1.0 if len(close) > 31 else None
    onvol = pd.Series(np.nanstd(gaps, axis=0), index=good) if gaps is not None else rvol

    caps = {t: (smap[t].get("market_cap") or 0.0) for t in good}
    cap_s = pd.Series(caps).reindex(good).fillna(0.0)

    # Group by sector
    by_sector: dict[str, list[str]] = {}
    for t in good:
        s = smap[t].get("sector") or "Unknown"
        by_sector.setdefault(s, []).append(t)

    readings: list[SectorReading] = []
    candidates: dict[str, dict] = {}

    for sector, names in by_sector.items():
        caps_arr = cap_s.reindex(names).values
        sret_1d = _wmean(r1.reindex(names).values, caps_arr)
        sret_5d = _wmean(r5.reindex(names).values, caps_arr)
        sret_20d = _wmean(r20.reindex(names).values, caps_arr)

        # within-sector deviation = z-score of 5d return vs sector peers
        s5 = r5.reindex(names).astype(float)
        med = np.nanmedian(s5.values)
        mad = np.nanmedian(np.abs(s5.values - med))
        scale = 1.4826 * mad if mad and mad > 0 else (np.nanstd(s5.values) or 1.0)
        dev = (s5 - med) / scale

        ranked = dev.sort_values(ascending=False)
        leaders = [t for t in ranked.index[:PER_SECTOR_LEADERS] if not np.isnan(ranked[t])]
        laggards = [t for t in ranked.index[-PER_SECTOR_LAGGARDS:] if not np.isnan(ranked[t])]

        readings.append(SectorReading(
            sector=sector, ret_1d=sret_1d, ret_5d=sret_5d, ret_20d=sret_20d,
            n=len(names), total_cap=float(np.nansum(caps_arr)),
            leaders=leaders, laggards=laggards,
        ))

        for t in set(leaders + laggards):
            candidates[t] = {
                "ticker": t, "sector": sector,
                "reason": ("sector_leader" if t in leaders else "sector_laggard"),
                "deviation_z": float(dev.get(t, 0.0)),
                "ret_5d_pct": float(s5.get(t, np.nan) * 100),
                "sector_ret_5d_pct": float(sret_5d * 100),
            }

    # High-volatility movers across the whole universe
    vol_rank = onvol.sort_values(ascending=False)
    for t in vol_rank.index[:N_VOL_MOVERS]:
        if t not in candidates:
            candidates[t] = {
                "ticker": t, "sector": smap[t].get("sector") or "Unknown",
                "reason": "high_volatility",
                "deviation_z": 0.0,
                "ret_5d_pct": float(r5.get(t, np.nan) * 100),
                "sector_ret_5d_pct": None,
            }
        candidates[t]["overnight_gap_vol_pct"] = float(onvol.get(t, np.nan) * 100)

    # Attach vol + price to all candidates; cap the set
    for t in candidates:
        candidates[t].setdefault("overnight_gap_vol_pct", float(onvol.get(t, np.nan) * 100))
        candidates[t]["realized_vol_30d_pct"] = float(rvol.get(t, np.nan) * 100)
        candidates[t]["last_close"] = float(last.get(t, np.nan))
        candidates[t]["day_change_pct"] = float(r1.get(t, np.nan) * 100)

    # Rank candidates: sector-leaders in strong sectors first, then |dev|, then vol
    def cand_priority(c):
        sector_mom = next((s.ret_5d for s in readings if s.sector == c["sector"]), 0.0)
        lead_bonus = 1.0 if c["reason"] == "sector_leader" else 0.0
        return (lead_bonus * max(sector_mom, 0) * 50
                + abs(c["deviation_z"])
                + 0.1 * (c.get("overnight_gap_vol_pct") or 0))

    ordered = sorted(candidates.values(), key=cand_priority, reverse=True)[:MAX_CANDIDATES]

    readings.sort(key=lambda s: s.ret_5d, reverse=True)
    payload = {
        "date_end": str(close.index[-1].date()),
        "universe_size": len(good),
        "sectors": [vars(s) for s in readings],
        "candidates": ordered,
        "candidate_tickers": [c["ticker"] for c in ordered],
    }
    out = DATA_DIR / "sector_scan.json"
    out.write_text(json.dumps(payload, indent=2, default=str))
    return payload


def heatmap_text(payload: dict) -> str:
    """Render the sector heatmap as text (the finviz-map summary)."""
    lines = [f"SECTOR HEATMAP  (as of {payload['date_end']}, 5-day cap-weighted)", "-" * 64]
    for s in payload["sectors"]:
        arrow = "▲" if s["ret_5d"] >= 0 else "▼"
        bar_n = int(min(abs(s["ret_5d"]) * 300, 30))
        bar = ("+" if s["ret_5d"] >= 0 else "-") * bar_n
        lines.append(f"  {s['sector']:<22} {arrow}{s['ret_5d']*100:+6.2f}%  "
                     f"(1d {s['ret_1d']*100:+5.2f}, 20d {s['ret_20d']*100:+6.2f}) "
                     f"n={s['n']:<3} {bar}")
    return "\n".join(lines)


if __name__ == "__main__":
    p = scan()
    print(heatmap_text(p))
    print(f"\n{len(p['candidates'])} candidates selected:")
    for c in p["candidates"]:
        print(f"  {c['ticker']:<6} {c['sector']:<22} {c['reason']:<14} "
              f"dev={c['deviation_z']:+.2f} onVol={c.get('overnight_gap_vol_pct',0):.1f}%")
