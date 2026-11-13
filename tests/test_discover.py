from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from clipvault import runs
from clipvault.config import Config
from clipvault.discover.viewer_clips import (
    day_windows,
    discover_all,
    parse_clip,
    select_clips,
)
from clipvault.streamers import import_streamers, parse_streamers_text
from clipvault.twitch.api import TwitchClient
from clipvault.twitch.auth import AppTokenProvider, TwitchAuthError
from tests.twitch_fake import FakeTwitch, load_fixture, make_clip

CHICAGO = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)  # 12:00 in Chicago
CONFIG = Config()  # timezone America/Chicago


def clip(clip_id: str, created_at: str, views: int):
    return parse_clip(make_clip(clip_id, "1", created_at, views))


def client_for(fake: FakeTwitch) -> TwitchClient:
    http = fake.client()
    return TwitchClient("cid", AppTokenProvider("cid", "secret", http), http, sleep=lambda s: None)


@pytest.fixture
def fake() -> FakeTwitch:
    twitch = FakeTwitch()
    twitch.add_user("1001", "alpha")
    twitch.add_user("1002", "beta")
    twitch.games = {"33214": "Fortnite"}
    return twitch


def setup_streamers(conn: sqlite3.Connection, text: str = "alpha top_n=2\nbeta top_n=2\n") -> None:
    import_streamers(conn, parse_streamers_text(text))


def clip_ids(conn: sqlite3.Connection) -> list[str]:
    return [r["id"] for r in conn.execute("SELECT id FROM clips ORDER BY id")]


# --- pure functions ------------------------------------------------------------


def test_parse_clip_from_recorded_fixture() -> None:
    first, second, _ = (parse_clip(raw) for raw in load_fixture("clips.json")["data"])
    assert first.id == "TawdryMuddyDogePJSalt-KjqqboPnH55MrIKY"
    assert first.url == "https://www.twitch.tv/valkyrae/clip/TawdryMuddyDogePJSalt-KjqqboPnH55MrIKY"
    assert first.created_at == datetime(2026, 11, 7, 0, 7, 23, tzinfo=UTC)
    assert (first.view_count, first.duration) == (2536, 55.8)
    assert (first.video_id, first.vod_offset, first.game_id) == ("2921565789", 3085, "32982")
    assert first.creator_name == "viewer_a"
    # Twitch sends an empty string and null once the VOD is gone.
    assert (second.video_id, second.vod_offset) == (None, None)


def test_parse_clip_tolerates_missing_optional_values() -> None:
    raw = make_clip("x", "1", "2026-11-10T23:01:02Z", 12, game_id="", video_id="", vod_offset=None)
    raw.update(title="", creator_name="")
    parsed = parse_clip(raw)
    assert (parsed.video_id, parsed.vod_offset, parsed.game_id) == (None, None, None)
    assert (parsed.title, parsed.creator_name) == ("", None)


def test_select_top_n_per_local_day() -> None:
    clips = [
        # 2026-11-11 in Chicago (UTC-6): 06:00Z on the 11th .. 05:59Z on the 12th
        clip("a", "2026-11-11T08:00:00Z", 50),
        clip("b", "2026-11-11T20:00:00Z", 500),
        clip("c", "2026-11-12T03:00:00Z", 100),  # 21:00 local on the 11th
        # 2026-11-12 in Chicago
        clip("d", "2026-11-12T07:00:00Z", 5),
        clip("e", "2026-11-12T15:00:00Z", 1),
    ]
    picked = select_clips(clips, top_n=2, min_views=None, tz=CHICAGO)
    assert [c.id for c in picked] == ["b", "c", "d", "e"]
    # The same clips grouped by UTC day would pick differently:
    assert [c.id for c in select_clips(clips, 2, None, UTC)] == ["a", "b", "c", "d"]


def test_select_ties_go_to_the_older_clip() -> None:
    clips = [clip("new", "2026-11-11T20:00:00Z", 10), clip("old", "2026-11-11T19:00:00Z", 10)]
    assert [c.id for c in select_clips(clips, 1, None, CHICAGO)] == ["old"]


def test_select_min_views_replaces_top_n() -> None:
    clips = [clip(str(v), "2026-11-11T20:00:00Z", v) for v in (5, 50, 500, 5000)]
    picked = select_clips(clips, top_n=1, min_views=50, tz=CHICAGO)
    assert sorted(c.view_count for c in picked) == [50, 500, 5000]


def test_day_windows_cover_whole_local_days() -> None:
    start = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)  # 14:00 local on the 10th
    windows = day_windows(start, NOW, CHICAGO)
    assert windows == [
        (datetime(2026, 11, 10, 6, tzinfo=UTC), datetime(2026, 11, 11, 6, tzinfo=UTC)),
        (datetime(2026, 11, 11, 6, tzinfo=UTC), datetime(2026, 11, 12, 6, tzinfo=UTC)),
        (datetime(2026, 11, 12, 6, tzinfo=UTC), NOW),
    ]


def test_day_windows_handle_daylight_saving_and_tiny_windows() -> None:
    # Clocks went back on 2026-11-01 in Chicago: that local day is 25 hours long.
    start = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    end = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    first, second = day_windows(start, end, CHICAGO)
    assert first[1] - first[0] == timedelta(hours=25)
    assert second == (datetime(2026, 11, 2, 6, tzinfo=UTC), end)
    # 20 seconds past local midnight: today's window is too short to query.
    just_after_midnight = datetime(2026, 11, 12, 6, 0, 20, tzinfo=UTC)
    assert len(day_windows(just_after_midnight, just_after_midnight, CHICAGO)) == 0


# --- the job -------------------------------------------------------------------


def test_discover_inserts_top_clips_as_found(conn: sqlite3.Connection, fake: FakeTwitch) -> None:
    setup_streamers(conn)
    fake.clips = [
        make_clip("a1", "1001", "2026-11-11T20:00:00Z", 900),
        make_clip("a2", "1001", "2026-11-11T21:00:00Z", 800),
        make_clip("a3", "1001", "2026-11-11T22:00:00Z", 10),  # third of the day: not picked
        make_clip("a4", "1001", "2026-11-12T15:00:00Z", 3),  # today
        make_clip("old", "1001", "2026-11-01T15:00:00Z", 99999),  # before the 7-day first run
        make_clip(
            "b1", "1002", "2026-11-09T15:00:00Z", 7, video_id="", vod_offset=None, game_id=""
        ),
    ]
    results = discover_all(conn, client_for(fake), CONFIG, now=NOW)

    assert {r.login: [c.id for c in r.new_clips] for r in results} == {
        "alpha": ["a1", "a2", "a4"],
        "beta": ["b1"],
    }
    assert all(r.error is None for r in results)
    a1 = conn.execute("SELECT * FROM clips WHERE id = 'a1'").fetchone()
    assert (a1["state"], a1["source"], a1["view_count"]) == ("found", "viewer", 900)
    assert (a1["game_id"], a1["game_name"]) == ("33214", "Fortnite")
    assert (a1["vod_id"], a1["vod_offset"], a1["created_at"]) == ("v1", 100, "2026-11-11T20:00:00Z")
    assert a1["url"] == "https://clips.twitch.tv/a1"
    assert a1["downloaded_via"] is None
    b1 = conn.execute("SELECT * FROM clips WHERE id = 'b1'").fetchone()
    assert (b1["vod_id"], b1["vod_offset"], b1["game_name"]) == (None, None, None)
    assert runs.get_cursor(conn, "discover") == "2026-11-12T18:00:00Z"
    assert runs.get_run(conn, "discover")["last_error"] is None


def test_running_again_adds_nothing(conn: sqlite3.Connection, fake: FakeTwitch) -> None:
    setup_streamers(conn)
    fake.clips = [make_clip(f"c{i}", "1001", "2026-11-11T20:00:00Z", 100 - i) for i in range(6)]
    client = client_for(fake)
    first = discover_all(conn, client, CONFIG, now=NOW)
    assert sum(len(r.new_clips) for r in first) == 2
    before = clip_ids(conn)

    for minutes in (0, 15, 30):
        again = discover_all(conn, client, CONFIG, now=NOW + timedelta(minutes=minutes))
        assert sum(len(r.new_clips) for r in again) == 0
    assert clip_ids(conn) == before
    assert fake.tokens_issued == 1
    assert len(fake.calls("/helix/games")) == 1  # game names are cached


def test_resumes_from_cursor_after_a_long_sleep(conn: sqlite3.Connection, fake: FakeTwitch) -> None:
    setup_streamers(conn, "alpha top_n=1\n")
    client = client_for(fake)
    discover_all(conn, client, CONFIG, now=NOW)
    fake.requests.clear()

    # The laptop sleeps for 3 days; clips were made on each day in between.
    for day in (13, 14, 15):
        fake.clips.append(make_clip(f"d{day}", "1001", f"2026-11-{day}T15:00:00Z", 10))
    later = NOW + timedelta(days=3)
    results = discover_all(conn, client, CONFIG, now=later)
    assert [c.id for c in results[0].new_clips] == ["d13", "d14", "d15"]
    # One request per local day from the day before the cursor up to today.
    assert results[0].days == 5 == len(fake.calls("/helix/clips"))


def test_view_counts_and_vod_position_are_refreshed(
    conn: sqlite3.Connection, fake: FakeTwitch
) -> None:
    setup_streamers(conn, "alpha\n")
    fake.clips = [make_clip("a1", "1001", "2026-11-12T15:00:00Z", 5, video_id="", vod_offset=None)]
    client = client_for(fake)
    discover_all(conn, client, CONFIG, now=NOW)
    conn.execute("UPDATE clips SET state = 'fetched', updated_at = '2000-01-01T00:00:00Z'")

    fake.clips[0].update(view_count=250, video_id="v9", vod_offset=777)
    discover_all(conn, client, CONFIG, now=NOW + timedelta(minutes=15))
    row = conn.execute("SELECT * FROM clips WHERE id = 'a1'").fetchone()
    assert (row["view_count"], row["vod_id"], row["vod_offset"]) == (250, "v9", 777)
    assert row["state"] == "fetched"  # discovery never moves a clip backwards
    assert row["updated_at"] > "2000-01-01T00:00:00Z"


def test_min_views_mode_pages_until_below_threshold(
    conn: sqlite3.Connection, fake: FakeTwitch
) -> None:
    setup_streamers(conn, "alpha min_views=100\n")
    fake.clips = [
        make_clip(f"c{i:03}", "1001", "2026-11-12T15:00:00Z", 1000 - i) for i in range(250)
    ] + [make_clip(f"low{i}", "1001", "2026-11-12T15:30:00Z", 5) for i in range(150)]
    results = discover_all(conn, client_for(fake), CONFIG, now=NOW, since=timedelta(hours=1))
    assert len(results[0].new_clips) == 250
    assert len(fake.calls("/helix/clips")) == 3  # stops on the page where views drop below 100


def test_since_overrides_the_cursor(conn: sqlite3.Connection, fake: FakeTwitch) -> None:
    setup_streamers(conn, "alpha\n")
    client = client_for(fake)
    discover_all(conn, client, CONFIG, now=NOW)
    fake.clips = [make_clip("ancient", "1001", "2026-10-20T15:00:00Z", 10)]
    assert discover_all(conn, client, CONFIG, now=NOW)[0].new_clips == []
    result = discover_all(conn, client, CONFIG, now=NOW, since=timedelta(days=30))[0]
    assert [c.id for c in result.new_clips] == ["ancient"]


def test_one_streamer_failing_does_not_stop_the_others(
    conn: sqlite3.Connection, fake: FakeTwitch
) -> None:
    setup_streamers(conn)
    fake.clips = [
        make_clip("a1", "1001", "2026-11-12T15:00:00Z", 5),
        make_clip("b1", "1002", "2026-11-12T15:00:00Z", 5),
    ]
    fake.fail_clips_for = {"1001"}
    client = client_for(fake)
    results = {r.login: r for r in discover_all(conn, client, CONFIG, now=NOW)}
    assert "kept failing" in results["alpha"].error
    assert [c.id for c in results["beta"].new_clips] == ["b1"]
    assert "alpha" in runs.get_run(conn, "discover")["last_error"]
    alpha_id = conn.execute("SELECT id FROM streamers WHERE login = 'alpha'").fetchone()[0]
    assert runs.get_cursor(conn, f"discover:{alpha_id}") is None  # nothing was skipped over

    fake.fail_clips_for = set()
    results = {r.login: r for r in discover_all(conn, client, CONFIG, now=NOW)}
    assert [c.id for c in results["alpha"].new_clips] == ["a1"]
    assert runs.get_run(conn, "discover")["last_error"] is None


def test_unknown_login_and_paused_streamer(conn: sqlite3.Connection, fake: FakeTwitch) -> None:
    setup_streamers(conn, "alpha save=off\nghost\n")
    fake.clips = [make_clip("a1", "1001", "2026-11-12T15:00:00Z", 5)]
    results = discover_all(conn, client_for(fake), CONFIG, now=NOW)
    assert [(r.login, r.error) for r in results] == [
        ("ghost", "Twitch has no user with this login")
    ]
    assert clip_ids(conn) == []
    assert fake.calls("/helix/clips") == []


def test_only_the_requested_streamer_runs(conn: sqlite3.Connection, fake: FakeTwitch) -> None:
    setup_streamers(conn)
    fake.clips = [
        make_clip("a1", "1001", "2026-11-12T15:00:00Z", 5),
        make_clip("b1", "1002", "2026-11-12T15:00:00Z", 5),
    ]
    results = discover_all(conn, client_for(fake), CONFIG, login="beta", now=NOW)
    assert [r.login for r in results] == ["beta"]
    assert clip_ids(conn) == ["b1"]


def test_game_lookup_failure_still_saves_the_clip(
    conn: sqlite3.Connection, fake: FakeTwitch
) -> None:
    setup_streamers(conn, "alpha\n")
    fake.clips = [make_clip("a1", "1001", "2026-11-12T15:00:00Z", 5, game_id="777")]
    discover_all(conn, client_for(fake), CONFIG, now=NOW)  # Twitch doesn't know game 777
    row = conn.execute("SELECT game_id, game_name FROM clips").fetchone()
    assert (row["game_id"], row["game_name"]) == ("777", None)


def test_bad_keys_stop_the_run(conn: sqlite3.Connection, fake: FakeTwitch) -> None:
    setup_streamers(conn)
    http = fake.client()
    client = TwitchClient("cid", AppTokenProvider("cid", "wrong", http), http)
    with pytest.raises(TwitchAuthError):
        discover_all(conn, client, CONFIG, now=NOW)
