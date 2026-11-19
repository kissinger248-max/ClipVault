"""The website's database: runs and the clips made from them (SQLite, WAL).

One connection per thread, as in the app (``clipvault.db``). Run ids are the private
links, so they are long and random, and nothing ever lists runs to visitors.
"""

from __future__ import annotations

import re
import secrets
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

from clipvault import db
from clipvault.timeutil import parse_rfc3339, to_rfc3339, utc_now

from .settings import WebSettings

MIGRATIONS = "clipvault_web.migrations"
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{20,40}$")
WORKING = ("finding", "making")
MAX_ATTEMPTS = 3


def open_store(settings: WebSettings) -> sqlite3.Connection:
    """Open (and migrate) the website's database."""
    return db.open_db(settings.db_path, MIGRATIONS)


def new_run_id() -> str:
    return secrets.token_urlsafe(18)  # 24 characters, 144 random bits


def run_dir(settings: WebSettings, run_id: str) -> Path:
    """The folder holding every file of one run (checked: never outside ``runs/``)."""
    if not RUN_ID.match(run_id):
        raise ValueError("not a run id")
    return settings.runs_dir / run_id


def relpath(settings: WebSettings, path: Path) -> str:
    return path.resolve().relative_to(settings.data).as_posix()


def resolve(settings: WebSettings, rel: str | None) -> Path | None:
    """A file named in the database, if it is still there (and inside the data folder)."""
    if not rel:
        return None
    path = (settings.data / rel).resolve()
    if not path.is_relative_to(settings.data) or not path.is_file():
        return None
    return path


def create_run(
    conn: sqlite3.Connection,
    settings: WebSettings,
    *,
    run_id: str,
    kind: str,
    source_name: str,
    source: Path,
    duration: float,
    width: int,
    height: int,
    name: str | None,
    visitor: str | None,
) -> None:
    """Record a run whose source file is already in its folder; it waits for a worker."""
    now = utc_now()
    conn.execute(
        "INSERT INTO runs (id, kind, status, source_name, source_path, duration, width, height, "
        "name, visitor, step, created_at, expires_at) "
        "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, 'Waiting for its turn', ?, ?)",
        (run_id, kind, source_name, relpath(settings, source), duration, width, height,
         name or None, visitor, to_rfc3339(now),
         to_rfc3339(now + timedelta(hours=settings.keep_hours))),
    )  # fmt: skip


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    if not RUN_ID.match(run_id or ""):
        return None
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def clips(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM run_clips WHERE run_id = ? ORDER BY n", (run_id,)).fetchall()


def set_step(
    conn: sqlite3.Connection, run_id: str, step: str, done: int = 0, total: int = 0
) -> None:
    conn.execute(
        "UPDATE runs SET step = ?, step_done = ?, step_total = ? WHERE id = ?",
        (step, done, total, run_id),
    )


def claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Take the oldest waiting run (one transaction, so two workers never take the same)."""
    with db.transaction(conn):
        row = conn.execute(
            "SELECT id FROM runs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE runs SET status = 'finding', attempts = attempts + 1, "
            "started_at = coalesce(started_at, ?) WHERE id = ?",
            (to_rfc3339(utc_now()), row["id"]),
        )
    return get_run(conn, row["id"])


def requeue_interrupted(conn: sqlite3.Connection) -> int:
    """After a restart: runs that were being worked on go back in the queue (they pick up
    where they stopped), unless they already failed too often."""
    conn.execute(
        "UPDATE runs SET status = 'failed', error = 'It stopped too many times.', "
        "finished_at = ? WHERE status IN ('finding', 'making') AND attempts >= ?",
        (to_rfc3339(utc_now()), MAX_ATTEMPTS),
    )
    return conn.execute(
        "UPDATE runs SET status = 'queued' WHERE status IN ('finding', 'making')"
    ).rowcount


def queue_position(conn: sqlite3.Connection, run: sqlite3.Row) -> int:
    """How many runs are ahead of this one (0 when it is being made)."""
    if run["status"] != "queued":
        return 0
    return conn.execute(
        "SELECT count(*) FROM runs WHERE status = 'queued' AND created_at < ?",
        (run["created_at"],),
    ).fetchone()[0]


def expire(conn: sqlite3.Connection, settings: WebSettings, now=None) -> int:
    """Delete every file of runs past their expiry and mark them; returns how many.

    Only each run's own folder under ``runs/`` is deleted (files the website made,
    hard rule 8). The rows stay (without files), so an old link says "expired".
    """
    now = now or utc_now()
    rows = conn.execute(
        "SELECT id FROM runs WHERE status != 'expired' AND expires_at <= ?", (to_rfc3339(now),)
    ).fetchall()
    for row in rows:
        folder = run_dir(settings, row["id"])
        if folder.is_dir() and folder.resolve().is_relative_to(settings.runs_dir.resolve()):
            shutil.rmtree(folder, ignore_errors=True)
        conn.execute(
            "UPDATE runs SET status = 'expired', source_path = NULL WHERE id = ?", (row["id"],)
        )
        conn.execute("UPDATE run_clips SET path = NULL, thumb_path = NULL WHERE run_id = ?",
                     (row["id"],))  # fmt: skip
    return len(rows)


def clear_incoming(settings: WebSettings, older_than_s: float = 3600, now=None) -> int:
    """Remove uploads that never finished (a closed tab, a crash); returns how many."""
    folder = settings.incoming_dir
    if not folder.is_dir():
        return 0
    cutoff = (now or utc_now()).timestamp() - older_than_s
    removed = 0
    for path in folder.glob("*.part"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def expires_in_words(run: sqlite3.Row) -> str:
    """'in 23 hours' / 'in 40 minutes' until the run's files are deleted."""
    left = (parse_rfc3339(run["expires_at"]) - utc_now()).total_seconds()
    if left <= 0:
        return "now"
    if left >= 5400:
        return f"in {round(left / 3600)} hours"
    return f"in {max(round(left / 60), 1)} minutes"
