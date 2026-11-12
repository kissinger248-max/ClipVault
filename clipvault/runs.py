"""Job bookkeeping in the ``runs`` table: when a job last ran and where it got to.

A job's ``cursor`` is the point it has fully processed up to. On start a job
continues from its cursor, which is how runs missed while a laptop slept are
caught up. These helpers issue single statements, so they can be called on their
own or inside ``db.transaction``.
"""

from __future__ import annotations

import sqlite3

from clipvault.timeutil import to_rfc3339, utc_now


def get_run(conn: sqlite3.Connection, job: str) -> sqlite3.Row | None:
    """Return the ``runs`` row for ``job``, or ``None`` if it has never run."""
    return conn.execute("SELECT * FROM runs WHERE job = ?", (job,)).fetchone()


def get_cursor(conn: sqlite3.Connection, job: str) -> str | None:
    """Return the stored cursor for ``job``, or ``None``."""
    row = get_run(conn, job)
    return row["cursor"] if row else None


def mark_started(conn: sqlite3.Connection, job: str) -> None:
    """Record that ``job`` started now."""
    conn.execute(
        "INSERT INTO runs (job, last_started) VALUES (?, ?) "
        "ON CONFLICT(job) DO UPDATE SET last_started = excluded.last_started",
        (job, to_rfc3339(utc_now())),
    )


def mark_success(conn: sqlite3.Connection, job: str, cursor: str | None = None) -> None:
    """Record that ``job`` succeeded now; store ``cursor`` if one is given."""
    conn.execute(
        "INSERT INTO runs (job, last_success, cursor, last_error) VALUES (?, ?, ?, NULL) "
        "ON CONFLICT(job) DO UPDATE SET last_success = excluded.last_success, "
        "cursor = COALESCE(excluded.cursor, runs.cursor), last_error = NULL",
        (job, to_rfc3339(utc_now()), cursor),
    )


def mark_error(conn: sqlite3.Connection, job: str, error: str) -> None:
    """Record that ``job`` failed; the cursor is left where it was."""
    conn.execute(
        "INSERT INTO runs (job, last_error) VALUES (?, ?) "
        "ON CONFLICT(job) DO UPDATE SET last_error = excluded.last_error",
        (job, error[:2000]),
    )
