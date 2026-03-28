"""APScheduler daemon — runs the pipeline daily at 5AM Eastern."""
from __future__ import annotations

import signal
import sys

from loguru import logger


def _run_daily_pipeline() -> None:
    from src.pipeline.main_pipeline import run_pipeline
    try:
        run_pipeline()
    except Exception as e:
        logger.exception(f"Pipeline failed: {e}")


def start_scheduler() -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from src.config import settings

    # Parse "HH:MM" from settings
    hour, minute = settings.pipeline_start_time.split(":")

    scheduler = BlockingScheduler(timezone=settings.newsletter_timezone)
    scheduler.add_job(
        _run_daily_pipeline,
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
