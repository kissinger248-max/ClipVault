from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clipvault import jobs
from clipvault.alerts import open_alerts, raise_alert, resolve_alerts
from clipvault.fetch.ytdlp import DownloadError
from clipvault.library import retention, storage
from clipvault.process.filter import Span, find_duplicates, overlap_fraction
from tests.helpers import FakeDownloader, add_clip, add_streamer, clip_row, make_config

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def alert_kinds(conn: sqlite3.Connection) -> list[str]:
    return [a["kind"] for a in open_alerts(conn)]


# --- duplicates (pure) -----------------------------------------------------------


def span(clip_id: str, start: float | None, duration: float, views: int, **kw) -> Span:
    return Span(clip_id, kw.pop("vod", "v1"), start, duration, views, kw.pop("at", "2026"), **kw)


def test_overlap_fraction_uses_the_shorter_clip() -> None:
    assert overlap_fraction(span("a", 100, 30, 1), span("b", 110, 30, 1)) == pytest.approx(2 / 3)
    assert overlap_fraction(span("a", 100, 60, 1), span("b", 120, 10, 1)) == 1.0
    assert overlap_fraction(span("a", 100, 30, 1), span("b", 130, 30, 1)) == 0.0
    assert overlap_fraction(span("a", None, 30, 1), span("b", 100, 30, 1)) == 0.0


def test_duplicates_keep_the_most_viewed() -> None:
    spans = [
        span("low", 100, 30, 10),
        span("high", 105, 30, 500),
        span("barely", 120, 30, 50),  # overlaps "high" by exactly half: not a duplicate
        span("other-vod", 100, 30, 1, vod="v2"),
        span("no-position", None, 30, 1),
        span("no-vod", 100, 30, 1, vod=None),
    ]
    assert find_duplicates(spans) == {"low": "high"}


def test_a_downloaded_clip_always_wins_and_ties_go_to_the_older() -> None:
    spans = [span("saved", 100, 30, 5, settled=True), span("new", 100, 30, 9999)]
    assert find_duplicates(spans) == {"new": "saved"}
    tie = [span("b", 100, 30, 7, at="2026-11-02"), span("a", 100, 30, 7, at="2026-11-01")]
    assert find_duplicates(tie) == {"b": "a"}
    both_saved = [span("x", 100, 30, 1, settled=True), span("y", 100, 30, 2, settled=True)]
    assert find_duplicates(both_saved) == {}


# --- the fetch step --------------------------------------------------------------


def test_fetch_saves_file_sidecar_and_marks_fetched(conn: sqlite3.Connection, home: Path) -> None:
    config = make_config(home)
    add_streamer(conn, "alpha")
    add_clip(conn, "Clip-One", title="Big play!", view_count=1234, vod_id="v1", vod_offset=10)
    download = FakeDownloader(size=4096)
    lines: list[str] = []
    summary = jobs.run_fetch(conn, config, now=NOW, download=download, on_progress=lines.append)

    assert (summary.downloaded, summary.failed, summary.bytes, summary.remaining) == (1, 0, 4096, 0)
    row = clip_row(conn, "Clip-One")
    assert row["file_path"] == "alpha/2026-11/2026-11-10_1234views_big-play_Clip-One.mp4"
    assert (row["state"], row["downloaded_via"], row["file_size"]) == (
        "fetched",
        "unofficial",
        4096,
    )
    assert row["fetched_at"] and row["retry_at"] is None
    video = config.library_root / row["file_path"]
    assert video.stat().st_size == 4096
    assert download.calls == [("https://www.twitch.tv/alpha/clip/Clip-One", video, "720p")]

    sidecar = json.loads((config.library_root / row["sidecar_path"]).read_text(encoding="utf-8"))
    assert sidecar["id"] == "Clip-One" and sidecar["title"] == "Big play!"
    assert sidecar["source_url"] == "https://www.twitch.tv/alpha/clip/Clip-One"
    assert (sidecar["downloaded_via"], sidecar["state"]) == ("unofficial", "fetched")
    assert (sidecar["streamer_login"], sidecar["view_count"]) == ("alpha", 1234)
    assert "1/1" in lines[0] and "Big play!" in lines[0]


def test_second_run_downloads_nothing(conn: sqlite3.Connection, home: Path) -> None:
    config = make_config(home)
    add_streamer(conn)
    for i in range(3):
        add_clip(conn, f"c{i}", view_count=10 * i)
    download = FakeDownloader()
    jobs.run_fetch(conn, config, now=NOW, download=download)
    assert [c[0][-2:] for c in download.calls] == ["c2", "c1", "c0"]  # most viewed first
    again = jobs.run_fetch(conn, config, now=NOW, download=download)
    assert (again.downloaded, again.already_there) == (0, 0)
    assert len(download.calls) == 3


def test_crash_after_download_is_picked_up_without_downloading_again(
    conn: sqlite3.Connection, home: Path
) -> None:
    config = make_config(home)
    add_streamer(conn)
    add_clip(conn, "c1", view_count=5)

    class Crash(Exception):
        pass

    def crash_after_writing(url: str, dest: Path, quality: str) -> Path:
        FakeDownloader()(url, dest, quality)
        raise Crash("power cut right after the file was written")

    with pytest.raises(Crash):
        jobs.run_fetch(conn, config, now=NOW, download=crash_after_writing)
    row = clip_row(conn, "c1")
    assert row["state"] == "found" and row["file_path"]  # the name was recorded first

    # Views changed meanwhile; the clip must still land in the same file.
    conn.execute("UPDATE clips SET view_count = 999 WHERE id = 'c1'")
    download = FakeDownloader()
    summary = jobs.run_fetch(conn, config, now=NOW, download=download)
    assert (summary.already_there, summary.downloaded, download.calls) == (1, 0, [])
    after = clip_row(conn, "c1")
    assert after["state"] == "fetched" and after["file_path"] == row["file_path"]
    files = [p.name for p in (config.library_root / "alpha").rglob("*.mp4")]
    assert len(files) == 1


def test_failures_retry_after_1_5_30_minutes_then_dead(
    conn: sqlite3.Connection, home: Path
) -> None:
    config = make_config(home)
    add_streamer(conn)
    add_clip(conn, "c1")
    download = FakeDownloader()
    download.fail_all = DownloadError("network is down")
    moment = NOW
    for expected_wait in (1, 5, 30):
        summary = jobs.run_fetch(conn, config, now=moment, download=download)
        assert summary.failed == 1 and "network is down" in summary.errors[0]
        row = clip_row(conn, "c1")
        assert row["state"] == "found" and row["last_error"] == "network is down"
        retry_at = datetime.fromisoformat(row["retry_at"])
        assert retry_at - moment == timedelta(minutes=expected_wait)
        # Too early: the clip waits and nothing is attempted.
        early = jobs.run_fetch(conn, config, now=retry_at - timedelta(seconds=1), download=download)
        assert (early.failed, early.waiting) == (0, 1)
        moment = retry_at
    jobs.run_fetch(conn, config, now=moment, download=download)
    row = clip_row(conn, "c1")
    assert (row["state"], row["fail_count"], row["retry_at"]) == ("dead", 4, None)
    assert len(download.calls) == 4
    assert jobs.run_fetch(conn, config, now=moment, download=download).failed == 0  # never again


def test_success_after_a_failure_clears_the_error(conn: sqlite3.Connection, home: Path) -> None:
    config = make_config(home)
    add_streamer(conn)
    add_clip(conn, "c1")
    download = FakeDownloader()
    download.fail_all = DownloadError("blip")
    jobs.run_fetch(conn, config, now=NOW, download=download)
    download.fail_all = None
    jobs.run_fetch(conn, config, now=NOW + timedelta(minutes=2), download=download)
    row = clip_row(conn, "c1")
    assert (row["state"], row["fail_count"], row["last_error"]) == ("fetched", 0, None)


def test_clip_gone_from_twitch_is_rejected_without_retries(
    conn: sqlite3.Connection, home: Path
) -> None:
    config = make_config(home)
    add_streamer(conn)
    add_clip(conn, "gone", view_count=50)
    add_clip(conn, "fine", view_count=5)
    download = FakeDownloader()
    download.failures["gone"] = DownloadError("This clip is no longer available", permanent=True)
    summary = jobs.run_fetch(conn, config, now=NOW, download=download)
    assert (summary.unavailable, summary.downloaded, summary.failed) == (1, 1, 0)
    row = clip_row(conn, "gone")
    assert (row["state"], row["reject_reason"], row["file_path"]) == (
        "rejected",
        "unavailable",
        None,
    )
    assert alert_kinds(conn) == []


def test_three_failures_in_a_row_stop_the_run_and_raise_one_alert(
    conn: sqlite3.Connection, home: Path
) -> None:
    config = make_config(home)
    add_streamer(conn)
    for i in range(6):
        add_clip(conn, f"c{i}", view_count=100 - i)
    download = FakeDownloader()
    download.fail_all = DownloadError("extractor broken")
    summary = jobs.run_fetch(conn, config, now=NOW, download=download)
    assert summary.stopped_on_failures and summary.failed == 3 and summary.remaining == 3
    assert len(download.calls) == 3  # the other three were not burned through
    assert alert_kinds(conn) == ["downloader"]

    jobs.run_fetch(conn, config, now=NOW, download=download)  # fails again: still one alert
    assert alert_kinds(conn) == ["downloader"]

    download.fail_all = None
    jobs.run_fetch(conn, config, now=NOW + timedelta(hours=1), download=download)
    assert alert_kinds(conn) == []  # a success closes it


def test_disk_guard_pauses_downloads(conn: sqlite3.Connection, home: Path, monkeypatch) -> None:
    config = make_config(home, min_free_space_pct=15)
    add_streamer(conn)
    add_clip(conn, "c1")
    download = FakeDownloader()
    monkeypatch.setattr(retention, "free_space_pct", lambda path: 9.0)
    summary = jobs.run_fetch(conn, config, now=NOW, download=download)
    assert summary.paused_for_disk and summary.downloaded == 0 and summary.remaining == 1
    assert download.calls == []
    assert clip_row(conn, "c1")["state"] == "found"
    assert alert_kinds(conn) == ["storage"]

    monkeypatch.setattr(retention, "free_space_pct", lambda path: 40.0)
    summary = jobs.run_fetch(conn, config, now=NOW, download=download)
    assert summary.downloaded == 1 and alert_kinds(conn) == []


def test_duplicates_are_rejected_before_downloading(conn: sqlite3.Connection, home: Path) -> None:
    config = make_config(home)
    add_streamer(conn)
    add_clip(conn, "best", view_count=900, vod_id="v1", vod_offset=100, duration=30)
    add_clip(conn, "repeat", view_count=20, vod_id="v1", vod_offset=104, duration=30)
    add_clip(conn, "different", view_count=10, vod_id="v1", vod_offset=500, duration=30)
    download = FakeDownloader()
    summary = jobs.run_fetch(conn, config, now=NOW, download=download)
    assert (summary.duplicates, summary.downloaded) == (1, 2)
    row = clip_row(conn, "repeat")
    assert (row["state"], row["reject_reason"]) == ("rejected", "duplicate")
    assert "best" in row["last_error"]

    # A later, more-viewed clip of the same moment doesn't replace the saved one.
    add_clip(conn, "late", view_count=5000, vod_id="v1", vod_offset=101, duration=30)
    summary = jobs.run_fetch(conn, config, now=NOW, download=download)
    assert (summary.duplicates, summary.downloaded) == (1, 0)
    assert clip_row(conn, "best")["state"] == "fetched"


def test_limit_streamer_filter_and_save_switch(conn: sqlite3.Connection, home: Path) -> None:
    config = make_config(home)
    alpha = add_streamer(conn, "alpha")
    beta = add_streamer(conn, "beta")
    paused = add_streamer(conn, "paused", save_enabled=0)
    for i in range(3):
        add_clip(conn, f"a{i}", alpha, view_count=i)
    add_clip(conn, "b0", beta)
    add_clip(conn, "p0", paused)
    download = FakeDownloader()
    limited = jobs.run_fetch(conn, config, login="alpha", limit=2, now=NOW, download=download)
    assert (limited.downloaded, limited.remaining) == (2, 1)
    rest = jobs.run_fetch(conn, config, now=NOW, download=download)
    assert rest.downloaded == 2  # a0 and b0; the paused streamer's clip is left alone
    assert clip_row(conn, "p0")["state"] == "found"
    assert (config.library_root / "beta").is_dir()


def test_library_away_downloads_to_cache_then_moves_back(
    conn: sqlite3.Connection, home: Path
) -> None:
    config = make_config(home)
    storage.open_library(conn, config)
    marker = config.library_root / storage.MARKER
    marker.rename(home / "marker-aside")  # the drive is unplugged
    add_streamer(conn)
    add_clip(conn, "c1")
    download = FakeDownloader()
    summary = jobs.run_fetch(conn, config, now=NOW, download=download)
    assert summary.library_away and summary.downloaded == 1
    rel = clip_row(conn, "c1")["file_path"]
    assert (home / storage.CACHE_DIRNAME / rel).is_file()
    assert not (config.library_root / rel).exists()
    assert alert_kinds(conn) == ["library"]

    (home / "marker-aside").rename(marker)  # plugged back in
    add_clip(conn, "c2")
    jobs.run_fetch(conn, config, now=NOW, download=download)
    assert (config.library_root / rel).is_file()
    assert (config.library_root / storage.sidecar_relpath(rel)).is_file()
    assert alert_kinds(conn) == []
    assert clip_row(conn, "c1")["file_path"] == rel  # the database didn't need to change


def test_alerts_do_not_duplicate_and_can_reopen(conn: sqlite3.Connection) -> None:
    assert raise_alert(conn, "storage", "disk full") is True
    assert raise_alert(conn, "storage", "disk full") is False
    assert len(open_alerts(conn)) == 1
    assert resolve_alerts(conn, "storage") == 1
    assert raise_alert(conn, "storage", "disk full") is True
    with pytest.raises(ValueError):
        raise_alert(conn, "nonsense", "x")
