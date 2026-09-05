"""APScheduler wiring: daily fetch at 07:00 Europe/Berlin, nightly backup at 02:00."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from desk.config import Settings, get_settings
from desk.digest import run_digest
from desk.jobs import (
    backup_sqlite,
    refresh_screener_universe,
    run_daily,
    run_fundamentals_weekly,
    scan_inbox,
)

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
    sched.add_job(
        run_fundamentals_weekly,
        CronTrigger(
            day_of_week=settings.fundamentals_weekday,
            hour=settings.fundamentals_hour,
            minute=0,
            timezone=settings.tz,
        ),
        id="fundamentals_weekly",
        name="Weekly fundamentals",
        replace_existing=True,
        misfire_grace_time=3600 * 6,
        coalesce=True,
        max_instances=1,
    )
    sched.add_job(
        refresh_screener_universe,
        CronTrigger(day=settings.screener_refresh_day, hour=5, minute=30, timezone=settings.tz),
        id="screener_refresh",
        name="Monthly screener constituents",
        replace_existing=True,
        misfire_grace_time=3600 * 12,
        coalesce=True,
        max_instances=1,
    )
    sched.add_job(
        run_digest,
        CronTrigger(
            day_of_week=settings.digest_weekday,
            hour=settings.digest_hour,
            minute=settings.digest_minute,
            timezone=settings.tz,
        ),
        id="weekly_digest",
        name="Weekly digest",
        replace_existing=True,
        misfire_grace_time=3600 * 6,
        coalesce=True,
        max_instances=1,
    )
    sched.add_job(
        scan_inbox,
        IntervalTrigger(minutes=settings.inbox_scan_minutes, timezone=settings.tz),
        id="inbox_scan",
        name="Inbox scan",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    return sched
