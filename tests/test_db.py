import sqlite3
from pathlib import Path

import pytest

from clipvault import db

TABLES = {"streamers", "clips", "renders", "posts", "runs", "alerts", "settings"}


@pytest.fixture
def conn(home: Path):
    connection = db.open_db(home / "test.db")
    yield connection
    connection.close()


def test_wal_mode_and_foreign_keys(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_all_spec_tables_exist(conn: sqlite3.Connection) -> None:
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names


def test_migrate_is_idempotent(home: Path) -> None:
    conn = db.connect(home / "again.db")
    first = db.migrate(conn)
    assert first == [name for name, _ in db.available_migrations()]
    assert db.migrate(conn) == []
    conn.close()
    reopened = db.open_db(home / "again.db")
    assert reopened.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == len(first)
    reopened.close()


def test_streamer_defaults(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO streamers (login) VALUES ('somebody')")
    row = conn.execute("SELECT * FROM streamers").fetchone()
    assert (row["save_enabled"], row["post_enabled"], row["permission_type"]) == (1, 0, "none")
    assert (row["top_n"], row["vod_moments"]) == (5, 3)
    assert row["created_at"]


@pytest.mark.parametrize(
    ("ptype", "pref"),
    [("none", None), ("none", "https://example.test/c"), ("written", None), ("campaign", "  ")],
)
def test_posting_cannot_be_enabled_without_permission(
    conn: sqlite3.Connection, ptype: str, pref: str | None
) -> None:
    """Hard rule 1 at the database level."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO streamers (login, post_enabled, permission_type, permission_ref) "
            "VALUES ('x', 1, ?, ?)",
            (ptype, pref),
        )


def test_posting_can_be_enabled_with_permission(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO streamers (login, post_enabled, permission_type, permission_ref) "
        "VALUES ('ok', 1, 'written', 'DM on X, 2026-11-20')"
    )
    # ...and permission can't be removed while posting stays on.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE streamers SET permission_type = 'none' WHERE login = 'ok'")


def test_constraints_reject_bad_rows(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO streamers (login, permission_type) VALUES ('y', 'verbal')")
    with pytest.raises(sqlite3.IntegrityError):  # clip must belong to a streamer
        conn.execute("INSERT INTO clips (id, streamer_id, source) VALUES ('c1', 999, 'viewer')")


def test_failed_migration_rolls_back(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = [("0001_bad", "CREATE TABLE a (x); CREATE TABLE a (x);")]
    monkeypatch.setattr(db, "available_migrations", lambda: bad)
    conn = db.connect(home / "bad.db")
    with pytest.raises(sqlite3.Error):
        db.migrate(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a" not in names
    assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0
    conn.close()
