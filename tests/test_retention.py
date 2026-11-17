from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from clipvault import jobs
from clipvault.alerts import open_alerts
from clipvault.library import retention, storage
from tests.helpers import FakeDownloader, add_clip, add_streamer, clip_row, make_config

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
OLD = "2026-08-01T12:00:00Z"  # more than 60 days before NOW
RECENT = "2026-11-01T12:00:00Z"


def saved_library(conn: sqlite3.Connection, home: Path, clips: dict[str, dict]):
    """Create clips and 'download' them all, returning (config, library)."""
    config = make_config(home)
    if not conn.execute("SELECT 1 FROM streamers").fetchone():
        add_streamer(conn, "alpha")
    for clip_id, columns in clips.items():
        add_clip(conn, clip_id, **columns)
    jobs.run_fetch(conn, config, now=NOW, download=FakeDownloader())
    return config, storage.open_library(conn, config)


def planned(conn, config, library) -> list[str]:
    return [item.clip_id for item in retention.plan_retention(conn, config, library, NOW)]


def test_old_clips_below_the_median_are_planned(conn: sqlite3.Connection, home: Path) -> None:
    config, library = saved_library(
        conn,
        home,
        {
            "old-low": {"created_at": OLD, "view_count": 10},
            "old-mid": {"created_at": OLD, "view_count": 100},  # the median itself: kept
            "old-high": {"created_at": OLD, "view_count": 1000},
            "new-low": {"created_at": RECENT, "view_count": 1},  # too young
            "new-high": {"created_at": RECENT, "view_count": 5000},
        },
    )
    plan = retention.plan_retention(conn, config, library, NOW)
    assert [(p.clip_id, p.median_views) for p in plan] == [("old-low", 100)]


def test_chat_and_live_moments_are_not_judged_by_views(
    conn: sqlite3.Connection, home: Path
) -> None:
    """They have no view count; counting them as 0 deleted every one after the keep time."""
    config, library = saved_library(
        conn,
        home,
        {
            "old-low": {"created_at": OLD, "view_count": 10},
            "old-high": {"created_at": OLD, "view_count": 1000},
        },
    )
    for clip_id, source in (("live:v1:100", "live_spike"), ("vod:v2:300", "vod_spike")):
        relpath = f"alpha/2026-08/{storage.safe_id(clip_id)}.mp4"
        (library.root / relpath).parent.mkdir(parents=True, exist_ok=True)
        (library.root / relpath).write_bytes(b"cut")
        add_clip(conn, clip_id, source=source, view_count=None, created_at=OLD, state="fetched",
                 file_path=relpath, fetched_at=OLD)  # fmt: skip
    plan = retention.plan_retention(conn, config, library, NOW)
    # Only the viewer clip below the viewer clips' median (505) goes; the moments stay
    # and don't pull the median down.
    assert [(p.clip_id, p.median_views) for p in plan] == [("old-low", 505)]


def test_retention_deletes_files_and_keeps_the_record(conn: sqlite3.Connection, home: Path) -> None:
    config, library = saved_library(
        conn,
        home,
        {
            "old-low": {"created_at": OLD, "view_count": 10},
            "old-high": {"created_at": OLD, "view_count": 1000},
            "newer": {"created_at": RECENT, "view_count": 500},
        },
    )
    low = clip_row(conn, "old-low")
    video = library.root / low["file_path"]
    sidecar = library.root / low["sidecar_path"]
    assert video.is_file() and sidecar.is_file()

    plan = retention.plan_retention(conn, config, library, NOW)
    assert retention.apply_retention(conn, library, plan) == 1
    assert not video.exists() and not sidecar.exists()
    row = clip_row(conn, "old-low")
    assert (row["removed_reason"], row["state"]) == ("retention", "fetched")
    assert row["removed_at"]
    assert (library.root / clip_row(conn, "old-high")["file_path"]).is_file()

    # Running again deletes nothing more, and the removed clip is never downloaded again.
    assert planned(conn, config, library) == []
    download = FakeDownloader()
    jobs.run_fetch(conn, config, now=NOW, download=download)
    assert download.calls == []


def test_the_bar_does_not_creep_up_run_after_run(conn: sqlite3.Connection, home: Path) -> None:
    views = {f"c{v}": {"created_at": OLD, "view_count": v} for v in (10, 20, 30, 40, 50, 60, 70)}
    config, library = saved_library(conn, home, views)
    first = retention.plan_retention(conn, config, library, NOW)
    assert sorted(p.clip_id for p in first) == ["c10", "c20", "c30"]
    retention.apply_retention(conn, library, first)
    # The median still counts the removed clips, so the survivors are safe.
    assert planned(conn, config, library) == []


def test_starred_favorites_and_posted_sources_are_kept(
    conn: sqlite3.Connection, home: Path
) -> None:
    config, library = saved_library(
        conn,
        home,
        {
            "starred": {"created_at": OLD, "view_count": 1, "starred": 1},
            "favorite": {"created_at": OLD, "view_count": 2},
            "queued": {"created_at": OLD, "view_count": 3},
            "posted-recently": {"created_at": OLD, "view_count": 4},
            "posted-long-ago": {"created_at": OLD, "view_count": 5},
            "plain": {"created_at": OLD, "view_count": 6},
            "top1": {"created_at": OLD, "view_count": 900},
            "top2": {"created_at": OLD, "view_count": 901},
            "top3": {"created_at": OLD, "view_count": 902},
            "top4": {"created_at": OLD, "view_count": 903},
            "top5": {"created_at": OLD, "view_count": 904},
            "top6": {"created_at": OLD, "view_count": 905},
        },
    )
    favorite = library.root / clip_row(conn, "favorite")["file_path"]
    (library.root / storage.FAVORITES).mkdir()
    favorite.rename(library.root / storage.FAVORITES / favorite.name)

    posts = {
        "queued": ("queued", None),
        "posted-recently": ("posted", "2026-11-01T00:00:00Z"),
        "posted-long-ago": ("posted", "2026-09-01T00:00:00Z"),
    }
    for clip_id, (status, posted_at) in posts.items():
        render = conn.execute(
            "INSERT INTO renders (clip_id, layout) VALUES (?, 'letterbox')", (clip_id,)
        ).lastrowid
        conn.execute(
            "INSERT INTO posts (render_id, platform, account, status, posted_at) "
            "VALUES (?, 'tiktok', 'acct', ?, ?)",
            (render, status, posted_at),
        )
    assert planned(conn, config, library) == ["posted-long-ago", "plain"]
    retention.apply_retention(conn, library, retention.plan_retention(conn, config, library, NOW))
    assert (library.root / storage.FAVORITES / favorite.name).is_file()
    assert (library.root / clip_row(conn, "starred")["file_path"]).is_file()


def test_files_outside_the_library_are_never_deleted(conn: sqlite3.Connection, home: Path) -> None:
    config, library = saved_library(
        conn,
        home,
        {"low": {"created_at": OLD, "view_count": 1}, "high": {"created_at": OLD, "view_count": 9}},
    )
    outside = home / "precious.mp4"
    outside.write_bytes(b"not ours")
    # A corrupted database row pointing out of the library must not lead to a deletion.
    conn.execute("UPDATE clips SET file_path = '../precious.mp4' WHERE id = 'low'")
    plan = retention.plan_retention(conn, config, library, NOW)
    assert [p.clip_id for p in plan] == ["low"]
    retention.apply_retention(conn, library, plan)
    assert outside.read_bytes() == b"not ours"


def test_nothing_is_planned_while_the_library_is_away(conn: sqlite3.Connection, home: Path) -> None:
    config, library = saved_library(
        conn,
        home,
        {"low": {"created_at": OLD, "view_count": 1}, "high": {"created_at": OLD, "view_count": 9}},
    )
    (library.root / storage.MARKER).unlink()
    away = storage.open_library(conn, config)
    assert retention.plan_retention(conn, config, away, NOW) == []
    assert retention.mark_missing_files(conn, away) == 0


def test_a_file_deleted_by_hand_is_noticed_and_not_redownloaded(
    conn: sqlite3.Connection, home: Path
) -> None:
    config, library = saved_library(conn, home, {f"c{i}": {"view_count": i} for i in range(12)})
    (library.root / clip_row(conn, "c3")["file_path"]).unlink()
    assert retention.mark_missing_files(conn, library) == 1
    row = clip_row(conn, "c3")
    assert (row["removed_reason"], row["state"]) == ("missing", "fetched")
    assert retention.mark_missing_files(conn, library) == 0

    download = FakeDownloader()
    jobs.run_fetch(conn, config, now=NOW, download=download)
    assert download.calls == []
    assert retention.plan_retention(conn, config, library, NOW) == []


def test_a_file_that_comes_back_is_unmarked(conn: sqlite3.Connection, home: Path) -> None:
    config, library = saved_library(conn, home, {f"c{i}": {"view_count": i} for i in range(12)})
    video = library.root / clip_row(conn, "c3")["file_path"]
    aside = home / "recycle-bin.mp4"
    video.rename(aside)
    assert retention.mark_missing_files(conn, library) == 1
    aside.rename(video)  # restored from the recycle bin
    assert retention.mark_missing_files(conn, library) == 0
    assert clip_row(conn, "c3")["removed_at"] is None

    # Moving it into Favorites later counts as "still there" too.
    video.rename(aside)
    retention.mark_missing_files(conn, library)
    (library.root / storage.FAVORITES).mkdir()
    aside.rename(library.root / storage.FAVORITES / video.name)
    retention.mark_missing_files(conn, library)
    assert clip_row(conn, "c3")["removed_at"] is None


def test_many_files_vanishing_at_once_raises_an_alert_instead(
    conn: sqlite3.Connection, home: Path
) -> None:
    config, library = saved_library(conn, home, {f"c{i}": {"view_count": i} for i in range(12)})
    for row in conn.execute("SELECT file_path FROM clips").fetchall():
        (library.root / row["file_path"]).unlink()  # as if the folder had been moved away
    assert retention.mark_missing_files(conn, library) == 0
    assert (
        conn.execute("SELECT count(*) FROM clips WHERE removed_at IS NOT NULL").fetchone()[0] == 0
    )
    alerts = open_alerts(conn)
    assert [a["kind"] for a in alerts] == ["library"]
    assert "12 of 12" in alerts[0]["message"]


def test_disk_check_opens_and_closes_the_storage_alert(
    conn: sqlite3.Connection, home: Path, monkeypatch
) -> None:
    config = make_config(home, min_free_space_pct=15)
    library = storage.open_library(conn, config)
    assert 0 < retention.free_space_pct(library.root / "not" / "there" / "yet") <= 100
    monkeypatch.setattr(retention, "free_space_pct", lambda path: 14.9)
    assert retention.check_disk(conn, config, library) is False
    assert retention.check_disk(conn, config, library) is False
    assert len(open_alerts(conn)) == 1
    monkeypatch.setattr(retention, "free_space_pct", lambda path: 15.0)
    assert retention.check_disk(conn, config, library) is True
    assert open_alerts(conn) == []
