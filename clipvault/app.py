"""Runs ClipVault unattended: the scheduler, the control panel and (``tray.py``) the tray icon.

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
# How often the updates step looks at its state; it asks GitHub only every 6 hours.
UPDATES_EVERY = timedelta(minutes=30)
# A heartbeat this late means the computer was asleep (or the process was frozen).
WAKE_GAP = timedelta(minutes=3)
# A daily step that failed is tried again after this long, not on every heartbeat.
DAILY_RETRY = timedelta(minutes=30)
LOCK_NAME = "clipvault.lock"


class AlreadyRunning(Exception):
    """Another ClipVault is already running from the same folder."""


def port_in_use(port: int) -> bool:
    """True when some program already answers on 127.0.0.1:``port``."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def choose_port(wanted: int) -> int:
    """``wanted``, or the next free port when another program (not ClipVault) already uses it.

    Without this, a second web app on the same port can silently answer the app window's
    requests (it connects to 127.0.0.1) and show that program's pages instead of ClipVault's.
    """
    for port in range(wanted, wanted + 20):
        if not port_in_use(port):
            return port
    return wanted


class PanelServer:
    """The control panel served by uvicorn on a background thread."""

    def __init__(self, config: cfg.Config, home: Path | None = None) -> None:
        import uvicorn

        from clipvault.web.server import create_app

        self.config = config
        self.port = choose_port(config.panel.port)
        if self.port != config.panel.port:
            log.warning("port %s is used by another program; the control panel uses %s instead",
                        config.panel.port, self.port, extra=ctx(job="panel"))  # fmt: skip
        self.server = uvicorn.Server(
            uvicorn.Config(
                create_app(home),
                host=config.panel.host,
                port=self.port,
                log_level="warning",
                access_log=False,
                lifespan="off",
            )
        )
        self.thread = threading.Thread(target=self._serve, name="panel", daemon=True)

    def _serve(self) -> None:
        import asyncio

        # Windows' default (proactor) event loop logs a traceback every time a browser
        # drops a connection; the selector loop doesn't, and is plenty for one user.
        factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        with asyncio.Runner(loop_factory=factory) as runner:
            runner.run(self.server.serve())

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 10.0) -> bool:
        """Start serving; False when the server could not start (port taken)."""
        self.thread.start()
        waited = 0.0
        while not self.server.started and self.thread.is_alive() and waited < timeout:
            threading.Event().wait(0.1)
            waited += 0.1
        ok = bool(self.server.started)
        if ok:
            log.info("control panel started", extra=ctx(job="panel", host=self.config.panel.host,
                                                          port=self.port))  # fmt: skip
            try:
                (cfg.home_dir() / cfg.PANEL_PORT_FILE).write_text(str(self.port), encoding="utf-8")
            except OSError:
                pass
        return ok

    def stop(self) -> None:
        try:
            (cfg.home_dir() / cfg.PANEL_PORT_FILE).unlink()
        except OSError:
            pass
        self.server.should_exit = True
        self.thread.join(timeout=10)


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

    def __init__(self, config: cfg.Config, watch_live: bool = True) -> None:
        from apscheduler.executors.pool import ThreadPoolExecutor
        from apscheduler.schedulers.background import BackgroundScheduler

        self.config = config
        self.watch_live = watch_live
        self._live_stop = threading.Event()
        self._live_thread: threading.Thread | None = None
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
            scheduler.run_step(name, scheduler.STEP_FUNCTIONS[name], self._current_config())
        finally:
            lock.release()
            self._note_next(name)

    def _current_config(self) -> cfg.Config:
        """Settings as saved now, so a change in the panel counts from the next round.

        (How often each step runs is fixed when the app starts.)
        """
        try:
            return cfg.load_config()
        except cfg.ConfigError as exc:
            log.warning("config.yaml can't be read; using the settings from start: %s", exc,
                        extra=ctx(job="app"))  # fmt: skip
            return self.config

    def _note_next(self, name: str) -> None:
        from clipvault import activity

        job = self.scheduler.get_job(name)
        when = job.next_run_time if job is not None else None
        activity.next_run(name, when.timestamp() if when else None)

    def _cut_when_due(self, due: datetime) -> None:
        """A live moment can be cut at ``due``: run the download step then, not up to
        two minutes later."""
        self.scheduler.add_job(self._run, "date", run_date=due + timedelta(seconds=5),
                               args=["worker"], id=f"cut:{due.timestamp():.0f}",
                               replace_existing=True)  # fmt: skip

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
        every(self._run, "interval", args=["updates"], id="updates",
              next_run_time=now + timedelta(minutes=1),
              seconds=UPDATES_EVERY.total_seconds())  # fmt: skip
        every(self._run, "interval", args=["posting"], id="posting",
              next_run_time=now + timedelta(seconds=90),
              seconds=60)  # fmt: skip
        every(self.heartbeat.tick, "interval", id="heartbeat",
              next_run_time=now + timedelta(seconds=10),
              seconds=TICK_EVERY.total_seconds())  # fmt: skip
        self.scheduler.start()
        self._note_next("discover")
        log.info("scheduler started", extra=ctx(job="app", pid=os.getpid()))
        if self.watch_live:
            from clipvault.live import LiveWatcher

            self._live_stop = threading.Event()
            watcher = LiveWatcher(self.config, on_moment=self._cut_when_due)
            self._live_thread = threading.Thread(
                target=watcher.run, args=(self._live_stop,), name="live-chat", daemon=True
            )
            self._live_thread.start()

    def stop(self) -> None:
        from clipvault import lifecycle

        lifecycle.stopping.set()  # running loops stop after the clip they are on
        if self._live_thread is not None:
            self._live_stop.set()
            self._live_thread.join(timeout=15)
        self.scheduler.shutdown(wait=True)
        log.info("scheduler stopped", extra=ctx(job="app"))
