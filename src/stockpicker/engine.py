"""
engine.py — Signal computation + IC backtest + composite scoring (the core).

Pipeline
--------
1. Load the universe (tickers).
2. Download ~2y daily OHLCV (yfinance) for the universe + SPY + ^VIX.
3. Compute price-based signals as full time series per ticker.
4. Backtest: at each historical date, cross-sectionally rank each signal and
   correlate (Spearman) with the realized forward h-day return → measured IC.
5. Build the confidence matrix (confidence.py): shrink measured IC toward priors,
   derive weights.
6. Snapshot the CURRENT signal values, normalize cross-sectionally, combine with
   weights → composite score → ranked picks.
7. Persist everything to data/stockpicker/ as JSON for the report stage.

Signals computed here (price-only, computable for the whole universe):
  momentum_12_1, reversal_5d, overnight_drift, vol_signal (overnight vol),
  technical_52w.
Data-dependent signals (earnings_sue, pead_drift, revision_proxy, sentiment,
  insider_cluster) are enriched per-name in the report stage and carry their
  literature prior IC until the accuracy ledger accumulates real history.

Run:  python -m src.stockpicker.engine [universe_csv]
"""
from __future__ import annotations

import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import yfinance as yf

from scipy import stats  # for spearman

# Price-only signals measured in the backtest:
PRICE_SIGNALS = [
    "momentum_12_1",
    "reversal_5d",
    "overnight_drift",
    "vol_signal",
    "technical_52w",
]

# The longest lookback any price signal reaches back for, in trading bars,
# measured from the evaluation bar t. sig_momentum_12_1 reads close.iloc[t-252],
# so t must be >= 252 and the frame therefore needs 253 bars at minimum.
# Anything shorter makes momentum return all-NaN, which cross_sectional_z then
# converts into a clean 0.0 — see all_nan_signals() below.
MAX_SIGNAL_LOOKBACK = 252
MIN_HISTORY_BARS = MAX_SIGNAL_LOOKBACK + 1

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "stockpicker"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def all_nan_signals(sigs: dict[str, pd.Series]) -> list[str]:
    """Names of signals that produced no usable value for any ticker.

    Measures the computed frame rather than predicting from the bar count, so
    it also catches a signal killed by missing data rather than by a short
    window. A signal with even one finite value is live — only a total absence
    is reported, because that is the case cross_sectional_z silently zeroes.
    """
    return sorted(
        name for name, s in sigs.items()
        if not np.isfinite(np.asarray(s, dtype=float)).any()
    )


# ---------------------------------------------------------------------------
# Signal definitions — each takes price frames, returns a per-ticker Series
# aligned to the columns of `close`. All oriented so HIGHER = more bullish.
# ---------------------------------------------------------------------------
def sig_momentum_12_1(close: pd.DataFrame, t: int) -> pd.Series:
    """12-1 month momentum: return from ~252d ago to ~21d ago (skip last month)."""
    if t < 252:
        return pd.Series(np.nan, index=close.columns)
    p_then = close.iloc[t - 252]
    p_recent = close.iloc[t - 21]
    return (p_recent / p_then) - 1.0


def sig_reversal_5d(close: pd.DataFrame, t: int) -> pd.Series:
    """Short-term reversal: NEGATIVE of trailing 5-day return (losers bounce)."""
    if t < 5:
        return pd.Series(np.nan, index=close.columns)
    return -((close.iloc[t] / close.iloc[t - 5]) - 1.0)


def sig_overnight_drift(close: pd.DataFrame, open_: pd.DataFrame, t: int,
                        window: int = 30) -> pd.Series:
    """Trailing mean overnight gap return: mean(open[t]/close[t-1]-1) over window."""
    if t < window + 1:
        return pd.Series(np.nan, index=close.columns)
    gaps = open_.iloc[t - window + 1:t + 1].values / close.iloc[t - window:t].values - 1.0
    return pd.Series(np.nanmean(gaps, axis=0), index=close.columns)


def sig_vol_signal(close: pd.DataFrame, open_: pd.DataFrame, t: int,
                   window: int = 30) -> pd.Series:
    """Overnight-gap volatility, oriented NEGATIVE (lower vol = higher score).

    Serves the user's 'overnight price indicating volatility' requirement. High
    overnight-gap dispersion = event/uncertainty risk; we penalize it in the
    composite (so the raw signal is negated std of overnight gaps).
    """
    if t < window + 1:
        return pd.Series(np.nan, index=close.columns)
    gaps = open_.iloc[t - window + 1:t + 1].values / close.iloc[t - window:t].values - 1.0
    return pd.Series(-np.nanstd(gaps, axis=0), index=close.columns)


def sig_technical_52w(close: pd.DataFrame, t: int) -> pd.Series:
    """52-week-high proximity: price / trailing-252d max (George-Hwang)."""
    if t < 60:
        return pd.Series(np.nan, index=close.columns)
    lo = max(0, t - 252)
    high = close.iloc[lo:t + 1].max()
    return close.iloc[t] / high


def compute_price_signals_at(close, open_, t) -> dict[str, pd.Series]:
    return {
        "momentum_12_1":   sig_momentum_12_1(close, t),
        "reversal_5d":     sig_reversal_5d(close, t),
        "overnight_drift": sig_overnight_drift(close, open_, t),
        "vol_signal":      sig_vol_signal(close, open_, t),
        "technical_52w":   sig_technical_52w(close, t),
    }


# ---------------------------------------------------------------------------
# Normalization — robust cross-sectional z-score (median / MAD).
# ---------------------------------------------------------------------------
def cross_sectional_z(s: pd.Series) -> pd.Series:
    x = s.astype(float).copy()
    med = np.nanmedian(x)
    mad = np.nanmedian(np.abs(x - med))
    scale = 1.4826 * mad if mad > 0 else np.nanstd(x)
    if not scale or np.isnan(scale):
        return pd.Series(0.0, index=s.index)
    z = (x - med) / scale
    return z.clip(-3, 3).fillna(0.0)


# ---------------------------------------------------------------------------
# IC backtest
# ---------------------------------------------------------------------------
def backtest_ic(close, open_, *, horizon=1, stride=2, min_names=20):
    """Measure each price signal's IC over history.

    For each date t (strided), compute signal cross-section and the forward
    h-day return; Spearman-correlate. Returns per-signal dict with ic, n, icir,
    hit_rate plus the per-date IC series for diagnostics.
    """
    n_days = len(close)
    start = 252  # need a year of history for momentum
    end = n_days - horizon - 1
    ic_series: dict[str, list[float]] = {s: [] for s in PRICE_SIGNALS}
    hit_series: dict[str, list[float]] = {s: [] for s in PRICE_SIGNALS}

    for t in range(start, end, stride):
        fwd = (close.iloc[t + horizon] / close.iloc[t]) - 1.0
        sigs = compute_price_signals_at(close, open_, t)
        for name, sval in sigs.items():
            df = pd.concat([sval, fwd], axis=1).dropna()
            df.columns = ["sig", "fwd"]
            if len(df) < min_names or df["sig"].nunique() < 5:
                continue
            ic, _ = stats.spearmanr(df["sig"], df["fwd"])
            if not np.isnan(ic):
                ic_series[name].append(ic)
                # directional hit: sign(sig - median) == sign(fwd)
                med = df["sig"].median()
                pred_up = (df["sig"] > med)
                act_up = (df["fwd"] > 0)
                hit_series[name].append(float((pred_up == act_up).mean()))

    out = {}
    for name in PRICE_SIGNALS:
        arr = np.array(ic_series[name], dtype=float)
        hits = np.array(hit_series[name], dtype=float)
        if len(arr) == 0:
            out[name] = {"ic": float("nan"), "n": 0, "icir": float("nan"),
                         "hit_rate": float("nan")}
            continue
        ic_mean = float(np.nanmean(arr))
        ic_std = float(np.nanstd(arr))
        icir = ic_mean / ic_std if ic_std > 0 else float("nan")
        out[name] = {
            "ic": ic_mean,
            "n": int(len(arr)),
            "icir": icir,
            "hit_rate": float(np.nanmean(hits)) if len(hits) else float("nan"),
        }
    return out


# ---------------------------------------------------------------------------
# Universe loading
# ---------------------------------------------------------------------------
def load_universe(csv_path: str | None = None) -> list[str]:
    """Load tickers from the Fidelity positions CSV (both accounts) by default."""
    if csv_path is None:
        csv_path = "/mnt/c/Users/nicky/Downloads/Portfolio_Positions_Jun-24-2026.csv"
    accounts = {"Z38250874", "X86341126", "Z32785271"}
    skip_substr = ["**", "Pending"]
    funds = {"FMPXX", "FDRXX", "FCNTX", "SPAXX", "VOO", "VTIAX", "VUG", "VOOG",
             "FDRXX", "VTSAX"}
    tickers: set[str] = set()
    import csv as _csv
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in _csv.DictReader(f):
            acc = (row.get("Account Number") or "").strip()
            sym = (row.get("Symbol") or "").strip()
            if acc not in accounts or not sym:
                continue
            if any(x in sym for x in skip_substr) or sym in funds:
                continue
            tickers.add(sym)
    return sorted(tickers)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(universe_csv: str | None = None):
    from src.stockpicker.confidence import build_confidence_matrix

    tickers = load_universe(universe_csv)
    print(f"[engine] universe: {len(tickers)} tickers")

    dl = yf.download(tickers + ["SPY", "^VIX"], period="2y",
                     auto_adjust=True, progress=False)
    close = dl["Close"].copy()
    open_ = dl["Open"].copy()

    # Drop tickers with too little data
    good = [c for c in tickers if c in close.columns
            and close[c].notna().sum() > 260]
    close_u = close[good]
    open_u = open_[good]
    print(f"[engine] usable tickers: {len(good)}  dates: {len(close_u)}")
    print(f"[engine] date range: {close_u.index[0].date()} -> {close_u.index[-1].date()}")

    # --- IC backtest (price signals) ---
    print("[engine] backtesting signal ICs (horizon=1) ...")
    measured = backtest_ic(close_u, open_u, horizon=1, stride=2)
    print("[engine] backtesting signal ICs (horizon=5) ...")
    measured_h5 = backtest_ic(close_u, open_u, horizon=5, stride=3)

    # --- Signal correlation matrix (current cross-section) for de-dup penalty ---
    t_now = len(close_u) - 1
    cur = compute_price_signals_at(close_u, open_u, t_now)
    z_now = {k: cross_sectional_z(v) for k, v in cur.items()}
    zdf = pd.DataFrame({k: z_now[k] for k in PRICE_SIGNALS}).dropna()
    corr = zdf.corr().values if len(zdf) > 5 else None

    # --- Confidence matrix ---
    cm = build_confidence_matrix(
        measured, horizon_days=1, correlation=corr,
        signal_order=PRICE_SIGNALS, use_icir=True,
    )
    print("\n[engine] CONFIDENCE MATRIX (h=1)\n" + cm.as_table())

    # --- Current composite using ALL signals (price live; others = 0 now) ---
    # Price signals get measured weights; data-dependent signals carry weight but
    # contribute 0 at the universe stage (enriched per-name later).
    weights = cm.weights
    composite = pd.Series(0.0, index=good)
    contrib = {}
    for name in PRICE_SIGNALS:
        z = cross_sectional_z(cur[name]).reindex(good).fillna(0.0)
        w = weights.get(name, 0.0)
        composite = composite + w * z
        contrib[name] = (w * z)
    composite = composite.sort_values(ascending=False)

    # --- current prices + simple stats for the report stage ---
    last_close = close_u.iloc[-1]
    prev_close = close_u.iloc[-2]
    day_chg = (last_close / prev_close - 1.0)

    # overnight gap vol (annualized-ish, raw daily std of overnight gaps, 30d)
    gaps = open_u.values[-30:] / close_u.values[-31:-1] - 1.0
    on_vol = pd.Series(np.nanstd(gaps, axis=0), index=good)

    # 30d realized daily vol
    rets = close_u.pct_change().iloc[-30:]
    rvol = rets.std()

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(good),
        "date_start": str(close_u.index[0].date()),
        "date_end": str(close_u.index[-1].date()),
        "vix_last": float(close["^VIX"].iloc[-1]) if "^VIX" in close.columns else None,
        "spy_last": float(close["SPY"].iloc[-1]) if "SPY" in close.columns else None,
        "confidence_matrix_h1": [vars(r) for r in cm.rows],
        "confidence_matrix_h5": measured_h5,
        "measured_ic_h1": measured,
        "weights": weights,
        "signal_correlation": (corr.tolist() if corr is not None else None),
        "signal_order": PRICE_SIGNALS,
        "ranked": [],
    }

    for tk in composite.index:
        snapshot["ranked"].append({
            "ticker": tk,
            "composite": float(composite[tk]),
            "last_close": float(last_close[tk]),
            "day_change_pct": float(day_chg[tk] * 100),
            "overnight_gap_vol_pct": float(on_vol[tk] * 100),
            "realized_vol_30d_pct": float(rvol[tk] * 100),
            "contributions": {n: float(contrib[n].reindex([tk]).iloc[0])
                              for n in PRICE_SIGNALS},
            "raw_signals": {n: (float(cur[n].reindex([tk]).iloc[0])
                                if not np.isnan(cur[n].reindex([tk]).iloc[0]) else None)
                            for n in PRICE_SIGNALS},
        })

    out_path = DATA_DIR / "universe_scores.json"
    with open(out_path, "w") as f:
        json.dump(snapshot, f, indent=2)
    print(f"\n[engine] wrote {out_path}")
    print(f"[engine] VIX={snapshot['vix_last']:.1f}  SPY={snapshot['spy_last']:.2f}")
    print("\n[engine] TOP 25 by composite:")
    for r in snapshot["ranked"][:25]:
        print(f"  {r['ticker']:<6} comp={r['composite']:+.3f}  "
              f"close=${r['last_close']:.2f}  onVol={r['overnight_gap_vol_pct']:.2f}%")
    return snapshot


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
