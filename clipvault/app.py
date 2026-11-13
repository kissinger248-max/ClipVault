"""Runs ClipVault unattended: the scheduler now, the web panel (M5) and tray icon later.

Timing (SPEC §4):

- discover: every ``discover_interval_min`` minutes, right at start, and right after waking
- worker: every 2 minutes (downloads, then transcription on mains power)
- vods: every hour (new finished streams; their chat is searched for moments)
- notify: every minute
- heartbeat: every minute; runs the daily steps when due (cleanup 04:00, summary
  at ``notifications.summary_time``) and notices when the computer slept
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO
from zoneinfo import ZoneInfo

from clipvault import config as cfg
from clipvault import db, scheduler
from clipvault.logging_setup import ctx
from clipvault.timeutil import utc_now

log = logging.getLogger(__name__)

WORKER_EVERY = timedelta(minutes=2)
VODS_EVERY = timedelta(minutes=60)
TICK_EVERY = timedelta(minutes=1)
# A heartbeat this late means the computer was asleep (or the process was frozen).
WAKE_GAP = timedelta(minutes=3)
# A daily step that failed is tried again after this long, not on every heartbeat.
DAILY_RETRY = timedelta(minutes=30)
LOCK_NAME = "clipvault.lock"


class AlreadyRunning(Exception):
    """Another ClipVault is already running from the same folder."""


@contextmanager
def single_instance(home: Path) -> Iterator[None]:
    """Hold a lock file for as long as the app runs; two copies would duplicate work."""
    path = home / LOCK_NAME
    handle: IO[str] = open(path, "a+")  # noqa: SIM115 - held open for the whole run
    try:
        try:
            _lock(handle)
        except OSError as exc:
            raise AlreadyRunning(f"ClipVault is already running in {home}") from exc
        yield
    finally:
        handle.close()


def _lock(handle: IO[str]) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class Heartbeat:
    """Notices sleep from gaps between ticks, and runs daily steps when they are due."""

    def __init__(
        self,
        config: cfg.Config,
        run: Callable[[str], object],
        on_wake: Callable[[timedelta], None],
        db_path: Path | None = None,
    ) -> None:
        self.config = config
        self.run = run
        self.on_wake = on_wake
        self.db_path = db_path
        self.last_tick: datetime | None = None
        self.attempted: dict[str, datetime] = {}

    def tick(self, now: datetime | None = None) -> list[str]:
        """One heartbeat; returns the daily steps it started."""
        now = now or utc_now()
        if self.last_tick is not None and now - self.last_tick > WAKE_GAP:
            gap = now - self.last_tick
            log.info("woke up after %s", gap, extra=ctx(job="heartbeat"))
            self.on_wake(gap)
        self.last_tick = now
        tz = ZoneInfo(self.config.timezone)
        daily = {"cleanup": scheduler.CLEANUP_AT, "summary": self.config.notifications.summary_time}
        conn = db.open_db(self.db_path or cfg.db_path())
        try:
            due = [
                name for name, at in daily.items() if scheduler.daily_due(conn, name, at, tz, now)
            ]
        finally:
            conn.close()
        due = [
            n
            for n in due
            if now - self.attempted.get(n, datetime.min.replace(tzinfo=now.tzinfo)) >= DAILY_RETRY
        ]
        for name in due:
            self.attempted[name] = now
            self.run(name)
        return due


class App:
    """The scheduler and its jobs. ``start`` returns at once; ``stop`` waits for running jobs."""

    def __init__(self, config: cfg.Config) -> None:
        from apscheduler.executors.pool import ThreadPoolExecutor
        from apscheduler.schedulers.background import BackgroundScheduler

        self.config = config
        self.scheduler = BackgroundScheduler(
            timezone=ZoneInfo(config.timezone),
            executors={"default": ThreadPoolExecutor(4)},
            # Missed runs (asleep) collapse into one; a job never overlaps itself.
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": None},
        )
        self.heartbeat = Heartbeat(config, self._run_now, self._woke)
        # Daily steps and on-demand runs share these locks with the scheduled jobs.
        self._locks = {name: threading.Lock() for name in scheduler.STEPS}

    def _run(self, name: str) -> None:
        lock = self._locks[name]
        if not lock.acquire(blocking=False):
            return  # already running
        try:
            scheduler.run_step(name, scheduler.STEP_FUNCTIONS[name], self.config)
        finally:
            lock.release()

    def _run_now(self, name: str) -> None:
        """Start a step in the background as soon as possible."""
        self.scheduler.add_job(self._run, args=[name], id=f"now:{name}", replace_existing=True)

    def _woke(self, gap: timedelta) -> None:
        for name in ("discover", "worker"):
            self._run_now(name)

    def start(self) -> None:
        now = datetime.now(ZoneInfo(self.config.timezone))
        every = self.scheduler.add_job
        every(self._run, "interval", args=["discover"], id="discover", next_run_time=now,
              minutes=self.config.discover_interval_min)  # fmt: skip
        every(self._run, "interval", args=["worker"], id="worker",
              next_run_time=now + timedelta(seconds=20),
              seconds=WORKER_EVERY.total_seconds())  # fmt: skip
        every(self._run, "interval", args=["vods"], id="vods",
              next_run_time=now + timedelta(minutes=2),
              seconds=VODS_EVERY.total_seconds())  # fmt: skip
        every(self._run, "interval", args=["notify"], id="notify",
              next_run_time=now + timedelta(seconds=5),
              seconds=TICK_EVERY.total_seconds())  # fmt: skip
        every(self.heartbeat.tick, "interval", id="heartbeat",
              next_run_time=now + timedelta(seconds=10),
              seconds=TICK_EVERY.total_seconds())  # fmt: skip
        self.scheduler.start()
        log.info("scheduler started", extra=ctx(job="app", pid=os.getpid()))

    def stop(self) -> None:
        self.scheduler.shutdown(wait=True)
        log.info("scheduler stopped", extra=ctx(job="app"))
