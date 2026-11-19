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
    forget(conn, now)
    return len(rows)


FORGET_VISITORS = timedelta(days=7)


def forget(conn: sqlite3.Connection, now=None) -> None:
    """Privacy upkeep: a run stops knowing which (hashed) address made it after 7 days
    (daily limits only need the last day), and ended sign-ins are deleted."""
    now = now or utc_now()
    conn.execute(
        "UPDATE runs SET visitor = NULL WHERE visitor IS NOT NULL AND created_at < ?",
        (to_rfc3339(now - FORGET_VISITORS),),
    )
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (to_rfc3339(now),))
    conn.execute("UPDATE sessions SET access_token = NULL, token_expires_at = NULL "
                 "WHERE token_expires_at <= ?", (to_rfc3339(now),))  # fmt: skip


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


# --- server settings, the download switch, opt-outs (W3) ------------------------------------

DOWNLOADS = "twitch_downloads"
FAILURES = "download_failures"
FAILURES_BEFORE_OFF = 5  # the downloader keeps failing: switch to Twitch's own players


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def downloads_on(conn: sqlite3.Connection, settings: WebSettings) -> bool:
    """May the server download other channels' videos right now? (The switch, plan section
    5 safeguard 6; it starts OFF unless CLIPVAULT_WEB_TWITCH_DOWNLOADS=on.)"""
    value = get_setting(conn, DOWNLOADS)
    if value is None:
        return settings.downloads_default
    return value.startswith("on")


def set_downloads(conn: sqlite3.Connection, on: bool, why: str = "") -> None:
    set_setting(conn, DOWNLOADS, ("on" if on else "off") + (f": {why}" if why else ""))
    if on:
        set_setting(conn, FAILURES, "0")


def note_download(conn: sqlite3.Connection, ok: bool) -> bool:
    """Count downloads that failed in a row; after ``FAILURES_BEFORE_OFF`` the switch goes
    off by itself (safeguard 9). Returns True when it was just switched off."""
    if ok:
        set_setting(conn, FAILURES, "0")
        return False
    conn.execute(  # one statement, so downloads failing at the same moment all count
        "INSERT INTO settings (key, value) VALUES (?, '1') ON CONFLICT (key) "
        "DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)",
        (FAILURES,),
    )
    failures = int(get_setting(conn, FAILURES, "0") or 0)
    if failures >= FAILURES_BEFORE_OFF and get_setting(conn, DOWNLOADS, "on").startswith("on"):
        set_downloads(conn, False, f"{failures} downloads failed in a row")
        return True
    return False


def salt(conn: sqlite3.Connection) -> str:
    """The secret mixed into visitor hashes; made once and kept, so daily limits survive a
    restart (``CLIPVAULT_WEB_SALT`` wins when set)."""
    import os

    value = os.environ.get("CLIPVAULT_WEB_SALT") or get_setting(conn, "salt")
    if not value:
        value = secrets.token_hex(16)
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('salt', ?)", (value,))
        value = get_setting(conn, "salt") or value
    return value


def opted_out(conn: sqlite3.Connection, user_id: str | None, login: str | None) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM optouts WHERE twitch_user_id = ? OR lower(login) = lower(?)",
            (user_id or "", login or ""),
        ).fetchone()
        is not None
    )


def opt_out(conn: sqlite3.Connection, settings: WebSettings, user_id: str, login: str,
            how: str) -> int:  # fmt: skip
    """Block a channel from now on and delete the files of every run made from it now;
    returns how many runs were removed."""
    from clipvault.timeutil import to_rfc3339 as stamp

    conn.execute(
        "INSERT INTO optouts (twitch_user_id, login, created_at, how) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (twitch_user_id) DO UPDATE SET login = excluded.login",
        (user_id, login.lower(), stamp(utc_now()), how),
    )
    rows = conn.execute(
        "SELECT id FROM runs WHERE twitch_user_id = ? AND status != 'expired'", (user_id,)
    ).fetchall()
    runs = [r["id"] for r in rows]
    for run_id in runs:
        remove_run(conn, settings, run_id)
    return len(runs)


def remove_run(conn: sqlite3.Connection, settings: WebSettings, run_id: str) -> None:
    """Delete a run's files now (a takedown, an opt-out) and mark it expired."""
    folder = run_dir(settings, run_id)
    if folder.is_dir() and folder.resolve().is_relative_to(settings.runs_dir.resolve()):
        shutil.rmtree(folder, ignore_errors=True)
    conn.execute(
        "UPDATE runs SET status = 'expired', source_path = NULL, expires_at = ? WHERE id = ?",
        (to_rfc3339(utc_now()), run_id),
    )
    conn.execute(
        "UPDATE run_clips SET path = NULL, thumb_path = NULL, source_path = NULL "
        "WHERE run_id = ?", (run_id,))  # fmt: skip
    conn.execute("DELETE FROM grants WHERE run_id = ?", (run_id,))


def is_blocked(conn: sqlite3.Connection, visitor: str | None) -> bool:
    return (
        bool(visitor)
        and conn.execute("SELECT 1 FROM blocked WHERE visitor = ?", (visitor,)).fetchone()
        is not None
    )
