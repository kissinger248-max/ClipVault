"""Background threads: make waiting runs one after another, and delete expired files.

On start, runs that were being made when the server stopped go back in the queue and
continue where they stopped (``store.requeue_interrupted``).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from clipvault.logging_setup import ctx

from . import making, store
from .settings import WebSettings

POLL_S = 2.0
SWEEP_S = 600.0  # expiry check every 10 minutes
AFTER_ERROR_S = 5.0

log = logging.getLogger(__name__)


class Worker:
    """Makes runs in the background while the website serves pages."""

    def __init__(self, settings: WebSettings, make: Callable[..., str] = making.make_run) -> None:
        self.settings = settings
        self._make = make
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        conn = store.open_store(self.settings)
        try:
            back = store.requeue_interrupted(conn)
        finally:
            conn.close()
        if back:
            log.info("runs resumed after a restart", extra=ctx(job="web", count=back))
        for i in range(self.settings.workers):
            self._spawn(self._work, f"clipvault-web-maker-{i}")
        self._spawn(self._sweep, "clipvault-web-expiry")

    def _spawn(self, target: Callable[[], None], name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def wake(self) -> None:
        """A new run is waiting: don't wait for the next poll."""
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout)

    def run_once(self) -> bool:
        """Make the oldest waiting run, if any; True when one was made (or tried)."""
        conn = store.open_store(self.settings)
        try:
            run = store.claim_next(conn)
        finally:
            conn.close()
        if run is None:
            return False
        try:
            self._make(self.settings, run["id"])
        except Exception as exc:  # noqa: BLE001 - one bad run must never stop the worker
            log.exception("run crashed: %s", exc, extra=ctx(job="web", run=run["id"]))
            conn = store.open_store(self.settings)
            try:
                retry = run["attempts"] < store.MAX_ATTEMPTS
                conn.execute(
                    "UPDATE runs SET status = ?, error = ? WHERE id = ?",
                    ("queued" if retry else "failed",
                     None if retry else "Something went wrong while making your clips.",
                     run["id"]),
                )  # fmt: skip
            finally:
                conn.close()
            self._stop.wait(AFTER_ERROR_S)
        return True

    def _work(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._wake.wait(POLL_S)
                self._wake.clear()

    def sweep(self) -> int:
        """Delete expired runs' files and abandoned uploads; returns runs expired."""
        conn = store.open_store(self.settings)
        try:
            expired = store.expire(conn, self.settings)
        finally:
            conn.close()
        abandoned = store.clear_incoming(self.settings)
        if expired or abandoned:
            log.info("expired files deleted",
                     extra=ctx(job="web", runs=expired, uploads=abandoned))  # fmt: skip
        return expired

    def _sweep(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception as exc:  # noqa: BLE001 - try again next time
                log.exception("expiry sweep failed: %s", exc, extra=ctx(job="web"))
            self._stop.wait(SWEEP_S)
