"""Main pipeline — runs the full financial-bytes workflow end-to-end."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

from loguru import logger

from src.config import settings
from src.portfolio.reader import read_portfolio, save_portfolio_to_db
from src.portfolio.models import PortfolioSnapshot


def _load_purchase_history(portfolio_name: str) -> dict[str, list[dict]]:
    """Load per-lot purchase history from the portfolio def, if configured.

    Returns a dict keyed by ticker → list of lot dicts with keys:
      shares (Decimal | None), cost_basis (Decimal), purchase_date (str ISO).
    Empty dict if no purchase_history is configured.
    """
    import json
    from src.portfolio.portfolio_config import load_portfolio_defs

    try:
        defs = load_portfolio_defs()
    except Exception:
        return {}

    pdef = next((d for d in defs if d.name == portfolio_name), None)
    if pdef is None or not pdef.purchase_history:
        return {}

    history_path = Path(pdef.purchase_history)
    if not history_path.exists():
        logger.warning(f"Purchase history file not found: {history_path}")
        return {}

    with history_path.open(encoding="utf-8") as f:
        raw = json.load(f)

    # Strip comment keys and empty lot lists
    return {k: v for k, v in raw.items() if not k.startswith("_") and v}


def _apply_purchase_history_to_holdings(
    holdings: list, lot_overrides: dict[str, list[dict]]
) -> None:
    """Mutate holdings in-place: set purchase_date to the earliest lot date per ticker.

    This gives the analyst agent accurate holding period context.
    """
    from datetime import date as _date

    for holding in holdings:
        lots = lot_overrides.get(holding.ticker)
        if not lots:
            continue
        dates = []
        for lot in lots:
            ds = lot.get("purchase_date")
            if ds:
                try:
                    dates.append(_date.fromisoformat(ds))
                except ValueError:
                    pass
        if dates:
            holding.purchase_date = min(dates)


def _fetch_prices_only(tickers: list[str]) -> dict:
    """Batch-fetch market prices with no signals or analyst work attached."""
    from decimal import Decimal

    from src.api.yfinance_client import get_quotes_batch_yfinance

    out: dict[str, Decimal] = {}
    for ticker, quote in (get_quotes_batch_yfinance(tickers) or {}).items():
        # QuoteSnapshot.current_price — guessing this attribute name is how the
        # first cut silently priced 0/192 holdings while logging success.
        price = getattr(quote, "current_price", None)
        if price is not None:
            out[ticker] = Decimal(str(price))
    return out


def _price_excluded_holdings(excluded: list, prices: dict) -> None:
    """Price positions that sit outside analyst coverage, in place.

    max_positions bounds how many tickers get an (expensive) analyst call. It must
    not bound *valuation*: an unpriced holding falls back to cost basis, which
    reports unrealized P&L of exactly $0.00 and is indistinguishable from a flat
    position. On 2026-08-18 that silently understated the account by $112,509
    across 191 positions.

    Prices are cheap, so fetch them for everything and analyse the top N.
    A price-source failure degrades to the old behaviour rather than aborting.
    """
    if not excluded:
        return

    missing = sorted({h.ticker for h in excluded if h.ticker not in prices})
    if not missing:
        return

    logger.info(f"[price] Fetching market prices for {len(missing)} unanalysed holding(s)…")
    try:
        fetched = _fetch_prices_only(missing)
    except Exception as e:
        logger.warning(f"[price] Could not price unanalysed holdings ({e}) — they fall back to cost basis")
        return

    for ticker, price in fetched.items():
        prices.setdefault(ticker, price)

    still_missing = [t for t in missing if t not in prices]
    logger.info(
        f"[price] Priced {len(fetched)}/{len(missing)} unanalysed holding(s)"
        + (f"; still unpriced: {still_missing}" if still_missing else "")
    )


def _split_by_max_positions(holdings: list, pdef) -> tuple[list, list]:
    """Split holdings into (analyzed, excluded) by allowlist or top-N cap.

    Priority:
      1. allowed_tickers — if set, keep ONLY those tickers (exact match, case-insensitive).
         This is the authoritative scope for portfolios like Lilich where the CSV may
         contain the full account but only a subset of tickers are under management.
      2. max_positions — if set and no allowlist, keep top-N by total cost-basis value.

    Both lists are returned. The excluded ones are still owned, so the snapshot
    must count them in its totals even though they get no analyst call — see
    PortfolioSnapshot.excluded_holdings.
    """
    # 1. Allowlist takes precedence
    if pdef.allowed_tickers:
        allowed = {t.upper() for t in pdef.allowed_tickers}
        kept = [h for h in holdings if h.ticker.upper() in allowed]
        excluded = [h for h in holdings if h.ticker.upper() not in allowed]
        logger.info(
            f"  allowed_tickers filter: keeping {len(kept)} of {len(holdings)} holdings "
            f"for {pdef.name} — excluded {len(excluded)}: {[h.ticker for h in excluded][:10]}"
        )
        return kept, excluded

    # 2. Top-N cap fallback
    if pdef.max_positions and len(holdings) > pdef.max_positions:
        ranked = sorted(holdings, key=lambda h: h.shares * h.cost_basis, reverse=True)
        kept = ranked[: pdef.max_positions]
        excluded = ranked[pdef.max_positions :]
        logger.info(
            f"  max_positions={pdef.max_positions}: analyzing top {len(kept)} of "
            f"{len(holdings)} holdings by value for {pdef.name}; "
            f"{len(excluded)} excluded from analysis but still counted in totals"
        )
        return kept, excluded

    return list(holdings), []


def _resolve_portfolio_csv(portfolio_name: str) -> tuple[str, bool]:
    """Look up portfolio by name in portfolios.json and return (csv_path, is_temp).

    Thin wrapper over :func:`_resolve_portfolio_csv_ex` for callers that do not
    care which positions fell outside analyst coverage.
    """
    csv_path, is_temp, _excluded = _resolve_portfolio_csv_ex(portfolio_name)
    return csv_path, is_temp


def _resolve_portfolio_csv_ex(portfolio_name: str) -> tuple[str, bool, list]:
    """Resolve holdings source, returning (csv_path, is_temp, excluded_holdings).

    Tries fidelity_positions → transactions_path → csv_path in that order.
    Falls back to settings.portfolio_csv_path if nothing is configured.
    Caller must unlink the file when is_temp is True.

    `excluded_holdings` are positions trimmed by max_positions/allowed_tickers.
    They are deliberately kept out of the CSV (they get no analyst call) but the
    caller must still count them in portfolio totals — otherwise capping coverage
    silently shrinks the reported account.
    """
    from src.portfolio.portfolio_config import load_portfolio_defs

    excluded: list = []

    try:
        defs = load_portfolio_defs()
    except Exception as e:
        logger.warning(f"Could not load portfolio config: {e}")
        return settings.portfolio_csv_path, False, excluded

    pdef = next((d for d in defs if d.name == portfolio_name), None)
    if pdef is None:
        logger.warning(f"Portfolio '{portfolio_name}' not found in config, using default CSV")
        return settings.portfolio_csv_path, False, excluded

    def _apply_max_positions(holdings: list, pdef) -> list:
        """Keep the analyzed holdings; record the rest for the caller's totals."""
        kept, dropped = _split_by_max_positions(holdings, pdef)
        excluded.extend(dropped)
        return kept

    # ── Plaid (live, preferred when configured) ──────────────────────
    if pdef.plaid_access_token_env:
        import os
        token = os.getenv(pdef.plaid_access_token_env)
        if token:
            try:
                from src.portfolio.plaid_reader import read_plaid_holdings
                from src.portfolio.transaction_reader import export_holdings_to_csv
                logger.info(f"[plaid] Loading live holdings for {portfolio_name}...")
                holdings = read_plaid_holdings(portfolio_name)
                holdings = _apply_max_positions(holdings, pdef)
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".csv", delete=False, prefix="fb_portfolio_"
                ) as tmp:
                    tmp_path = tmp.name
                export_holdings_to_csv(holdings, tmp_path)
                return tmp_path, True, excluded
            except Exception as e:
                logger.warning(f"[plaid] Failed to fetch live holdings ({e}) — falling through to Fidelity CSV")
        else:
            logger.debug(f"[plaid] {pdef.plaid_access_token_env} not set — skipping Plaid")

    # ── Fidelity Scraper (live, when credentials are configured) ─────
    if pdef.fidelity_creds_prefix is not None:
        import os
        creds_prefix = pdef.fidelity_creds_prefix or ""
        env_prefix = f"FIDELITY_{creds_prefix.upper()}_" if creds_prefix else "FIDELITY_"
        if os.getenv(f"{env_prefix}USERNAME") and os.getenv(f"{env_prefix}PASSWORD"):
            try:
                from src.portfolio.fidelity_scraper import read_fidelity_live
                from src.portfolio.transaction_reader import export_holdings_to_csv
                logger.info(f"[fidelity] Loading live holdings for {portfolio_name} via scraper...")
                holdings = read_fidelity_live(
                    portfolio_name=portfolio_name,
                    creds_prefix=creds_prefix,
                    account_filter=pdef.fidelity_account_filter,
                    headless=True,
                )
                holdings = _apply_max_positions(holdings, pdef)
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".csv", delete=False, prefix="fb_portfolio_"
                ) as tmp:
                    tmp_path = tmp.name
                export_holdings_to_csv(holdings, tmp_path)
                return tmp_path, True, excluded
            except Exception as e:
                logger.warning(
                    f"[fidelity] Live scrape failed ({e}) — falling through to static CSV"
                )
        else:
            logger.debug(f"[fidelity] {env_prefix}USERNAME not set — skipping live scrape")

    # ── Fidelity CSV (static export fallback) ────────────────────────
    if pdef.fidelity_positions:
        from src.portfolio.fidelity_reader import read_fidelity_positions
        from src.portfolio.transaction_reader import export_holdings_to_csv
        holdings = read_fidelity_positions(
            pdef.fidelity_positions, account_filter=pdef.fidelity_account_filter
        )
        holdings = _apply_max_positions(holdings, pdef)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, prefix="fb_portfolio_"
        ) as tmp:
            tmp_path = tmp.name
        export_holdings_to_csv(holdings, tmp_path)
        return tmp_path, True, excluded

    if pdef.transactions_path:
        from src.portfolio.transaction_reader import read_transactions, export_holdings_to_csv
        holdings = read_transactions(Path(pdef.transactions_path))
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, prefix="fb_portfolio_"
        ) as tmp:
            tmp_path = tmp.name
        export_holdings_to_csv(holdings, tmp_path)
        return tmp_path, True, excluded

    if pdef.csv_path:
        return pdef.csv_path, False, excluded

    return settings.portfolio_csv_path, False, excluded


def _resolve_recipients(portfolio_name: str, override: list[str] | None) -> list[str] | None:
    """Return email recipients: explicit override takes priority, then portfolio def, then None."""
    if override:
        return override
    from src.portfolio.portfolio_config import load_portfolio_defs
    try:
        defs = load_portfolio_defs()
        pdef = next((d for d in defs if d.name == portfolio_name), None)
        if pdef and pdef.email_recipients:
            return pdef.email_recipients
    except Exception:
        pass
    return None


def _load_cached_signal(ticker: str) -> "tuple[object, object] | tuple[None, None]":
    """Return (TickerSignals, price) from api_signals if a fresh row exists for today.

    'Fresh' means created within settings.signal_cache_ttl_hours hours.
    Returns (None, None) if no suitable cached row exists.
    """
    from datetime import timedelta
    from decimal import Decimal
    from src.db.models import ApiSignal
    from src.db.session import get_db
    from src.api.models import (
        TickerSignals, QuoteSnapshot, TechnicalIndicators,
    )

    try:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            hours=settings.signal_cache_ttl_hours
        )
        today = date.today()

        # Materialize all column values inside the session to avoid DetachedInstanceError
        current_price_raw = day_change_pct_raw = rsi_raw = macd_raw = None
        analyst_rating = None
        raw: dict = {}
        with get_db() as db:
            row = (
                db.query(ApiSignal)
                .filter(
                    ApiSignal.ticker == ticker,
                    ApiSignal.signal_date == today,
                    ApiSignal.created_at >= cutoff,
                )
                .order_by(ApiSignal.created_at.desc())
                .first()
            )
            if row is None:
                return None, None
            # Capture all needed values before session closes
            current_price_raw = row.current_price
            day_change_pct_raw = row.day_change_pct
            rsi_raw = row.rsi
            macd_raw = row.macd
            analyst_rating = row.analyst_rating
            raw = dict(row.raw_data) if row.raw_data else {}

        price = Decimal(str(current_price_raw)) if current_price_raw is not None else None

        # Reconstruct a minimal TickerSignals from stored columns
        quote = None
        if price is not None:
            quote = QuoteSnapshot(
                ticker=ticker,
                current_price=price,
                day_change_pct=day_change_pct_raw,
            )
        technicals = None
        if rsi_raw is not None or macd_raw is not None:
            technicals = TechnicalIndicators(
                ticker=ticker,
                rsi=float(rsi_raw) if rsi_raw is not None else None,
                macd=float(macd_raw) if macd_raw is not None else None,
            )

        signals = TickerSignals(
            ticker=ticker,
            quote=quote,
            technicals=technicals,
            consensus_rating=analyst_rating,
        )

        # Restore raw data fields if available
        if raw:
            from src.api.models import FinvizFundamentals
            fund_data = raw.get("fundamentals")
            if fund_data and isinstance(fund_data, dict):
                valid_fields = FinvizFundamentals.model_fields.keys()
                filtered = {k: v for k, v in fund_data.items() if k in valid_fields}
                if filtered:
                    signals.fundamentals = FinvizFundamentals(**filtered)
            sec_data = raw.get("sec_filings")
            if sec_data and isinstance(sec_data, list):
                signals.sec_filings = sec_data

        return signals, price
    except Exception as e:
        logger.debug(f"Signal cache lookup failed for {ticker}: {e}")
        return None, None


def _save_signal_to_db(ticker: str, signals: object, price: "Decimal | None") -> None:
    """Persist api_signals row for the given ticker (upsert by ticker + signal_date)."""
    from decimal import Decimal
    from src.db.models import ApiSignal
    from src.db.session import get_db

    try:
        today = date.today()
        raw: dict = {}
        # Stash fundamentals + SEC filings in raw_data for cache reconstruction
        if hasattr(signals, "fundamentals") and signals.fundamentals is not None:
            raw["fundamentals"] = signals.fundamentals.model_dump(exclude_none=True)
        if hasattr(signals, "sec_filings") and signals.sec_filings:
            raw["sec_filings"] = signals.sec_filings

        rsi = macd = day_change_pct = analyst_rating = price_target = benzinga_sentiment = None
        if hasattr(signals, "technicals") and signals.technicals:
            t = signals.technicals
            rsi = t.rsi
            macd = t.macd
        if hasattr(signals, "quote") and signals.quote:
            q = signals.quote
            day_change_pct = float(q.day_change_pct) if q.day_change_pct is not None else None
        analyst_rating = getattr(signals, "consensus_rating", None)

        with get_db() as db:
            existing = db.query(ApiSignal).filter_by(
                ticker=ticker,
                signal_date=today,
            ).first()
            data = dict(
                current_price=price,
                day_change_pct=day_change_pct,
                rsi=rsi,
                macd=macd,
                analyst_rating=analyst_rating,
                price_target=price_target,
                benzinga_sentiment=benzinga_sentiment,
                raw_data=raw or None,
            )
            if existing:
                for k, v in data.items():
                    setattr(existing, k, v)
            else:
                db.add(ApiSignal(ticker=ticker, signal_date=today, **data))
    except Exception as e:
        logger.debug(f"Failed to save signal to DB for {ticker}: {e}")


def _upsert_pipeline_run(
    run_id: str,
    portfolio_name: str,
    report_date: date,
    status: str = "running",
    phase: str | None = None,
    total_tickers: int | None = None,
    tickers_complete: int | None = None,
    error_message: str | None = None,
) -> None:
    """Create or update the pipeline_runs row for this portfolio/date."""
    from src.db.models import PipelineRun
    from src.db.session import get_db

    try:
        with get_db() as db:
            row = db.query(PipelineRun).filter_by(
                portfolio_name=portfolio_name,
                report_date=report_date,
            ).first()
            if row is None:
                row = PipelineRun(
                    run_id=run_id,
                    portfolio_name=portfolio_name,
                    report_date=report_date,
                )
                db.add(row)
            row.status = status
            if phase is not None:
                row.phase = phase
            if total_tickers is not None:
                row.total_tickers = total_tickers
            if tickers_complete is not None:
                row.tickers_complete = tickers_complete
            if error_message is not None:
                row.error_message = error_message
            if status in ("complete", "failed"):
                row.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    except Exception as e:
        logger.debug(f"PipelineRun upsert failed (non-fatal): {e}")


def _fetch_signals_for_ticker(ticker: str):
    """Fetch massive.com signals + Finviz fundamentals in a worker thread."""
    try:
        from decimal import Decimal
        from src.api.massive_client import MassiveClient
        from src.api.endpoints import MassiveEndpoints

        with MassiveClient() as client:
            endpoints = MassiveEndpoints(client)
            signals = endpoints.get_ticker_signals(ticker)
            price = (
                Decimal(str(signals.quote.current_price))
                if signals and signals.quote and signals.quote.current_price
                else None
            )

        # Augment with Finviz fundamentals and SEC filings
        try:
            from src.scrapers.finviz_scraper import FinvizScraper
            from src.api.models import FinvizFundamentals

            fv = FinvizScraper()
            fund_data = fv.scrape_fundamentals(ticker)
            if fund_data and signals:
                # Only keep fields that belong to FinvizFundamentals
                valid_fields = FinvizFundamentals.model_fields.keys()
                filtered = {k: v for k, v in fund_data.items() if k in valid_fields}
                signals.fundamentals = FinvizFundamentals(**filtered)

            sec_filings = fv.scrape_sec_filings(ticker)
            if sec_filings and signals:
                signals.sec_filings = sec_filings
        except Exception as fv_err:
            logger.debug(f"Finviz fundamentals fetch failed for {ticker}: {fv_err}")

        return ticker, signals, price
    except Exception as e:
        logger.warning(f"Signals fetch failed for {ticker}: {e}")
        return ticker, None, None


def _run_squeeze_checks(
    analyst_reports: list,
    director_report: object,
    squeeze_tickers: tuple[str, ...] = ("INTU",),
) -> None:
    """Run short squeeze checks for tickers held on a squeeze thesis.

    For each ticker in squeeze_tickers that is present in analyst_reports:
      - Score the squeeze setup (0-10)
      - If score >= 7: append a note to the analyst report's tax_note field
        (rendered in the newsletter under that ticker's section) and append an
        action item to director_report.action_items
      - If score >= 7: fire a Discord alert (non-blocking; failure is logged, not raised)

    Runs silently — all errors are caught and logged so a squeeze check failure
    never aborts the main pipeline.

    Args:
        analyst_reports: List of AnalystReport objects from the pipeline.
        director_report:  DirectorReport with an action_items list.
        squeeze_tickers:  Tickers to check (default: INTU only).
    """
    try:
        from src.alerts.short_squeeze_monitor import run_squeeze_check, squeeze_alert
    except ImportError as e:
        logger.warning(f"squeeze_check: import failed — skipping ({e})")
        return

    # Build a quick lookup: ticker → analyst report (case-insensitive)
    report_map = {ar.ticker.upper(): ar for ar in analyst_reports}

    for ticker in squeeze_tickers:
        ticker = ticker.upper()
        if ticker not in report_map:
            logger.debug(f"squeeze_check: {ticker} not in portfolio — skipping")
            continue

        logger.info(f"squeeze_check: running for {ticker}…")
        try:
            result = run_squeeze_check(ticker)
        except Exception as e:
            logger.warning(f"squeeze_check: failed for {ticker} ({e}) — skipping")
            continue

        logger.info(
            f"squeeze_check: {ticker} score={result.score}/10 ({result.score_label})"
        )

        if result.score < 7:
            logger.info(
                f"squeeze_check: {ticker} score {result.score}/10 < 7 — no newsletter injection"
            )
            continue

        # ── Inject into analyst report's tax_note (rendered in newsletter) ──
        ar = report_map[ticker]
        squeeze_note = (
            f"**SHORT SQUEEZE MONITOR — Score {result.score}/10 ({result.score_label})**\n"
            f"Short Float: {f'{result.short_float * 100:.1f}%' if result.short_float is not None else 'N/A'} | "
            f"Days-to-Cover: {f'{result.days_to_cover:.1f}' if result.days_to_cover is not None else 'N/A'} | "
            f"RSI: {f'{result.rsi_14:.1f}' if result.rsi_14 is not None else 'N/A'}\n"
            f"Signals: {'; '.join(result.signals) if result.signals else 'See score breakdown.'}\n"
            f"Hold position — squeeze setup forming. Confirm thesis before taking action."
        )
        if ar.tax_note:
            ar.tax_note = ar.tax_note + "\n\n" + squeeze_note
        else:
            ar.tax_note = squeeze_note

        # ── Add action item to director report ──────────────────────────────
        action_item = (
            f"SQUEEZE ALERT: {ticker} scores {result.score}/10 — "
            f"short float {f'{result.short_float * 100:.1f}%' if result.short_float is not None else 'N/A'}, "
            f"DTC {f'{result.days_to_cover:.1f}' if result.days_to_cover is not None else 'N/A'}. "
            f"Hold — squeeze setup is forming."
        )
        try:
            director_report.action_items.append(action_item)
        except Exception as e:
            logger.debug(f"squeeze_check: could not append action item: {e}")

        # ── Discord alert ────────────────────────────────────────────────────
        try:
            squeeze_alert(result)
        except Exception as e:
            logger.warning(f"squeeze_check: Discord alert failed for {ticker}: {e}")


def _run_insider_cluster_checks(
    analyst_reports: list,
    director_report: object,
) -> None:
    """Run EDGAR Form 4 insider cluster checks for all portfolio tickers.

    For each ticker in analyst_reports:
      - Scan Form 4s over the past 60 days for open-market purchases
      - If 3+ distinct insiders bought within any 30-day window, inject a note
        into the analyst report and add an action item to the director report
      - Fire a Discord alert for STRONG clusters (2+ officers OR 5+ insiders)

    Runs silently — errors are caught per-ticker so a single failure never
    aborts the main pipeline. EDGAR network calls are rate-limited (~8 req/s).

    Args:
        analyst_reports: List of AnalystReport objects from the pipeline.
        director_report:  DirectorReport with an action_items list.
    """
    try:
        from src.alerts.insider_cluster import run_insider_cluster_check, cluster_alert
    except ImportError as e:
        logger.warning(f"insider_cluster: import failed — skipping ({e})")
        return

    report_map = {ar.ticker.upper(): ar for ar in analyst_reports}
    tickers = list(report_map.keys())
    logger.info(f"insider_cluster: scanning {len(tickers)} portfolio tickers via EDGAR…")

    for ticker in tickers:
        logger.info(f"insider_cluster: checking {ticker}…")
        try:
            result = run_insider_cluster_check(ticker)
        except Exception as e:
            logger.warning(f"insider_cluster: failed for {ticker} ({e}) — skipping")
            continue

        if result is None:
            continue

        # ── Inject into analyst report's tax_note ──────────────────────────────
        ar = report_map[ticker]
        cluster_note = result.newsletter_section()
        if ar.tax_note:
            ar.tax_note = ar.tax_note + "\n\n" + cluster_note
        else:
            ar.tax_note = cluster_note

        # ── Add action item to director report ──────────────────────────────────
        val_str = f"~${result.total_value:,.0f}" if result.total_value else "value unknown"
        action_item = (
            f"INSIDER CLUSTER BUY: {ticker} — {result.insider_count} insiders "
            f"({result.officer_count} officers) bought {result.total_shares:,.0f} sh "
            f"({val_str}) between {result.window_start}→{result.window_end}. "
            f"Signal: {result.signal_strength}."
        )
        try:
            director_report.action_items.append(action_item)
        except Exception as e:
            logger.debug(f"insider_cluster: could not append action item: {e}")

        # ── Discord alert for STRONG clusters only ───────────────────────────────
        if result.signal_strength == "STRONG":
            try:
                cluster_alert(result)
            except Exception as e:
                logger.warning(f"insider_cluster: Discord alert failed for {ticker}: {e}")
        else:
            logger.info(
                f"insider_cluster: {ticker} cluster is {result.signal_strength} — "
                "no Discord alert (STRONG threshold not met)"
            )


def run_pipeline(
    portfolio_csv: str | None = None,
    report_date: date | None = None,
    skip_scrape: bool = False,
    skip_email: bool = False,
    output_dir: Path | None = None,
    portfolio_name: str = "nbossn",
    portfolio_label: str | None = None,
    email_recipients: list[str] | None = None,
) -> dict:
    """Full pipeline: portfolio → scrape → analyse → direct → newsletter → email.

    Performance notes (15+ tickers):
    - Phase 2 (scrape): parallel across tickers, bounded by MAX_PARALLEL_TICKERS
    - Phase 3 (signals): parallel across tickers + parallel within each ticker (4 HTTP calls)
    - Phase 4 (analysts): all tickers run concurrently via asyncio.gather, bounded by MAX_PARALLEL_ANALYSTS
    """
    today = report_date or date.today()
    out = output_dir or Path("newsletters")
    label = portfolio_label or portfolio_name.replace("_", " ").title()
    t0 = time.monotonic()
    run_id = str(uuid.uuid4())

    logger.info("=" * 60)
    logger.info(f"Financial Bytes pipeline starting — {today} [{portfolio_name}]")
    logger.info("=" * 60)

    # ── Phase 1: Portfolio ─────────────────────────────────────────
    logger.info("[1/5] Loading portfolio...")
    # Resolve holdings source: explicit path → portfolios.json lookup → env default
    csv_path, _is_temp_csv, _excluded_holdings = (
        (portfolio_csv, False, []) if portfolio_csv
        else _resolve_portfolio_csv_ex(portfolio_name)
    )
    email_recipients = _resolve_recipients(portfolio_name, email_recipients)
    lot_overrides = _load_purchase_history(portfolio_name)
    if lot_overrides:
        logger.info(f"      Purchase history loaded: {list(lot_overrides.keys())}")
    try:
        holdings = read_portfolio(csv_path)
        save_portfolio_to_db(holdings, portfolio_name=portfolio_name)
    finally:
        if _is_temp_csv:
            try:
                os.unlink(csv_path)
            except Exception:
                pass
    # Apply earliest lot dates to holdings for accurate analyst context
    if lot_overrides:
        _apply_purchase_history_to_holdings(holdings, lot_overrides)
    snapshot = PortfolioSnapshot(
        holdings=holdings,
        lot_overrides=lot_overrides,
        excluded_holdings=_excluded_holdings,
    )
    if snapshot.is_truncated:
        logger.info(
            f"      {snapshot.analyzed_count} of {snapshot.position_count} position(s) under "
            f"analyst coverage; the other {len(_excluded_holdings)} are still counted in totals"
        )
    logger.debug(f"      {snapshot.position_count} holding(s) loaded — value ${snapshot.total_value:,.2f}")

    # Register pipeline run — allows resume detection on subsequent calls.
    # _pipeline_exc tracks any failure so the finally block can write "failed" to DB.
    _upsert_pipeline_run(
        run_id=run_id,
        portfolio_name=portfolio_name,
        report_date=today,
        status="running",
        phase="portfolio",
        total_tickers=len(holdings),
    )
    # ── Phase 2: Scrape ────────────────────────────────────────────
    # scrape_tickers_parallel uses skip_cached=True by default — already DB-first.
    # Articles scraped today are returned from DB without re-fetching.
    # skip_scrape=True bypasses entirely (useful for tests or resume-only runs).
    all_articles: dict[str, list] = {}
    if skip_scrape:
        logger.info("[2/5] Scraping skipped (--skip-scrape)")
    else:
        logger.info(
            f"[2/5] Scraping news articles "
            f"(parallel workers: {settings.max_parallel_tickers}, DB-cached)..."
        )
        from src.scrapers.scraper_orchestrator import scrape_tickers_parallel

        tickers = [h.ticker for h in holdings]
        all_articles = scrape_tickers_parallel(tickers, max_workers=settings.max_parallel_tickers)
    for ticker, arts in all_articles.items():
        logger.info(f"      {ticker}: {len(arts)} article(s)")
    _upsert_pipeline_run(run_id=run_id, portfolio_name=portfolio_name, report_date=today, phase="scrape")

    # ── Phase 3: massive.com signals (DB-first, then live fetch) ──
    logger.info("[3/5] Fetching market signals (DB-first cache, then live)...")
    ticker_signals: dict[str, object] = {}
    market_prices: dict[str, "Decimal"] = {}

    # Split into cache hits and misses
    tickers_need_fetch: list[str] = []
    for h in holdings:
        cached_signals, cached_price = _load_cached_signal(h.ticker)
        if cached_signals is not None:
            ticker_signals[h.ticker] = cached_signals
            if cached_price is not None:
                market_prices[h.ticker] = cached_price
            logger.info(f"      {h.ticker}: signals from DB cache")
        else:
            tickers_need_fetch.append(h.ticker)

    if tickers_need_fetch:
        logger.info(f"      Live fetch needed for {len(tickers_need_fetch)} ticker(s): {tickers_need_fetch}")
        signal_workers = min(len(tickers_need_fetch), 10)
        with ThreadPoolExecutor(max_workers=signal_workers) as pool:
            future_map = {
                pool.submit(_fetch_signals_for_ticker, ticker): ticker
                for ticker in tickers_need_fetch
            }
            for future in as_completed(future_map):
                ticker, signals, price = future.result()
                if signals is not None:
                    ticker_signals[ticker] = signals
                    _save_signal_to_db(ticker, signals, price)
                if price is not None:
                    market_prices[ticker] = price
                logger.info(f"      {ticker}: {'signals fetched' if signals else 'signals unavailable'}")
    else:
        logger.info("      All signals served from DB cache")

    _upsert_pipeline_run(run_id=run_id, portfolio_name=portfolio_name, report_date=today, phase="signals")

    # Positions outside analyst coverage still need a market price, or they are
    # valued at cost basis and report $0.00 unrealized P&L.
    _price_excluded_holdings(_excluded_holdings, market_prices)

    if market_prices:
        snapshot = PortfolioSnapshot(
            holdings=holdings,
            prices=market_prices,
            as_of=today,
            lot_overrides=lot_overrides,
            excluded_holdings=_excluded_holdings,
        )

    # A holding with no market price is valued at cost basis, which reports
    # unrealized P&L of exactly $0.00 — indistinguishable from a flat position.
    # BRKB shipped that way in ten consecutive newsletters. Say so out loud.
    if snapshot.missing_prices:
        logger.warning(
            f"[price] {len(snapshot.missing_prices)}/{snapshot.position_count} holding(s) have no market "
            f"price and will be valued at cost basis (zero P&L): "
            f"{', '.join(snapshot.missing_prices)}"
        )
    else:
        logger.info(f"[price] All {snapshot.position_count} holding(s) priced at market")

    # ── Phase 4: Analyst agents (concurrent via asyncio, DB-first) ─
    # analyze_ticker_async checks the summaries table before calling Claude.
    # On a resumed run where all 345 summaries are already in DB, this phase
    # returns immediately without any Claude calls.
    logger.info(
        f"[4/5] Running analyst agents "
        f"(max concurrent: {settings.max_parallel_analysts}, DB-first)..."
    )
    from src.agents.analyst_agent import run_analysts_parallel, _load_summary_from_db

    # Pre-filter: skip tickers whose summaries are already cached in DB (fast resume).
    # On a 345-ticker resume this avoids 345 semaphore-serialized DB trips in asyncio.gather.
    cached_reports: list = []
    holdings_to_analyze = holdings
    if settings.analyst_cache_enabled:
        holdings_to_analyze = []
        for h in holdings:
            cached = _load_summary_from_db(h.ticker, portfolio_name, today)
            if cached is not None:
                cached_reports.append(cached)
            else:
                holdings_to_analyze.append(h)
        if cached_reports:
            logger.info(
                f"      Resume: {len(cached_reports)}/{len(holdings)} tickers already in DB — "
                f"analyzing {len(holdings_to_analyze)} remaining"
            )

    analyst_reports = asyncio.run(
        run_analysts_parallel(
            holdings=holdings_to_analyze,
            all_articles=all_articles,
            ticker_signals=ticker_signals,
            report_date=today,
            max_concurrent=settings.max_parallel_analysts,
            portfolio_name=portfolio_name,
        )
    )

    # Combine newly-analyzed + pre-cached reports for tax notes + director phases
    analyst_reports = analyst_reports + cached_reports

    for report in analyst_reports:
        logger.info(
            f"      {report.ticker}: {report.recommendation} ({report.confidence:.0%} confidence)"
        )
    _upsert_pipeline_run(
        run_id=run_id,
        portfolio_name=portfolio_name,
        report_date=today,
        phase="analysts",
        tickers_complete=len(analyst_reports),
    )

    # ── Attach per-ticker tax notes ────────────────────────────────────────
    # Computed deterministically from lot data — no LLM call needed.
    try:
        from src.portfolio.tax_calculator import generate_tax_note
        tax_summary = snapshot.tax_summary
        # Group lots by ticker for O(1) lookup
        lots_by_ticker: dict[str, list] = {}
        for lot in tax_summary.lots:
            lots_by_ticker.setdefault(lot.ticker, []).append(lot)
        for report in analyst_reports:
            ticker_lots = lots_by_ticker.get(report.ticker, [])
            report.tax_note = generate_tax_note(report.ticker, ticker_lots, report.recommendation)
    except Exception as tax_err:
        logger.warning(f"Tax note generation failed: {tax_err}")

    # ── Phase 5: Director agent ────────────────────────────────────
    logger.info("[5/5] Director synthesis...")
    from src.agents.director_agent import synthesize_portfolio

    # Find most recent prior newsletter for continuity context
    # generator saves to {out}/{portfolio_name}/ — look there first, then root out/
    prior_nl_path: Path | None = None
    try:
        for nl_dir in [out / portfolio_name, out]:
            prior_htmls = sorted(nl_dir.glob("*.html"), reverse=True)
            if prior_htmls:
                prior_nl_path = prior_htmls[0]
                break
        if prior_nl_path:
            logger.info(f"      Prior newsletter: {prior_nl_path.name}")
    except Exception:
        pass

    director_report = synthesize_portfolio(
        snapshot,
        analyst_reports=analyst_reports or None,  # in-memory list when available; DB fallback on resume
        report_date=today,
        portfolio_name=portfolio_name,
        prior_newsletter_path=prior_nl_path,
    )
    logger.info(f"      Theme: {director_report.market_theme[:80]}")
    logger.info(f"      Sentiment: {director_report.overall_sentiment:+.2f}")
    _upsert_pipeline_run(run_id=run_id, portfolio_name=portfolio_name, report_date=today, phase="director")

    # ── Short squeeze monitoring (INTU / any ticker with squeeze thesis) ──────
    _run_squeeze_checks(analyst_reports, director_report)

    # ── EDGAR Form 4 insider cluster scanner (all portfolio tickers) ──────────
    _run_insider_cluster_checks(analyst_reports, director_report)

    # ── Newsletter generation ──────────────────────────────────────
    logger.info("[6/6] Generating newsletter...")
    from src.newsletter.generator import generate

    paths = generate(
        report=director_report,
        analyst_reports=analyst_reports,
        snapshot=snapshot,
        report_date=today,
        output_dir=out,
        portfolio_name=portfolio_name,
        portfolio_label=label,
    )

    # ── Email delivery ─────────────────────────────────────────────
    if not skip_email:
        from src.delivery.email_sender import send_newsletter

        html_path = paths.get("html")
        md_path = paths.get("md")
        pdf_path = paths.get("pdf")
        if html_path and html_path.exists():
            send_newsletter(
                report_date=today,
                html_content=html_path.read_text(encoding="utf-8"),
                markdown_content=md_path.read_text(encoding="utf-8") if md_path and md_path.exists() else "",
                pdf_path=pdf_path,
                market_theme=director_report.market_theme,
                recipients=email_recipients or None,
            )
    else:
        logger.info("Email delivery skipped (--skip-email flag)")

    # ── Performance snapshot ───────────────────────────────────────
    try:
        from src.portfolio.performance import track_and_save
        track_and_save(snapshot, portfolio_name, today)
    except Exception as e:
        logger.warning(f"[perf] Snapshot failed (non-fatal): {e}")

    elapsed = time.monotonic() - t0
    _upsert_pipeline_run(
        run_id=run_id,
        portfolio_name=portfolio_name,
        report_date=today,
        status="complete",
        phase="newsletter",
        tickers_complete=len(analyst_reports),
    )
    logger.info(f"Pipeline complete in {elapsed:.1f}s")
    logger.info("=" * 60)

    return {
        "status": "ok",
        "report_date": today,
        "paths": paths,
        "director_report": director_report,
        "analyst_reports": analyst_reports,
    }


def mark_pipeline_run_failed(
    portfolio_name: str,
    report_date: "date",
    error_message: str,
) -> None:
    """Mark a running pipeline_runs row as failed.

    Looks up by (portfolio_name, report_date) — no run_id needed.
    Called from the scheduler's except block when run_pipeline raises.
    """
    from src.db.models import PipelineRun
    from src.db.session import get_db
    from datetime import datetime, timezone

    try:
        with get_db() as db:
            row = db.query(PipelineRun).filter_by(
                portfolio_name=portfolio_name,
                report_date=report_date,
            ).first()
            if row and row.status == "running":
                row.status = "failed"
                row.error_message = error_message[:500]
                row.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    except Exception as e:
        logger.debug(f"mark_pipeline_run_failed DB update skipped: {e}")
