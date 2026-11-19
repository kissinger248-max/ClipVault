import sqlite3
from pathlib import Path

import pytest

from clipvault import db

TABLES = {"streamers", "clips", "renders", "posts", "runs", "alerts", "settings", "games"}


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
    monkeypatch.setattr(db, "available_migrations", lambda *_: bad)
    conn = db.connect(home / "bad.db")
    with pytest.raises(sqlite3.Error):
        db.migrate(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a" not in names
    assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0
    conn.close()


def test_transaction_commits_all_or_nothing(conn: sqlite3.Connection, home: Path) -> None:
    with db.transaction(conn):
        conn.execute("INSERT INTO streamers (login) VALUES ('kept')")
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            conn.execute("INSERT INTO streamers (login) VALUES ('lost')")
            raise RuntimeError("crash mid-way")
    assert not conn.in_transaction
    other = db.connect(home / "clipvault.db")  # a second connection sees only committed rows
    assert [r["login"] for r in other.execute("SELECT login FROM streamers")] == ["kept"]
    other.close()


def test_single_statements_commit_on_their_own(conn: sqlite3.Connection, home: Path) -> None:
    conn.execute("INSERT INTO streamers (login) VALUES ('solo')")
    other = db.connect(home / "clipvault.db")
    assert other.execute("SELECT count(*) FROM streamers").fetchone()[0] == 1
    other.close()


def test_clip_updates_touch_updated_at(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO streamers (login) VALUES ('s')")
    conn.execute(
        "INSERT INTO clips (id, streamer_id, source, updated_at) "
        "VALUES ('c1', 1, 'viewer', '2000-01-01T00:00:00Z')"
    )
    conn.execute("UPDATE clips SET view_count = 5 WHERE id = 'c1'")
    assert conn.execute("SELECT updated_at FROM clips").fetchone()[0] > "2026"
    conn.execute("UPDATE clips SET updated_at = '2001-01-01T00:00:00Z' WHERE id = 'c1'")
    assert conn.execute("SELECT updated_at FROM clips").fetchone()[0] == "2001-01-01T00:00:00Z"
