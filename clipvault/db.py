"""SQLite connection (WAL mode) and a small forward-only migration runner.

How to use connections (the scheduler, the web panel and the CLI all share one file):

- Open one connection per thread with :func:`open_db`; never pass one between threads.
- A single statement commits on its own (autocommit).
- Group statements that must succeed or fail together in ``with transaction(conn):``.
- Never hold a transaction open across a network call or an FFmpeg run: do the slow
  work first, then write the result in one short transaction.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

_MIGRATION_NAME_RE = re.compile(r"^\d{4}_[a-z0-9_]+$")


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the database at ``path`` with the settings the app relies on.

    WAL journal (readers don't block the writer), foreign keys enforced, rows
    addressable by column name, autocommit, and a 5-second wait instead of an
    instant "database is locked" error.
    """
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run the block in one write transaction: all of it is saved, or none of it.

    Takes the write lock up front (``BEGIN IMMEDIATE``) so two writers queue
    instead of deadlocking. Transactions do not nest.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def available_migrations() -> list[tuple[str, str]]:
    """Return ``(name, sql)`` for every bundled migration, oldest first."""
    folder = resources.files("clipvault.migrations")
    found = []
    for entry in folder.iterdir():
        if entry.name.endswith(".sql"):
            name = entry.name[: -len(".sql")]
            if not _MIGRATION_NAME_RE.match(name):
                raise ValueError(f"bad migration file name: {entry.name}")
            found.append((name, entry.read_text(encoding="utf-8")))
    return sorted(found)


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply migrations that have not run yet; return the names applied.

    Each migration runs in its own transaction together with the row that
    records it, so a crash leaves the database at a known version. Migrations
    are never edited after they ship: schema changes get a new numbered file.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
    applied: list[str] = []
    for name, sql in available_migrations():
        if name in done:
            continue
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{sql}\n"
            "INSERT INTO schema_migrations (name, applied_at) "
            f"VALUES ('{name}', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            raise
        applied.append(name)
    return applied


def open_db(path: Path | str) -> sqlite3.Connection:
    """Open the database and bring its schema up to date."""
    conn = connect(path)
    migrate(conn)
    return conn
