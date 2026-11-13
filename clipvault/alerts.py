"""Alerts: problems that need the user's attention (shown in the panel, sent by ntfy later).

Also holds the tiny key/value helpers for the ``settings`` table.
"""

from __future__ import annotations

import logging
import sqlite3

from clipvault.logging_setup import ctx
from clipvault.timeutil import to_rfc3339, utc_now

# Alert kinds (SPEC §14) plus 'downloader' for "yt-dlp may need an update".
KINDS = ("relogin", "storage", "broken", "campaign", "downloader", "library")

log = logging.getLogger(__name__)


def raise_alert(
    conn: sqlite3.Connection, kind: str, message: str, fix_url: str | None = None
) -> bool:
    """Open an alert unless the same one is already open; return True if it is new."""
    if kind not in KINDS:
        raise ValueError(f"unknown alert kind: {kind!r}")
    cursor = conn.execute(
        "INSERT INTO alerts (kind, message, fix_url, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT DO NOTHING",
        (kind, message, fix_url, to_rfc3339(utc_now())),
    )
    if cursor.rowcount:
        log.warning("alert raised: %s", message, extra=ctx(job="alerts", kind=kind))
    return bool(cursor.rowcount)


def resolve_alerts(conn: sqlite3.Connection, kind: str) -> int:
    """Close every open alert of ``kind``; return how many were closed."""
    cursor = conn.execute(
        "UPDATE alerts SET resolved_at = ? WHERE kind = ? AND resolved_at IS NULL",
        (to_rfc3339(utc_now()), kind),
    )
    return cursor.rowcount


def open_alerts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return open alerts, newest first."""
    return conn.execute(
        "SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY created_at DESC, id DESC"
    ).fetchall()


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Read a value from the ``settings`` table."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Write a value to the ``settings`` table (non-secret values only)."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
