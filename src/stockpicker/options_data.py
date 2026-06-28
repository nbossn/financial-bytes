"""
options_data.py — Options, implied volatility, and put/call positioning.

Nick asked the picker to "consider options ... the volatility and greeks data."
Finviz's options page (ty=oc) renders the chain + greeks via JavaScript, so plain
requests can't parse delta/gamma/theta/vega — those need a paid options feed or a
headless browser. What IS reliably available, free and key-less, is the yfinance
option chain, which gives us the parts that actually move a stock-selection model:

    - ATM implied volatility (the market's expected move / event risk)
    - IV term-structure slope (front vs ~30-45d — earnings/event humps)
    - put/call ratio by volume AND by open interest (positioning / sentiment)
    - total open interest (options liquidity)

Greeks are intentionally NOT faked. `greeks_available` is always False here and
the report says so, rather than inventing delta/gamma we can't source for free.

Signals (all oriented so higher = more bullish, for the composite):
    opt_pc_sentiment   = -(put/call vol ratio)   high puts = bearish positioning
    opt_iv_level       = ATM IV (risk/vol input, NOT a direction — used by risk
                         tiering and reported, not added with a bullish sign)

Run: python -m src.stockpicker.options_data MU
"""
from __future__ import annotations

import warnings
from datetime import date, datetime

warnings.filterwarnings("ignore")

import numpy as np
import yfinance as yf

# IV values this small/large are yfinance artifacts on illiquid strikes.
_IV_MIN, _IV_MAX = 0.02, 5.0


def _clean_iv(series) -> "np.ndarray":
    v = series.to_numpy(dtype="float64", na_value=np.nan)
    v = v[(v > _IV_MIN) & (v < _IV_MAX)]
    return v


def _atm_iv(chain_calls, chain_puts, spot: float) -> float | None:
    """Mean IV of the ~3 strikes nearest spot, averaged across calls + puts."""
    ivs = []
    for df in (chain_calls, chain_puts):
        if df is None or df.empty or spot is None:
            continue
        d = df.copy()
        d["_dist"] = (d["strike"] - spot).abs()
        near = d.nsmallest(3, "_dist")
        iv = _clean_iv(near["impliedVolatility"])
        if iv.size:
            ivs.append(float(np.mean(iv)))
    return float(np.mean(ivs)) if ivs else None


def _days_to(exp: str) -> int:
    try:
        return (date.fromisoformat(exp) - date.today()).days
    except Exception:
        return 0


def fetch_options(ticker: str, tk: "yf.Ticker | None" = None) -> dict:
    """Options/IV/positioning snapshot for one ticker. Never raises."""
    out = {
        "ticker": ticker, "ok": False, "greeks_available": False,
        "atm_iv": None, "iv_front": None, "iv_30d": None, "iv_slope": None,
        "put_call_vol": None, "put_call_oi": None, "total_oi": None,
        "n_expirations": 0, "front_expiry": None,
    }
    try:
        t = tk or yf.Ticker(ticker)
        exps = list(t.options or [])
        out["n_expirations"] = len(exps)
        if not exps:
            return out
        out["front_expiry"] = exps[0]

        try:
            spot = t.fast_info.get("lastPrice")
        except Exception:
            spot = None
        if not spot:
            h = t.history(period="1d")
            spot = float(h["Close"].iloc[-1]) if not h.empty else None

        # front expiry: positioning + front IV
        front = t.option_chain(exps[0])
        cv = float(front.calls["volume"].fillna(0).sum())
        pv = float(front.puts["volume"].fillna(0).sum())
        coi = float(front.calls["openInterest"].fillna(0).sum())
        poi = float(front.puts["openInterest"].fillna(0).sum())
        out["put_call_vol"] = round(pv / cv, 3) if cv > 0 else None
        out["put_call_oi"] = round(poi / coi, 3) if coi > 0 else None
        out["total_oi"] = int(coi + poi)
        out["iv_front"] = _atm_iv(front.calls, front.puts, spot)

        # ~30-45d expiry for term-structure slope
        near30 = min(exps, key=lambda e: abs(_days_to(e) - 35))
        if near30 != exps[0]:
            c30 = t.option_chain(near30)
            out["iv_30d"] = _atm_iv(c30.calls, c30.puts, spot)
        else:
            out["iv_30d"] = out["iv_front"]

        out["atm_iv"] = out["iv_front"] or out["iv_30d"]
        if out["iv_front"] is not None and out["iv_30d"] is not None and out["iv_30d"]:
            # positive slope = front IV richer than 30d = near-term event premium
            out["iv_slope"] = round(out["iv_front"] - out["iv_30d"], 4)
        out["ok"] = out["atm_iv"] is not None or out["put_call_vol"] is not None
    except Exception:
        return out
    return out


def signals(opt: dict) -> dict:
    """Composite-ready options signals. Higher = more bullish (except iv_level)."""
    pc = opt.get("put_call_vol")
    return {
        # bearish put positioning lowers the score (negated ratio)
        "opt_pc_sentiment": (-pc if pc is not None else None),
        # IV level: NOT a direction — exposed for risk tiering + the report
        "opt_iv_level": opt.get("atm_iv"),
        # front-loaded IV (event hump) flag
        "opt_event_premium": opt.get("iv_slope"),
    }


def enrich_batch(tickers: list[str], progress_every: int = 15) -> dict[str, dict]:
    """Options enrichment over a list of tickers. Logs names with no chain."""
    out: dict[str, dict] = {}
    no_chain: list[str] = []
    for i, t in enumerate(tickers):
        out[t] = fetch_options(t)
        if not out[t].get("ok"):
            no_chain.append(t)
        if (i + 1) % progress_every == 0:
            print(f"  options {i + 1}/{len(tickers)} ({len(no_chain)} no chain)")
    if no_chain:
        print(f"  [options] {len(no_chain)}/{len(tickers)} have no listed options: {no_chain}")
    return out


if __name__ == "__main__":
    import sys, json
    tk = sys.argv[1] if len(sys.argv) > 1 else "MU"
    o = fetch_options(tk)
    print(f"{tk}: ok={o['ok']} greeks_available={o['greeks_available']}")
    print(f"  ATM IV {o['atm_iv']*100:.1f}%  " if o['atm_iv'] else "  ATM IV n/a  ",
          f"slope {o['iv_slope']}  exps {o['n_expirations']} (front {o['front_expiry']})")
    print(f"  put/call vol {o['put_call_vol']}  put/call OI {o['put_call_oi']}  totalOI {o['total_oi']}")
    print(f"  signals: {json.dumps(signals(o))}")
