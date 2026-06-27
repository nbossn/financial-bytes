"""
viz.py — Visuals for the stock picker. Currently: the short-squeeze map.

Nick asked for "a better visual" for short interest and squeeze detection than
finviz's line charts. This renders a squeeze MAP: short float (x) vs days-to-cover
(y), bubble size = relative volume, color = squeeze score. Names in the upper-right
(high short float + high days-to-cover) are the squeeze-fuel quadrant; bigger,
hotter bubbles there are the candidates to watch.

Reads the persisted finviz snapshots for the run date.
Run: python -m src.stockpicker.viz [YYYY-MM-DD]
Output: Projects/stock-picker/squeeze-map-<date>.png
"""
from __future__ import annotations

import json
import sys
import warnings
from datetime import date
from pathlib import Path

warnings.filterwarnings("ignore")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "stockpicker"
SNAP_DIR = DATA_DIR / "finviz_snapshots"
OUT_DIR = Path("/mnt/c/Users/nicky/Dopple/Projects/stock-picker")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def squeeze_map(as_of: str | None = None) -> Path | None:
    as_of = as_of or date.today().isoformat()
    snap_path = SNAP_DIR / f"{as_of}.json"
    if not snap_path.exists():
        # fall back to the most recent snapshot file
        files = sorted(SNAP_DIR.glob("*.json"))
        if not files:
            print("[viz] no finviz snapshots found"); return None
        snap_path = files[-1]
        as_of = snap_path.stem
    data = json.loads(snap_path.read_text())

    pts = []
    for tk, e in data.items():
        snap = e.get("snapshot", {})
        sf = _f(snap.get("short_float"))
        sr = _f(snap.get("short_ratio"))
        rv = _f(snap.get("rel_volume")) or 1.0
        sc = e.get("squeeze", {}).get("score")
        if sf is None or sr is None:
            continue
        pts.append((tk, sf, sr, rv, sc if sc is not None else 0))

    if not pts:
        print("[viz] no short data to plot"); return None

    fig, ax = plt.subplots(figsize=(13, 8.5), facecolor="#0d1117")
    ax.set_facecolor("#0d1117")

    xs = [p[1] for p in pts]; ys = [p[2] for p in pts]
    sizes = [max(40, min(600, p[3] * 180)) for p in pts]
    colors = [p[4] for p in pts]

    sc = ax.scatter(xs, ys, s=sizes, c=colors, cmap="plasma", vmin=0, vmax=70,
                    alpha=0.85, edgecolors="white", linewidths=0.6, zorder=3)

    for tk, sf, sr, rv, score in pts:
        ax.annotate(tk, (sf, sr), color="white", fontsize=8.5, fontweight="bold",
                    xytext=(4, 4), textcoords="offset points", zorder=4)

    # squeeze-fuel quadrant guide lines
    ax.axvline(10, color="#ff5555", ls="--", lw=0.8, alpha=0.5)
    ax.axhline(3, color="#ff5555", ls="--", lw=0.8, alpha=0.5)
    ax.text(0.985, 0.97, "SQUEEZE-FUEL QUADRANT\n(high short float + slow to cover)",
            transform=ax.transAxes, ha="right", va="top", color="#ff7777",
            fontsize=9, alpha=0.8)

    cbar = plt.colorbar(sc, ax=ax)
    cbar.set_label("Squeeze setup score (0-100)", color="white")
    cbar.ax.yaxis.set_tick_params(color="white")
    plt.setp(plt.getp(cbar.ax.axes, "yticklabels"), color="white")

    ax.set_xlabel("Short Float (% of float sold short)  →  more squeeze fuel",
                  color="white", fontsize=11)
    ax.set_ylabel("Short Ratio (days-to-cover)  →  harder to unwind",
                  color="white", fontsize=11)
    ax.set_title(f"Short-Squeeze Map — stock-picker candidates ({as_of})\n"
                 f"bubble size = relative volume · color = squeeze score",
                 color="white", fontsize=13, fontweight="bold")
    ax.tick_params(colors="white")
    for spine in ax.spines.values():
        spine.set_color("#30363d")
    ax.grid(True, color="#21262d", lw=0.5)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"squeeze-map-{as_of}.png"
    plt.tight_layout()
    plt.savefig(out, dpi=130, facecolor="#0d1117")
    plt.close()

    # rank the top squeeze setups for the caller
    ranked = sorted(pts, key=lambda p: p[4], reverse=True)
    print(f"[viz] wrote {out}")
    print("[viz] top squeeze setups:")
    for tk, sf, sr, rv, score in ranked[:8]:
        print(f"  {tk:<6} score={score:.0f}  shortFloat={sf:.1f}%  ratio={sr:.1f}d  relVol={rv:.2f}")
    return out


if __name__ == "__main__":
    squeeze_map(sys.argv[1] if len(sys.argv) > 1 else None)
