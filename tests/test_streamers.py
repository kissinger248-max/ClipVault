from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from clipvault import db
from clipvault.streamers import (
    StreamerEntry,
    StreamersFileError,
    import_streamers,
    normalize_login,
    parse_streamers_text,
    refresh_profiles,
)
from clipvault.twitch.api import TwitchClient
from clipvault.twitch.auth import AppTokenProvider
from tests.twitch_fake import FakeTwitch

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def client_for(fake: FakeTwitch) -> TwitchClient:
    http = fake.client()
    tokens = AppTokenProvider("cid", "secret", http)
    return TwitchClient("cid", tokens, http, sleep=lambda s: None)


@pytest.mark.parametrize(
    ("text", "login"),
    [
        ("Shroud", "shroud"),
        ("  @some_one ", "some_one"),
        ("https://www.twitch.tv/SomeOne/", "someone"),
        ("twitch.tv/abc123", "abc123"),
    ],
)
def test_normalize_login(text: str, login: str) -> None:
    assert normalize_login(text) == login


@pytest.mark.parametrize("text", ["", "has space", "way_too_long_for_a_twitch_login_x", "bad!"])
def test_normalize_login_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        normalize_login(text)


def test_parse_file_with_comments_urls_and_settings() -> None:
    text = """
    # my list
    Alpha
    beta top_n=10   # inline comment
    https://twitch.tv/gamma min_views=500 vod_moments=0 save=off
    delta min_views=none
    """
    assert parse_streamers_text(text) == [
        StreamerEntry("alpha"),
        StreamerEntry("beta", {"top_n": 10}),
        StreamerEntry("gamma", {"min_views": 500, "vod_moments": 0, "save_enabled": 0}),
        StreamerEntry("delta", {"min_views": None}),
    ]


def test_parse_reports_every_bad_line() -> None:
    text = "ok\nbad login\nalpha top_n=0\nbeta speed=fast\nok\ngamma post=on\ndelta top_n"
    with pytest.raises(StreamersFileError) as info:
        parse_streamers_text(text)
    message = str(info.value)
    assert "line 2:" in message and "key=value" in message
    assert "line 3:" in message and "top_n" in message
    assert "line 4:" in message and "unknown setting" in message
    assert "line 5:" in message and "listed twice" in message
    assert "line 6:" in message and "control panel" in message
    assert "line 7:" in message


def test_import_adds_updates_and_is_idempotent(conn: sqlite3.Connection) -> None:
    entries = parse_streamers_text("alpha\nbeta top_n=10 save=off\n")
    with db.transaction(conn):
        first = import_streamers(conn, entries)
    assert first.added == ("alpha", "beta")
    beta = conn.execute("SELECT * FROM streamers WHERE login = 'beta'").fetchone()
    assert (beta["top_n"], beta["save_enabled"], beta["post_enabled"]) == (10, 0, 0)

    again = import_streamers(conn, entries)
    assert (again.added, again.updated, again.unchanged) == ((), (), ("alpha", "beta"))

    changed = import_streamers(conn, parse_streamers_text("beta top_n=3\ngamma\n"))
    assert (changed.added, changed.updated) == (("gamma",), ("beta",))
    beta = conn.execute("SELECT * FROM streamers WHERE login = 'beta'").fetchone()
    assert (beta["top_n"], beta["save_enabled"]) == (3, 0)  # unlisted settings untouched
    assert conn.execute("SELECT count(*) FROM streamers").fetchone()[0] == 3  # alpha kept


def test_profiles_are_looked_up_once_and_cached(conn: sqlite3.Connection) -> None:
    fake = FakeTwitch()
    fake.add_user("1001", "alpha", "AlphaStreamer")
    import_streamers(conn, parse_streamers_text("alpha\nghost\n"))
    client = client_for(fake)

    assert refresh_profiles(conn, client, NOW) == ["ghost"]
    alpha = conn.execute("SELECT * FROM streamers WHERE login = 'alpha'").fetchone()
    assert alpha["twitch_user_id"] == "1001"
    assert alpha["display_name"] == "AlphaStreamer"
    assert alpha["avatar_url"] == "https://example.test/alpha.png"

    refresh_profiles(conn, client, NOW + timedelta(days=1))
    lookups = fake.calls("/helix/users")
    assert len(lookups) == 2
    assert lookups[1].url.query == b"login=ghost"  # alpha is cached; only ghost is retried


def test_stale_profile_is_refreshed_by_id_and_follows_a_rename(conn: sqlite3.Connection) -> None:
    fake = FakeTwitch()
    fake.add_user("1001", "alpha")
    import_streamers(conn, parse_streamers_text("alpha\n"))
    client = client_for(fake)
    refresh_profiles(conn, client, NOW)

    fake.users[0].update(login="alpha_new", display_name="Alpha New")
    assert refresh_profiles(conn, client, NOW + timedelta(days=8)) == []
    row = conn.execute("SELECT * FROM streamers").fetchone()
    assert (row["login"], row["display_name"]) == ("alpha_new", "Alpha New")
    assert fake.calls("/helix/users")[-1].url.query == b"id=1001"
