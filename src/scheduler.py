"""APScheduler daemon — runs the pipeline daily at 5AM Eastern."""
from __future__ import annotations

import signal
import sys

from loguru import logger


def _run_all_portfolios() -> None:
    from src.pipeline.main_pipeline import run_pipeline
    from src.portfolio.portfolio_config import load_portfolio_defs

    portfolios = load_portfolio_defs()
    logger.info(f"Scheduler: running {len(portfolios)} portfolio(s)")

    for pdef in portfolios:
        logger.info(f"  → Starting pipeline for portfolio: {pdef.name}")
        try:
            # Resolve holdings source: Fidelity positions > transactions > csv
            csv_path = pdef.csv_path
            tmp_path = None

            if pdef.fidelity_positions:
                import tempfile
                from pathlib import Path
                from src.portfolio.fidelity_reader import export_fidelity_to_portfolio_csv

                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".csv", delete=False, prefix=f"fb_{pdef.name}_"
                ) as tmp:
                    tmp_path = tmp.name
                export_fidelity_to_portfolio_csv(
                    pdef.fidelity_positions, tmp_path,
                    account_filter=pdef.fidelity_account_filter,
                )
                csv_path = tmp_path

            elif pdef.transactions_path:
                import tempfile
                import os
                from pathlib import Path
                from src.portfolio.transaction_reader import read_transactions, export_holdings_to_csv

                holdings = read_transactions(Path(pdef.transactions_path))
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".csv", delete=False, prefix=f"fb_{pdef.name}_"
                ) as tmp:
                    tmp_path = tmp.name
                export_holdings_to_csv(holdings, tmp_path)
                csv_path = tmp_path

            try:
                run_pipeline(
                    portfolio_csv=csv_path,
                    portfolio_name=pdef.name,
                    portfolio_label=pdef.label,
                    email_recipients=pdef.email_recipients or None,
                )
            finally:
                if tmp_path:
                    import os
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

        except Exception as e:
            logger.exception(f"Pipeline failed for portfolio '{pdef.name}': {e}")


def _send_reminder_discord(reminders: list[dict]) -> None:
    """Post pending decision reminders to Discord webhook."""
    import os
    import requests

    webhook_url = os.getenv(
        "DISCORD_WEBHOOK_URL",
        "https://discord.com/api/webhooks/1497193787900825711/09tGLG_ZzAhtzrXSl3zJpHCRbxlsWYyFWoaEzAYEpRKoi8FSBP1Y40vazPjfyRDzqMFZ",
    )

    lines = ["⏰ **Decision reminder(s) due today**"]
    for r in reminders:
        lines.append(f"• **Deadline {r['deadline']}:** {r['context']}")
    lines.append("_Check vault for analysis and action steps._")

    try:
        resp = requests.post(webhook_url, json={"content": "\n".join(lines)}, timeout=10)
        resp.raise_for_status()
        logger.info(f"Reminder Discord alert sent ({len(reminders)} reminder(s))")
    except Exception as e:
        logger.warning(f"Reminder Discord alert failed: {e}")


def _run_reminder_check() -> None:
    """Run at 6:00 AM ET — send Discord alert for any reminders due within 24h."""
    from src.portfolio.reminders import get_due_reminders, mark_sent

    due = get_due_reminders()
    if not due:
        logger.info("Reminder check: no reminders due today")
        return

    logger.info(f"Reminder check: {len(due)} reminder(s) due")
    _send_reminder_discord(due)
    for r in due:
        mark_sent(r["id"])


def _send_premarket_discord(results: list) -> None:
    """Post premarket inference results to Discord webhook."""
    import os
    import requests

    webhook_url = os.getenv(
        "DISCORD_WEBHOOK_URL",
        "https://discord.com/api/webhooks/1497193787900825711/09tGLG_ZzAhtzrXSl3zJpHCRbxlsWYyFWoaEzAYEpRKoi8FSBP1Y40vazPjfyRDzqMFZ",
    )

    lines = ["📊 **Pre-market earnings check** (7:10 AM ET)"]
    for result in results:
        if result.data_available:
            pct = f"{result.pct_change:+.1%}" if result.pct_change is not None else "N/A"
            lines.append(
                f"**{result.ticker}:** ${result.premarket_price:.2f} ({pct}) → {result.inference}"
            )
        else:
            lines.append(f"**{result.ticker}:** {result.inference}")

    lines.append("_Apply guide thresholds — check vault for decision framework._")

    try:
        resp = requests.post(webhook_url, json={"content": "\n".join(lines)}, timeout=10)
        resp.raise_for_status()
        logger.info("Premarket earnings Discord alert sent")
    except Exception as e:
        logger.warning(f"Premarket Discord alert failed: {e}")


def _run_premarket_earnings_check() -> None:
    """Run at 7:10 AM ET on earnings days — fires premarket_check for pre-market reporters."""
    from src.portfolio.earnings_calendar import get_todays_premarket_events
    from src.portfolio.premarket_check import check_earnings_day

    events = get_todays_premarket_events()
    if not events:
        logger.info("Premarket earnings check: no pre-market events today")
        return

    pairs = []
    for event in events:
        ticker = event["ticker"]
        prev_close = event.get("prev_close")
        if prev_close is None:
            import yfinance as yf
            pc = getattr(yf.Ticker(ticker).fast_info, "previous_close", None)
            if pc is None:
                logger.warning(f"Cannot fetch prev_close for {ticker} — skipping premarket check")
                continue
            prev_close = float(pc)
        pairs.append((ticker, prev_close))

    if not pairs:
        return

    results = check_earnings_day(pairs)
    for result in results:
        line = result.summary_line()
        logger.info(f"Premarket check: {line}")
        if result.data_available and result.detail:
            logger.info(f"  Detail: {result.detail}")

    _send_premarket_discord(results)


def start_scheduler() -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from src.config import settings

    # Parse "HH:MM" from settings
    hour, minute = settings.pipeline_start_time.split(":")

    scheduler = BlockingScheduler(timezone=settings.newsletter_timezone)

    # Daily newsletter pipeline
    scheduler.add_job(
        _run_all_portfolios,
        CronTrigger(
            hour=int(hour),
            minute=int(minute),
            timezone=settings.newsletter_timezone,
        ),
        id="daily_pipeline",
        name="Financial Bytes Daily Pipeline",
        misfire_grace_time=300,
        coalesce=True,
    )

    # Decision reminder check — fires at 6:00 AM ET daily
    # Sends Discord alert for any reminders with deadline within 24h
    scheduler.add_job(
        _run_reminder_check,
        CronTrigger(hour=6, minute=0, timezone="America/New_York"),
        id="reminder_check",
        name="Decision Reminder Check",
        misfire_grace_time=300,
        coalesce=True,
    )

    # Pre-market earnings check — fires at 7:10 AM ET daily
    # Only runs actionable logic on days with pre-market events in the calendar
    scheduler.add_job(
        _run_premarket_earnings_check,
        CronTrigger(hour=7, minute=10, timezone="America/New_York"),
        id="premarket_earnings_check",
        name="Pre-Market Earnings Check",
        misfire_grace_time=600,  # 10-minute grace window (pre-market window is short)
        coalesce=True,
    )

    def _shutdown(signum, frame):
        logger.info("Scheduler shutting down...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    next_run = scheduler.get_job("daily_pipeline").next_run_time
    logger.info(f"Scheduler started. Next daily pipeline run: {next_run}")
    logger.info("Pre-market earnings check scheduled at 7:10 AM ET daily")
    scheduler.start()
