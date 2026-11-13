from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from clipvault import alerts, app, db, jobs, power, runs, scheduler
from clipvault import config as cfg
from clipvault.alerts import open_alerts, set_setting
from clipvault.notify import ntfy
from tests.helpers import add_clip, add_streamer, make_config

CHICAGO = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)  # 12:00 in Chicago


def failing(conn, config, now):
    raise RuntimeError("boom")


def fine(conn, config, now):
    return "did it"


def broken_alerts(conn: sqlite3.Connection) -> list[str]:
    return [a["message"] for a in open_alerts(conn) if a["kind"] == "broken"]


# --- bookkeeping -------------------------------------------------------------------


def test_three_failures_in_a_row_raise_one_broken_alert_and_success_clears_it(conn) -> None:
    config = cfg.Config()
    for _ in range(2):
        assert scheduler.run_step("worker", failing, config, now=NOW) is None
    assert broken_alerts(conn) == []
    scheduler.run_step("worker", failing, config, now=NOW)
    scheduler.run_step("worker", failing, config, now=NOW)
    assert broken_alerts(conn) == [scheduler.broken_message("worker")]
    row = runs.get_run(conn, scheduler.step_job("worker"))
    assert row["fail_streak"] == 4 and "boom" in row["last_error"]
    assert scheduler.run_step("worker", fine, config, now=NOW) == "did it"
    assert broken_alerts(conn) == []
    assert runs.get_run(conn, scheduler.step_job("worker"))["fail_streak"] == 0


def test_pause_skips_work_but_not_notifications(conn) -> None:
    set_setting(conn, "paused", "1")
    assert scheduler.run_step("discover", fine, cfg.Config(), now=NOW) is None
    assert scheduler.run_step("notify", fine, cfg.Config(), now=NOW) == "did it"
    assert runs.get_run(conn, scheduler.step_job("discover")) is None


# --- daily timing ------------------------------------------------------------------


def test_latest_due_is_today_or_yesterday_in_local_time() -> None:
    assert scheduler.latest_due(NOW, "09:00", CHICAGO) == datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
    assert scheduler.latest_due(NOW, "13:00", CHICAGO) == datetime(2026, 11, 11, 19, 0, tzinfo=UTC)


def test_a_daily_step_missed_while_asleep_is_due_on_waking(conn) -> None:
    assert scheduler.daily_due(conn, "summary", "09:00", CHICAGO, NOW)  # never ran
    conn.execute(
        "INSERT INTO runs (job, last_success) VALUES (?, ?)",
        (scheduler.step_job("summary"), "2026-11-11T15:00:30Z"),  # yesterday's 09:00 run
    )
    assert scheduler.daily_due(conn, "summary", "09:00", CHICAGO, NOW)  # slept through today's
    conn.execute(
        "UPDATE runs SET last_success = '2026-11-12T15:01:00Z' WHERE job = ?",
        (scheduler.step_job("summary"),),
    )
    assert not scheduler.daily_due(conn, "summary", "09:00", CHICAGO, NOW)


def test_heartbeat_notices_sleep_and_runs_due_daily_steps_once_per_retry_window(conn) -> None:
    started: list[str] = []
    woke: list[timedelta] = []
    beat = app.Heartbeat(cfg.Config(), started.append, woke.append)
    assert beat.tick(NOW) == ["cleanup", "summary"]  # never ran: both due
    assert beat.tick(NOW + timedelta(minutes=1)) == []  # still failing/running: not hammered
    assert woke == []
    beat.tick(NOW + timedelta(hours=5))
    assert woke == [timedelta(hours=5) - timedelta(minutes=1)]
    assert started == ["cleanup", "summary", "cleanup", "summary"]


# --- steps ----------------------------------------------------------------------------


def test_worker_waits_with_transcription_on_battery(conn, home, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("clipvault.fetch.ytdlp.update_if_due", lambda conn, now: False)
    monkeypatch.setattr(
        jobs, "run_fetch", lambda *a, **k: calls.append("fetch") or jobs.FetchSummary()
    )
    monkeypatch.setattr(
        jobs,
        "run_transcribe",
        lambda *a, **k: calls.append("transcribe") or jobs.TranscribeSummary(),
    )
    monkeypatch.setattr(power, "on_battery", lambda: True)
    config = make_config(home)
    note = scheduler.step_worker(conn, config, NOW)
    assert calls == ["fetch"] and "mains power" in note
    monkeypatch.setattr(power, "on_battery", lambda: False)
    scheduler.step_worker(conn, config, NOW)
    assert calls == ["fetch", "fetch", "transcribe"]


def test_worker_reports_a_stuck_downloader(conn, home, monkeypatch) -> None:
    monkeypatch.setattr("clipvault.fetch.ytdlp.update_if_due", lambda conn, now: False)
    monkeypatch.setattr(
        jobs, "run_fetch", lambda *a, **k: jobs.FetchSummary(stopped_on_failures=True)
    )
    with pytest.raises(scheduler.StepFailed):
        scheduler.step_worker(conn, make_config(home), NOW)


def test_discover_without_keys_is_a_failure_and_without_streamers_is_not(conn) -> None:
    assert scheduler.step_discover(conn, cfg.Config(), NOW) == "no streamers yet"
    add_streamer(conn, "alpha")
    with pytest.raises(scheduler.StepFailed):
        scheduler.step_discover(conn, cfg.Config(), NOW)


def test_backups_rotate_by_weekday_inside_the_library(conn, home) -> None:
    root = home / "library"
    add_streamer(conn, "alpha")
    first = scheduler.backup_database(conn, root, NOW, CHICAGO)
    assert first == root / ".clipvault-backups" / "clipvault-thu.db"
    copy = sqlite3.connect(first)
    assert copy.execute("SELECT login FROM streamers").fetchone() == ("alpha",)
    copy.close()
    again = scheduler.backup_database(conn, root, NOW + timedelta(days=7), CHICAGO)
    assert again == first
    assert sorted(p.name for p in first.parent.iterdir()) == ["clipvault-thu.db"]


def test_summary_counts_the_last_day_and_lists_problems(conn) -> None:
    add_streamer(conn, "alpha")
    add_clip(conn, "new", state="fetched", file_path="a/new.mp4", file_size=5_000_000,
             fetched_at="2026-11-12T10:00:00Z")  # fmt: skip
    add_clip(conn, "old", state="fetched", file_path="a/old.mp4", file_size=1_000_000,
             fetched_at="2026-11-01T10:00:00Z")  # fmt: skip
    add_clip(conn, "waiting")
    alerts.raise_alert(conn, "storage", "Disk almost full")
    text = scheduler.summary_text(conn, NOW)
    assert "Last 24 h: 1 clip(s) saved (5 MB)." in text
    assert "Library: 2 clip(s)" in text and "1 clip(s) waiting" in text
    assert "- Disk almost full" in text


def test_summary_step_queues_one_notification(conn) -> None:
    assert scheduler.step_summary(conn, cfg.Config(), NOW) == "notifications not set up"
    ntfy.ensure_topic()
    scheduler.step_summary(conn, cfg.Config(), NOW)
    rows = conn.execute("SELECT kind, priority FROM notifications").fetchall()
    assert [tuple(r) for r in rows] == [("summary", 2)]


# --- running one copy -----------------------------------------------------------------


def test_only_one_copy_can_run(home: Path) -> None:
    with app.single_instance(home):
        with pytest.raises(app.AlreadyRunning):
            with app.single_instance(home):
                pass
    with app.single_instance(home):  # released when the first one stopped
        pass


def test_app_starts_its_jobs_and_runs_discovery_straight_away(home, monkeypatch) -> None:
    import threading

    db.open_db(home / "clipvault.db").close()
    ran = threading.Event()

    def fake_discover(conn, config, now):
        ran.set()
        return "ok"

    fakes = {name: fine for name in scheduler.STEP_FUNCTIONS} | {"discover": fake_discover}
    monkeypatch.setattr(scheduler, "STEP_FUNCTIONS", fakes)
    runner = app.App(cfg.Config())
    runner.start()
    try:
        ids = {job.id for job in runner.scheduler.get_jobs()}
        assert {"discover", "worker", "notify", "heartbeat"} <= ids
        assert ran.wait(10)
    finally:
        runner.stop()


def test_cli_pause_resume_and_refusing_a_second_copy(conn, home, capsys) -> None:
    from clipvault.__main__ import main

    assert main(["pause"]) == 0 and alerts.is_paused(conn)
    assert main(["resume"]) == 0 and not alerts.is_paused(conn)
    with app.single_instance(home):
        assert main(["run"]) == 1
    assert "already running" in capsys.readouterr().out


def test_cli_notify_setup_shows_the_topic(capsys) -> None:
    from clipvault.__main__ import main

    assert main(["notify", "status"]) == 0
    assert main(["notify", "setup"]) == 0
    assert ntfy.get_topic() in capsys.readouterr().out
    assert main(["notify", "status"]) == 0


def test_new_clips_get_their_phone_version_newest_first(conn, home) -> None:
    from clipvault import jobs
    from clipvault.render.ffmpeg import RenderError

    config = make_config(home)
    add_streamer(conn, "alpha")
    add_clip(
        conn, "old", state="transcribed", file_path="a/old.mp4", created_at="2026-11-01T00:00:00Z"
    )
    add_clip(
        conn, "new", state="transcribed", file_path="a/new.mp4", created_at="2026-11-12T00:00:00Z"
    )
    add_clip(
        conn, "bad", state="transcribed", file_path="a/bad.mp4", created_at="2026-11-10T00:00:00Z"
    )
    add_clip(conn, "raw", state="fetched", file_path="a/raw.mp4")  # waits for its transcript
    made: list[str] = []

    def fake_render(conn, config, clip_id):
        if clip_id == "bad":
            raise RenderError("broken file")
        made.append(clip_id)
        conn.execute("INSERT INTO renders (clip_id, layout, path) VALUES (?, 'letterbox', ?)",
                     (clip_id, f"a/vertical/{clip_id}.mp4"))  # fmt: skip

    assert jobs.clips_to_render(conn, config) == ["new", "bad", "old"]
    summary = jobs.run_render(conn, config, limit=2, render=fake_render)
    assert made == ["new"] and summary.failed == 1 and summary.remaining == 1
    jobs.run_render(conn, config, render=fake_render)
    assert made == ["new", "old"]  # the broken one isn't tried again
    assert jobs.clips_to_render(conn, config) == []
