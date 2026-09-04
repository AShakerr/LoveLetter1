"""APScheduler wiring: daily fetch at 07:00 Europe/Berlin, nightly backup at 02:00."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from desk.config import Settings, get_settings
from desk.jobs import backup_sqlite, run_daily

log = logging.getLogger(__name__)


def build_scheduler(settings: Settings | None = None) -> BackgroundScheduler:
    settings = settings or get_settings()
    sched = BackgroundScheduler(timezone=settings.tz)
    sched.add_job(
        run_daily,
        CronTrigger(
            hour=settings.daily_job_hour, minute=settings.daily_job_minute, timezone=settings.tz
        ),
        id="daily_fetch",
        name="Daily fetch",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    sched.add_job(
        backup_sqlite,
        CronTrigger(hour=settings.backup_hour, minute=0, timezone=settings.tz),
        id="nightly_backup",
        name="Nightly SQLite backup",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    return sched
