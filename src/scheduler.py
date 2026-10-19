"""
Scheduling.

Recommended (production): Windows Task Scheduler runs `scripts\\run_pipeline.bat`
every day at SCHEDULE_TIME. Register it with `scripts\\register_task.bat`
(see README). The task runs whether or not you are logged in, and survives reboots.

Alternative: `python -m src.main --daemon` keeps a process alive and runs the
pipeline every day at SCHEDULE_TIME (in TIMEZONE). Only use this if you cannot
use Task Scheduler — if the process or PC stops, the schedule stops too.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Settings

log = logging.getLogger("exch.scheduler")


def next_run_at(settings: Settings, now: datetime | None = None) -> datetime:
    tz = ZoneInfo(settings.timezone)
    now = now or datetime.now(tz)
    hh, mm = (int(x) for x in settings.schedule_time.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def run_daemon(settings: Settings, job: Callable[[], int]) -> None:
    """Run `job` every day at SCHEDULE_TIME. Never exits on job errors."""
    log.info("Daemon mode started — daily at %s (%s). Press Ctrl+C to stop.",
             settings.schedule_time, settings.timezone)
    while True:
        nxt = next_run_at(settings)
        log.info("Next run at %s", nxt.strftime("%Y-%m-%d %H:%M %Z"))
        while True:
            remaining = (nxt - datetime.now(ZoneInfo(settings.timezone))).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 300))  # wake up periodically (handles sleep/hibernate)
        try:
            code = job()
            log.info("Scheduled run finished with exit code %s", code)
        except Exception:
            log.exception("Scheduled run crashed — will try again tomorrow")
        time.sleep(61)  # never double-fire within the same minute


def task_scheduler_hint(settings: Settings, project_root: str) -> str:
    return (
        "To register the daily Windows task, open Command Prompt *as Administrator* and run:\n\n"
        f'  cd "{project_root}"\n'
        f"  scripts\\register_task.bat {settings.schedule_time}\n"
    )
