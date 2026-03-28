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


def start_scheduler() -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from src.config import settings

    # Parse "HH:MM" from settings
    hour, minute = settings.pipeline_start_time.split(":")

    scheduler = BlockingScheduler(timezone=settings.newsletter_timezone)
    scheduler.add_job(
        _run_all_portfolios,
        CronTrigger(
            hour=int(hour),
            minute=int(minute),
            timezone=settings.newsletter_timezone,
        ),
        id="daily_pipeline",
        name="Financial Bytes Daily Pipeline",
        misfire_grace_time=300,  # 5-minute grace window
        coalesce=True,
    )

    def _shutdown(signum, frame):
        logger.info("Scheduler shutting down...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    next_run = scheduler.get_job("daily_pipeline").next_run_time
    logger.info(f"Scheduler started. Next run: {next_run}")
    scheduler.start()
