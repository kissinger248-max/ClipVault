"""Counting what happens on the website, without cookies or anything about the visitor.

Each event adds one to a counter for that day (``events`` table): how many runs started,
how many clips were downloaded, how often each nudge toward the app was shown and
clicked. That's enough to see whether the website brings people to the app
(docs/WEB_VERSION_PLAN.md section 7.4), and there's nothing personal to protect.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from clipvault.timeutil import utc_now

# The events a page may report itself (everything else is counted by the server).
FROM_PAGES = {"nudge_shown"}
NUDGES = {"home", "working", "more", "live", "limit", "embed"}
LINKS = NUDGES | {"header", "footer"}  # where a "get the app" link can be
MAX_DETAIL = 40


def count(conn: sqlite3.Connection, name: str, detail: str = "") -> None:
    """Add one to today's counter for ``name`` (and ``detail``)."""
    conn.execute(
        "INSERT INTO events (day, name, detail, count) VALUES (?, ?, ?, 1) "
        "ON CONFLICT (day, name, detail) DO UPDATE SET count = count + 1",
        (utc_now().strftime("%Y-%m-%d"), name, (detail or "")[:MAX_DETAIL]),
    )


def summary(conn: sqlite3.Connection, days: int = 7) -> list[tuple[str, str, int]]:
    """``(name, detail, total)`` over the last ``days`` days, biggest first."""
    since = (utc_now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return [
        (row[0], row[1], row[2])
        for row in conn.execute(
            "SELECT name, detail, sum(count) FROM events WHERE day >= ? "
            "GROUP BY name, detail ORDER BY name, sum(count) DESC",
            (since,),
        )
    ]
