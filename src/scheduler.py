"""APScheduler daemon — runs the pipeline daily at 5AM Eastern."""
from __future__ import annotations

import signal
import sys

from loguru import logger


def _resolve_portfolio_csv(pdef) -> tuple[str | None, str | None]:
    """Return (csv_path, tmp_path) for a portfolio def. Caller must unlink tmp_path when done.

    If pdef.max_positions is set, only the top-N holdings by (shares × cost_basis) value
    are kept — prevents 100s-of-position accounts from blowing up the pipeline.
    """
    import csv
    import os
    import tempfile

    if pdef.fidelity_positions:
        from src.portfolio.fidelity_reader import export_fidelity_to_portfolio_csv
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, prefix=f"fb_{pdef.name}_"
        ) as tmp:
            tmp_path = tmp.name
        holdings = export_fidelity_to_portfolio_csv(
            pdef.fidelity_positions, tmp_path,
            account_filter=pdef.fidelity_account_filter,
        )

        # Apply max_positions filter: keep top-N by position value (shares × cost_basis)
        if pdef.max_positions and len(holdings) > pdef.max_positions:
            from decimal import Decimal
            top = sorted(holdings, key=lambda h: h.shares * h.cost_basis, reverse=True)
            top = top[: pdef.max_positions]
            logger.info(
                f"  max_positions={pdef.max_positions}: keeping top {len(top)} of "
                f"{len(holdings)} holdings by value for {pdef.name}"
            )
            # Rewrite the temp CSV with only the top positions
            with open(tmp_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["ticker", "shares", "cost_basis", "purchase_date"])
                for h in top:
                    writer.writerow([h.ticker, h.shares, h.cost_basis, h.purchase_date])

        return tmp_path, tmp_path

    if pdef.transactions_path:
        from pathlib import Path
        from src.portfolio.transaction_reader import read_transactions, export_holdings_to_csv
        holdings = read_transactions(Path(pdef.transactions_path))
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, prefix=f"fb_{pdef.name}_"
        ) as tmp:
            tmp_path = tmp.name
        export_holdings_to_csv(holdings, tmp_path)
        return tmp_path, tmp_path

    return pdef.csv_path, None


def _send_group_email(group_name: str, runs: list[tuple]) -> None:
    """Send one combined newsletter email for all portfolios in a named group.

    Extracts <body> from each portfolio's HTML output and concatenates them into
    a single email document, with a visual divider between portfolios.
    Recipients are the union of all portfolios' email_recipients lists.
    """
    import re
    from datetime import date
    from pathlib import Path
    from src.delivery.email_sender import send_newsletter

    if not runs:
        return

    # Deduplicated recipient list (insertion order preserved)
    seen: set[str] = set()
    all_recipients: list[str] = []
    for pdef, _ in runs:
        for r in (pdef.email_recipients or []):
            if r not in seen:
                all_recipients.append(r)
                seen.add(r)

    if not all_recipients:
        logger.warning(f"Group '{group_name}': no recipients — skipping combined email")
        return

    html_parts: list[tuple[str, str]] = []   # (label, full_html)
    md_parts: list[str] = []
    market_themes: list[str] = []
    report_date = None

    for pdef, result in runs:
        paths = result.get("paths", {})
        html_path = paths.get("html")
        md_path = paths.get("md")
        if html_path and Path(html_path).exists():
            html_parts.append((pdef.label, Path(html_path).read_text(encoding="utf-8")))
        if md_path and Path(md_path).exists():
            md_parts.append(Path(md_path).read_text(encoding="utf-8"))
        dr = result.get("director_report")
        if dr and getattr(dr, "market_theme", None):
            market_themes.append(dr.market_theme)
        if report_date is None:
            report_date = result.get("report_date")

    if not html_parts:
        logger.error(f"Group '{group_name}': no HTML output found — skipping combined email")
        return

    def _body(html: str) -> str:
        m = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
        return m.group(1) if m else html

    DIVIDER = (
        '<hr style="border:none;border-top:3px solid #e5e7eb;margin:48px 0 40px 0;">'
        '<div style="text-align:center;font-size:11px;color:#9ca3af;font-family:sans-serif;'
        'letter-spacing:1.5px;text-transform:uppercase;margin-bottom:32px;">'
        "{label}</div>"
    )

    combined_bodies: list[str] = []
    for i, (label, html) in enumerate(html_parts):
        if i > 0:
            combined_bodies.append(DIVIDER.format(label=label))
        combined_bodies.append(_body(html))

    combined_html = re.sub(
        r"<body[^>]*>.*?</body>",
        "<body>\n" + "\n".join(combined_bodies) + "\n</body>",
        html_parts[0][1],
        flags=re.DOTALL | re.IGNORECASE,
    )
    combined_md = "\n\n---\n\n".join(md_parts)
    market_theme = market_themes[0] if market_themes else None

    logger.info(
        f"Group '{group_name}': sending combined email "
        f"({len(runs)} portfolio(s)) → {all_recipients}"
    )
    send_newsletter(
        report_date=report_date or date.today(),
        html_content=combined_html,
        markdown_content=combined_md,
        market_theme=market_theme,
        recipients=all_recipients,
    )


def _run_all_portfolios() -> None:
    import os
    from src.pipeline.main_pipeline import run_pipeline
    from src.portfolio.portfolio_config import load_portfolio_defs

    portfolios = load_portfolio_defs()
    logger.info(f"Scheduler: running {len(portfolios)} portfolio(s)")

    # group_name → [(pdef, result)] for deferred combined-email sending
    group_runs: dict[str, list[tuple]] = {}

    for pdef in portfolios:
        logger.info(f"  → Starting pipeline for portfolio: {pdef.name}")
        try:
            csv_path, tmp_path = _resolve_portfolio_csv(pdef)
            in_group = bool(pdef.email_group)

            try:
                result = run_pipeline(
                    portfolio_csv=csv_path,
                    portfolio_name=pdef.name,
                    portfolio_label=pdef.label,
                    email_recipients=pdef.email_recipients or None,
                    skip_email=in_group,   # grouped portfolios defer email until combined
                )
                if in_group:
                    group_runs.setdefault(pdef.email_group, []).append((pdef, result))
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

        except Exception as e:
            logger.exception(f"Pipeline failed for portfolio '{pdef.name}': {e}")

    # Send one combined email per group
    for group_name, runs in group_runs.items():
        try:
            _send_group_email(group_name, runs)
        except Exception as e:
            logger.exception(f"Combined group email failed for group '{group_name}': {e}")


def _send_reminder_discord(reminders: list[dict]) -> None:
    """Post pending decision reminders to Discord webhook."""
    import os
    import requests

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("DISCORD_WEBHOOK_URL not set — skipping Discord alert")
        return

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

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("DISCORD_WEBHOOK_URL not set — skipping Discord alert")
        return

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

    logger.info("Scheduler starting — daily pipeline, reminder check, pre-market earnings check registered")
    logger.info("Pre-market earnings check scheduled at 7:10 AM ET daily")
    scheduler.start()  # blocking — next_run_time only available after start
