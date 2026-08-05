"""
yfinance price fallback — used when massive.com snapshot endpoint is unavailable (plan limitation).

Provides EOD/delayed prices (15-min delay during market hours) via Yahoo Finance.
Free, no API key required. Used as a fallback in MassiveEndpoints.get_quote().
"""
from datetime import datetime, timezone
from decimal import Decimal

from loguru import logger

from src.api.models import QuoteSnapshot
from src.api.symbols import resolve_symbol, to_yahoo_symbol


def get_quote_yfinance(ticker: str) -> QuoteSnapshot | None:
    """
    Fetch current price data via yfinance (Yahoo Finance).

    Returns a QuoteSnapshot compatible with the massive.com version.
    Data is 15-min delayed during market hours; EOD after close.
    """
    try:
        import yfinance as yf

        symbol, rewritten = resolve_symbol(ticker)
        if rewritten:
            logger.debug(f"[yfinance] {ticker} → {symbol} (broker dual-class form)")

        t = yf.Ticker(symbol)
        info = t.fast_info

        price = info.last_price
        prev_close = info.previous_close

        if not price:
            logger.warning(f"[yfinance] No price data for {ticker}")
            return None

        price_d = Decimal(str(round(price, 4)))
        prev_d = Decimal(str(round(prev_close, 4))) if prev_close else None
        change = (price_d - prev_d) if prev_d else None
        change_pct = (change / prev_d * 100) if (change is not None and prev_d) else None

        logger.info(
            f"[yfinance] {ticker}: ${price_d} "
            f"({'%+.2f' % float(change_pct)}% vs prev close)" if change_pct else f"[yfinance] {ticker}: ${price_d}"
        )

        return QuoteSnapshot(
            ticker=ticker,
            current_price=price_d,
            prev_close=prev_d,
            day_change=change,
            day_change_pct=change_pct,
            volume=int(info.three_month_average_volume) if info.three_month_average_volume else None,
            market_cap=Decimal(str(int(info.market_cap))) if info.market_cap else None,
            as_of=datetime.now(timezone.utc),
        )

    except Exception as e:
        logger.warning(f"[yfinance] Failed to get quote for {ticker}: {e}")
        return None


def get_quotes_batch_yfinance(tickers: list[str]) -> dict[str, QuoteSnapshot]:
    """
    Fetch prices for multiple tickers in one yfinance call (more efficient).
    Returns dict of ticker -> QuoteSnapshot.
    """
    try:
        import yfinance as yf

        # Download by Yahoo symbol, but key every result by the caller's
        # ticker — downstream consumers look prices up by `holding.ticker`.
        symbol_of = {t: to_yahoo_symbol(t) for t in tickers}

        data = yf.download(
            tickers=" ".join(symbol_of[t] for t in tickers),
            period="2d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )

        results = {}
        if data.empty:
            return results

        close = data["Close"]
        if not hasattr(close, "columns"):
            # Older yfinance returned a Series for a single ticker. Current
            # versions return a one-column DataFrame already, and the
            # unconditional `.to_frame()` this replaces raised AttributeError
            # for *every* single-ticker call — MSFT included — which the
            # blanket `except` turned into an empty dict. Branch on the shape
            # rather than on len(tickers).
            close = close.to_frame(name=symbol_of[tickers[0]])

        for ticker in tickers:
            symbol = symbol_of[ticker]
            if symbol not in close.columns:
                continue
            prices = close[symbol].dropna()
            if len(prices) < 1:
                continue

            current = Decimal(str(round(float(prices.iloc[-1]), 4)))
            prev = Decimal(str(round(float(prices.iloc[-2]), 4))) if len(prices) >= 2 else None
            change = (current - prev) if prev else None
            change_pct = (change / prev * 100) if (change is not None and prev) else None

            results[ticker] = QuoteSnapshot(
                ticker=ticker,
                current_price=current,
                prev_close=prev,
                day_change=change,
                day_change_pct=change_pct,
                as_of=datetime.now(timezone.utc),
            )

        logger.info(f"[yfinance] Batch fetched {len(results)}/{len(tickers)} tickers")
        return results

    except Exception as e:
        logger.warning(f"[yfinance] Batch fetch failed: {e}")
        return {}
