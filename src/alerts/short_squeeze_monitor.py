"""Short squeeze setup detector — financial-bytes.

Monitors a ticker for squeeze setup signals and scores the probability
that a short squeeze is forming. Primary use case: INTU (Intuit Inc.)
held in the Lilich Trust at -35.1%, maintained on a squeeze thesis.

Data sources (all free):
  - yfinance: price history, RSI, options chain, info dict
  - FINRA REGSHO: bi-weekly short interest (free download)
    Falls back to yfinance `info` keys when FINRA data is unavailable
    or stale (>15 calendar days old).

Squeeze scoring (0-10):
  +2  Days-to-cover > 5
  +2  Short float > 20%
  +1  RSI < 35 (oversold)
  +2  RSI uptick from oversold (most recent RSI above prior low, now rising)
  +1  Call/put ratio > 1.5
  +2  Price holding above 52-week low despite high short interest

Score bands:
  7-10  HIGH — flag in newsletter, send Discord alert
  4-6   MODERATE — monitor weekly
  0-3   LOW — background monitoring only

Usage:
    from src.alerts.short_squeeze_monitor import run_squeeze_check, squeeze_alert

    result = run_squeeze_check("INTU")
    print(result.summary_line())

    if result.score >= 7:
        squeeze_alert(result)          # sends Discord notification
"""
from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import requests
import yfinance as yf
from loguru import logger

warnings.filterwarnings("ignore", category=FutureWarning)


# ── Constants ─────────────────────────────────────────────────────────────────

# FINRA REGSHO consolidated short interest download.
# Updated twice per month (settlement dates); file list at:
# https://www.finra.org/investors/learn-to-invest/advanced-investing/short-selling
FINRA_REGSHO_BASE_URL = "https://cdn.finra.org/equity/regsho/monthly"

# Days-to-cover threshold for +2 signal points
DTC_HIGH_THRESHOLD: float = 5.0

# Short float threshold (as fraction, e.g. 0.20 = 20%) for +2 signal points
SHORT_FLOAT_HIGH_THRESHOLD: float = 0.20

# RSI thresholds
RSI_OVERSOLD: float = 35.0
RSI_LOOKBACK_DAYS: int = 14

# Call/put ratio threshold for +1 signal point
CALL_PUT_HIGH_THRESHOLD: float = 1.5

# Proximity to 52W low: price within this % of the 52W low counts as "near low"
NEAR_52W_LOW_PCT: float = 0.15  # within 15% of the 52-week low

# FINRA data is considered stale after this many calendar days
FINRA_STALE_DAYS: int = 15

# How many RSI periods to look back to detect an uptick from oversold
RSI_UPTICK_LOOKBACK: int = 5


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class SqueezeResult:
    """Per-ticker squeeze setup evaluation."""

    ticker: str
    check_date: date

    # Raw signals
    short_float: float | None          # short interest as fraction of float (e.g. 0.22)
    days_to_cover: float | None        # short interest / avg daily volume
    rsi_14: float | None               # current 14-day RSI
    rsi_turning: bool                  # True if RSI uptick detected from oversold
    call_put_ratio: float | None       # (call OI) / (put OI) across nearest expiry
    current_price: float | None
    week_52_low: float | None
    week_52_high: float | None

    # Source metadata
    short_data_source: str             # "finra" or "yfinance_info"
    short_data_as_of: date | None      # settlement date of FINRA data (None if yfinance)
    finra_fetch_ok: bool               # True if FINRA download succeeded

    # Score breakdown
    score: int                         # 0-10
    score_breakdown: dict[str, int] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)

    @property
    def score_label(self) -> str:
        if self.score >= 7:
            return "HIGH SQUEEZE POTENTIAL"
        if self.score >= 4:
            return "MODERATE — monitor weekly"
        return "LOW — background monitoring"

    @property
    def near_52w_low(self) -> bool:
        if self.current_price is None or self.week_52_low is None:
            return False
        return self.current_price <= self.week_52_low * (1 + NEAR_52W_LOW_PCT)

    def summary_line(self) -> str:
        """One-line console summary."""
        short_pct = f"{self.short_float * 100:.1f}%" if self.short_float is not None else "N/A"
        dtc = f"{self.days_to_cover:.1f}" if self.days_to_cover is not None else "N/A"
        rsi = f"{self.rsi_14:.1f}" if self.rsi_14 is not None else "N/A"
        return (
            f"{self.ticker} Short Squeeze Monitor — {self.check_date}\n"
            f"Score: {self.score}/10 ({self.score_label})\n"
            f"Short Float: {short_pct} | Days-to-Cover: {dtc} | RSI: {rsi}\n"
            f"Signals: {'; '.join(self.signals) if self.signals else 'No active signals'}"
        )

    def newsletter_section(self) -> str | None:
        """Return a Markdown section for the newsletter when score >= 7, else None."""
        if self.score < 7:
            return None
        short_pct = f"{self.short_float * 100:.1f}%" if self.short_float is not None else "N/A"
        dtc = f"{self.days_to_cover:.1f}" if self.days_to_cover is not None else "N/A"
        rsi = f"{self.rsi_14:.1f}" if self.rsi_14 is not None else "N/A"
        source_note = (
            f"FINRA REGSHO ({self.short_data_as_of})"
            if self.short_data_source == "finra"
            else "yfinance info dict"
        )
        return (
            f"\n### {self.ticker} — SHORT SQUEEZE ALERT (Score {self.score}/10)\n\n"
            f"| Signal | Value |\n"
            f"|--------|-------|\n"
            f"| Short Float | {short_pct} |\n"
            f"| Days-to-Cover | {dtc} |\n"
            f"| RSI (14d) | {rsi} |\n"
            f"| Score | **{self.score}/10** |\n"
            f"| Short data source | {source_note} |\n\n"
            f"**Active signals:** {'; '.join(self.signals)}\n\n"
            f"**Hold position — squeeze setup is forming.** "
            f"A short squeeze occurs when heavily shorted stocks reverse sharply, forcing "
            f"short sellers to cover at increasingly higher prices. Review and confirm thesis.\n"
        )


# ── FINRA REGSHO fetcher ──────────────────────────────────────────────────────

def _fetch_finra_short_interest(ticker: str) -> tuple[float | None, float | None, date | None]:
    """Fetch short interest data from FINRA REGSHO consolidated files.

    FINRA publishes bi-monthly consolidated short interest files at:
      https://cdn.finra.org/equity/regsho/monthly/

    File naming convention: FNRAshvol{YYYYMMDD}.txt (tab-delimited)
    Columns: Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market

    Returns (short_volume_fraction, None, settlement_date) or (None, None, None).
    Note: FINRA REGSHO measures daily short *volume* fraction (shorts / total volume)
    which approximates short interest direction but is NOT the same as short float.
    We use it as a directional signal; float % comes from yfinance info.

    If the download fails or the ticker is not found, returns (None, None, None)
    so the caller can fall back to yfinance info.
    """
    # Try the most recent two months of data
    today = date.today()
    candidates: list[date] = []
    for delta_months in range(3):
        d = today - timedelta(days=delta_months * 30)
        # FINRA files are named by year-month; try first of each month
        candidates.append(d.replace(day=1))

    for ref_date in candidates:
        filename = f"FNRAshvol{ref_date.strftime('%Y%m')}.txt"
        url = f"{FINRA_REGSHO_BASE_URL}/{filename}"
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code != 200:
                logger.debug(f"finra: {url} returned {resp.status_code}")
                continue

            lines = resp.text.strip().splitlines()
            if len(lines) < 2:
                continue

            # Find the most recent row for this ticker
            header = lines[0].split("|")
            try:
                sym_idx    = header.index("Symbol")
                short_idx  = header.index("ShortVolume")
                total_idx  = header.index("TotalVolume")
                date_idx   = header.index("Date")
            except ValueError as e:
                logger.debug(f"finra: unexpected header format ({e})")
                continue

            ticker_rows = []
            for line in lines[1:]:
                parts = line.split("|")
                if len(parts) <= max(sym_idx, short_idx, total_idx, date_idx):
                    continue
                if parts[sym_idx].strip().upper() == ticker.upper():
                    ticker_rows.append(parts)

            if not ticker_rows:
                logger.debug(f"finra: {ticker} not found in {filename}")
                continue

            # Use the most recent row
            last = ticker_rows[-1]
            short_vol = float(last[short_idx].strip())
            total_vol = float(last[total_idx].strip())
            raw_date_str = last[date_idx].strip()

            if total_vol <= 0:
                continue

            short_vol_fraction = short_vol / total_vol
            settlement_date = datetime.strptime(raw_date_str, "%Y%m%d").date()

            # Reject stale data
            if (today - settlement_date).days > FINRA_STALE_DAYS:
                logger.debug(
                    f"finra: {ticker} data is {(today - settlement_date).days}d old — stale"
                )
                return None, None, None

            logger.info(
                f"finra: {ticker} short volume fraction {short_vol_fraction:.2%} "
                f"as of {settlement_date} (from {filename})"
            )
            return short_vol_fraction, None, settlement_date

        except requests.RequestException as e:
            logger.debug(f"finra: network error fetching {url}: {e}")
            continue
        except (ValueError, IndexError) as e:
            logger.debug(f"finra: parse error in {filename}: {e}")
            continue

    return None, None, None


# ── Technical signal helpers ──────────────────────────────────────────────────

def _compute_rsi(closes, period: int = 14) -> float | None:
    """Compute RSI(period) from a pandas Series of closing prices."""
    try:
        import pandas as pd

        if len(closes) < period + 1:
            return None

        delta = closes.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)

        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()

        # Last row
        ag = float(avg_gain.iloc[-1])
        al = float(avg_loss.iloc[-1])

        if al == 0:
            return 100.0

        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))
    except Exception as e:
        logger.debug(f"RSI computation failed: {e}")
        return None


def _detect_rsi_uptick(closes, period: int = 14, lookback: int = RSI_UPTICK_LOOKBACK) -> bool:
    """Return True if RSI has turned upward from an oversold reading.

    Detection: within the last `lookback` bars, RSI was < RSI_OVERSOLD,
    and the most recent RSI reading is higher than the RSI `lookback` bars ago.
    This suggests momentum is beginning to reverse.
    """
    try:
        if len(closes) < period + lookback + 2:
            return False

        # Compute RSI for the last (lookback + 1) candles
        rsi_values = []
        for i in range(lookback + 1):
            subset = closes.iloc[: len(closes) - i]
            r = _compute_rsi(subset, period)
            rsi_values.insert(0, r)  # oldest first

        if any(v is None for v in rsi_values):
            return False

        current_rsi = rsi_values[-1]
        prior_rsi_values = rsi_values[:-1]

        # Was it in oversold territory recently?
        was_oversold = any(v < RSI_OVERSOLD for v in prior_rsi_values)  # type: ignore[operator]
        # Is it now rising?
        is_rising = current_rsi > rsi_values[0]  # type: ignore[operator]

        return was_oversold and is_rising
    except Exception as e:
        logger.debug(f"RSI uptick detection failed: {e}")
        return False


def _compute_call_put_ratio(ticker_obj: yf.Ticker) -> float | None:
    """Compute call/put OI ratio from the nearest options expiry.

    Uses the first available expiry (usually 1-4 weeks out). Returns None
    if no options data is available.
    """
    try:
        expirations = ticker_obj.options
        if not expirations:
            return None

        # Use nearest expiry
        chain = ticker_obj.option_chain(expirations[0])
        call_oi = chain.calls["openInterest"].sum()
        put_oi  = chain.puts["openInterest"].sum()

        if put_oi <= 0:
            return None

        ratio = float(call_oi) / float(put_oi)
        logger.debug(f"options: call OI={call_oi:,.0f}, put OI={put_oi:,.0f}, ratio={ratio:.2f}")
        return ratio

    except Exception as e:
        logger.debug(f"options chain fetch failed: {e}")
        return None


# ── Core check ────────────────────────────────────────────────────────────────

def run_squeeze_check(ticker: str) -> SqueezeResult:
    """Run a full short squeeze setup check for `ticker`.

    Fetches all signals, scores them 0-10, and returns a SqueezeResult with
    the complete breakdown. Never raises — failures degrade gracefully to None
    values in the result with score contribution of 0.

    Args:
        ticker: Stock symbol (e.g. "INTU").

    Returns:
        SqueezeResult with score, signals, and raw signal values.
    """
    ticker = ticker.strip().upper()
    today = date.today()

    logger.info(f"squeeze_check: starting check for {ticker}")

    # ── 1. Price history (60d for RSI + 52W range) ────────────────────────────
    current_price: float | None = None
    week_52_low:   float | None = None
    week_52_high:  float | None = None
    rsi_14:        float | None = None
    rsi_turning:   bool = False
    avg_daily_vol: float | None = None

    try:
        tk = yf.Ticker(ticker)
        hist_1y = tk.history(period="1y", auto_adjust=True)

        if not hist_1y.empty:
            closes = hist_1y["Close"]
            volumes = hist_1y["Volume"]

            current_price = float(closes.iloc[-1])
            week_52_low   = float(closes.min())
            week_52_high  = float(closes.max())

            # Use last 20 trading days avg volume (more stable than full year)
            if len(volumes) >= 20:
                avg_daily_vol = float(volumes.iloc[-20:].mean())
            else:
                avg_daily_vol = float(volumes.mean())

            rsi_14    = _compute_rsi(closes, period=RSI_LOOKBACK_DAYS)
            rsi_turning = _detect_rsi_uptick(closes, period=RSI_LOOKBACK_DAYS)
        else:
            logger.warning(f"squeeze_check: no price history for {ticker}")

    except Exception as e:
        logger.warning(f"squeeze_check: price/history fetch failed for {ticker}: {e}")
        tk = yf.Ticker(ticker)  # still need the object for options

    # ── 2. Short interest — FINRA REGSHO first, yfinance fallback ─────────────
    short_float:      float | None = None
    days_to_cover:    float | None = None
    short_data_source = "yfinance_info"
    short_data_as_of: date | None = None
    finra_fetch_ok    = False

    finra_short_vol_frac, _, finra_date = _fetch_finra_short_interest(ticker)

    if finra_short_vol_frac is not None and finra_date is not None:
        finra_fetch_ok = True
        short_data_source = "finra"
        short_data_as_of = finra_date
        # FINRA short volume fraction is a daily signal; use it for DTC direction.
        # For actual short float %, we still use yfinance info (FINRA doesn't report float %).
        logger.info(
            f"squeeze_check: FINRA short vol fraction for {ticker}: "
            f"{finra_short_vol_frac:.2%} as of {finra_date}"
        )

    # Always pull yfinance info for short float % (not available in FINRA REGSHO)
    try:
        info = tk.info or {}
        yf_short_float = info.get("shortPercentOfFloat")
        yf_short_ratio = info.get("shortRatio")  # days-to-cover

        if yf_short_float is not None:
            short_float = float(yf_short_float)
            if not finra_fetch_ok:
                short_data_source = "yfinance_info"

        if yf_short_ratio is not None and avg_daily_vol is not None:
            days_to_cover = float(yf_short_ratio)
        elif yf_short_ratio is not None:
            days_to_cover = float(yf_short_ratio)

    except Exception as e:
        logger.debug(f"squeeze_check: yfinance info fetch failed: {e}")

    # ── 3. Options call/put ratio ──────────────────────────────────────────────
    call_put_ratio: float | None = None
    try:
        call_put_ratio = _compute_call_put_ratio(tk)
    except Exception as e:
        logger.debug(f"squeeze_check: options fetch failed: {e}")

    # ── 4. Scoring ────────────────────────────────────────────────────────────
    score = 0
    breakdown: dict[str, int] = {}
    signals: list[str] = []

    # Days-to-cover > 5: +2
    if days_to_cover is not None and days_to_cover > DTC_HIGH_THRESHOLD:
        pts = 2
        score += pts
        breakdown["days_to_cover"] = pts
        signals.append(f"Days-to-cover {days_to_cover:.1f} (>{DTC_HIGH_THRESHOLD:.0f})")
    else:
        breakdown["days_to_cover"] = 0

    # Short float > 20%: +2
    if short_float is not None and short_float > SHORT_FLOAT_HIGH_THRESHOLD:
        pts = 2
        score += pts
        breakdown["short_float"] = pts
        signals.append(f"Short float {short_float * 100:.1f}% (>{SHORT_FLOAT_HIGH_THRESHOLD * 100:.0f}%)")
    else:
        breakdown["short_float"] = 0

    # RSI < 35 (oversold): +1
    if rsi_14 is not None and rsi_14 < RSI_OVERSOLD:
        pts = 1
        score += pts
        breakdown["rsi_oversold"] = pts
        signals.append(f"RSI {rsi_14:.1f} (oversold <{RSI_OVERSOLD:.0f})")
    else:
        breakdown["rsi_oversold"] = 0

    # RSI uptick from oversold: +2
    if rsi_turning:
        pts = 2
        score += pts
        breakdown["rsi_uptick"] = pts
        signals.append("RSI turning up from oversold (momentum reversal signal)")
    else:
        breakdown["rsi_uptick"] = 0

    # Call/put ratio > 1.5: +1
    if call_put_ratio is not None and call_put_ratio > CALL_PUT_HIGH_THRESHOLD:
        pts = 1
        score += pts
        breakdown["call_put_ratio"] = pts
        signals.append(f"Call/put ratio {call_put_ratio:.2f} (>{CALL_PUT_HIGH_THRESHOLD:.1f})")
    else:
        breakdown["call_put_ratio"] = 0

    # Price holding above 52W low despite high short interest: +2
    # Condition: price > 52W low (not at new lows) AND short float is elevated
    if (
        current_price is not None
        and week_52_low is not None
        and short_float is not None
        and short_float > SHORT_FLOAT_HIGH_THRESHOLD
        and not (current_price <= week_52_low * (1 + NEAR_52W_LOW_PCT))
    ):
        pts = 2
        score += pts
        breakdown["price_above_52w_low"] = pts
        low_pct = (current_price - week_52_low) / week_52_low * 100
        signals.append(
            f"Price ${current_price:.2f} holding {low_pct:.1f}% above 52W low "
            f"despite {short_float * 100:.1f}% short float"
        )
    else:
        breakdown["price_above_52w_low"] = 0

    score = min(score, 10)  # cap at 10

    logger.info(
        f"squeeze_check: {ticker} score={score}/10 ({_score_label(score)}) — "
        f"short_float={short_float}, dtc={days_to_cover}, rsi={rsi_14}"
    )

    return SqueezeResult(
        ticker=ticker,
        check_date=today,
        short_float=short_float,
        days_to_cover=days_to_cover,
        rsi_14=rsi_14,
        rsi_turning=rsi_turning,
        call_put_ratio=call_put_ratio,
        current_price=current_price,
        week_52_low=week_52_low,
        week_52_high=week_52_high,
        short_data_source=short_data_source,
        short_data_as_of=short_data_as_of,
        finra_fetch_ok=finra_fetch_ok,
        score=score,
        score_breakdown=breakdown,
        signals=signals,
    )


def _score_label(score: int) -> str:
    if score >= 7:
        return "HIGH"
    if score >= 4:
        return "MODERATE"
    return "LOW"


# ── Discord notification ──────────────────────────────────────────────────────

def squeeze_alert(result: SqueezeResult) -> bool:
    """Send a Discord webhook notification when score >= 7.

    Follows the same webhook pattern as stop_loss.py: reads
    DISCORD_WEBHOOK_URL from env, posts as a JSON content message.

    Args:
        result: SqueezeResult from run_squeeze_check().

    Returns:
        True if the alert was sent successfully, False otherwise.
    """
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("squeeze_alert: DISCORD_WEBHOOK_URL not set — skipping Discord alert")
        return False

    if result.score < 7:
        logger.info(
            f"squeeze_alert: {result.ticker} score {result.score}/10 < 7 — alert suppressed"
        )
        return False

    short_pct = (
        f"{result.short_float * 100:.1f}%"
        if result.short_float is not None
        else "N/A"
    )
    dtc = f"{result.days_to_cover:.1f}" if result.days_to_cover is not None else "N/A"
    rsi = f"{result.rsi_14:.1f}" if result.rsi_14 is not None else "N/A"
    cpr = f"{result.call_put_ratio:.2f}" if result.call_put_ratio is not None else "N/A"

    score_bar = "█" * result.score + "░" * (10 - result.score)

    lines = [
        f"**SHORT SQUEEZE ALERT — {result.ticker}** ({result.check_date})",
        "",
        f"Score: **{result.score}/10** `{score_bar}` — {result.score_label}",
        "",
        f"Short Float: `{short_pct}` | Days-to-Cover: `{dtc}` | RSI: `{rsi}` | C/P Ratio: `{cpr}`",
        "",
    ]

    if result.signals:
        lines.append("**Active signals:**")
        for s in result.signals:
            lines.append(f"  - {s}")
        lines.append("")

    if result.short_data_source == "finra" and result.short_data_as_of:
        lines.append(f"Short data: FINRA REGSHO ({result.short_data_as_of})")
    else:
        lines.append("Short data: yfinance info dict (FINRA unavailable)")

    lines.append("")
    lines.append("_Hold position — squeeze setup forming. No auto-action taken. Review thesis._")

    content = "\n".join(lines)

    try:
        resp = requests.post(webhook_url, json={"content": content}, timeout=10)
        resp.raise_for_status()
        logger.info(f"squeeze_alert: Discord alert sent for {result.ticker} (score {result.score}/10)")
        return True
    except Exception as e:
        logger.error(f"squeeze_alert: Discord alert failed: {e}")
        return False


# ── Newsletter integration helper ─────────────────────────────────────────────

def get_newsletter_squeeze_section(ticker: str = "INTU") -> str | None:
    """Run a squeeze check and return a newsletter Markdown section if score >= 7.

    Intended to be called from the newsletter generation pipeline. Returns None
    when the score is below threshold (no section injected).

    Args:
        ticker: Ticker to evaluate (default "INTU").

    Returns:
        Markdown string if score >= 7, else None.
    """
    try:
        result = run_squeeze_check(ticker)
        return result.newsletter_section()
    except Exception as e:
        logger.warning(f"get_newsletter_squeeze_section: failed for {ticker}: {e}")
        return None
