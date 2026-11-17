"""The live view: what ClipVault is doing right now, and the /api/activity answer."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from clipvault import activity, alerts, jobs, power
from clipvault.timeutil import to_rfc3339, utc_now
from tests.helpers import add_clip, add_streamer
from tests.test_web import csrf, finish_setup, login, saved_clip

# --- the in-memory record ------------------------------------------------------------------


def test_work_is_shown_while_it_runs_and_its_usual_time_is_learned() -> None:
    before = activity.snapshot()["work"]
    with activity.working("c1", "editing", "alpha"):
        work = activity.snapshot()["work"]
        assert [(w["clip_id"], w["stage"], w["login"]) for w in work] == [
            ("c1", "editing", "alpha")
        ]
        assert work[0]["expected_s"] == activity.TYPICAL_S["editing"]
        assert activity.working_on() == {"c1": "editing"}
    assert before == [] and activity.snapshot()["work"] == []
    assert activity._state.typical["editing"] < activity.TYPICAL_S["editing"]  # it was quick


def test_failed_work_disappears_without_changing_the_usual_time() -> None:
    with pytest.raises(RuntimeError), activity.working("c1", "cutting"):
        raise RuntimeError("yt-dlp failed")
    assert activity.snapshot()["work"] == []
    assert activity._state.typical["cutting"] == activity.TYPICAL_S["cutting"]


def test_steps_events_and_next_runs() -> None:
    activity.step_started("discover")
    activity.step_started("notify")  # too small to show
    assert [s["label"] for s in activity.snapshot()["steps"]] == ["Looking for new clips"]
    activity.step_finished("discover")
    assert activity.snapshot()["steps"] == []
    activity.event("found", "3 new clips found", login="alpha")
    activity.event("moment", "Live moment caught", login="alpha")
    first = activity.snapshot()
    assert [e["kind"] for e in first["events"]] == ["found", "moment"]
    assert activity.snapshot(since=first["seq"])["events"] == []
    activity.next_run("discover", 1234.0)
    assert activity.snapshot()["next"] == {"discover": 1234.0}


def test_live_channels_come_and_go() -> None:
    activity.live_status("idle")
    channel = activity.LiveChannel("100", "alpha", windows=list(range(50)), baseline=4.0)
    activity.live_channel(channel)
    live = activity.snapshot()["live"]
    assert live["status"] == "watching" and len(live["channels"][0]["windows"]) == 30
    activity.live_ended("100")
    assert activity.snapshot()["live"]["channels"] == []
    activity.live_status("off")
    assert activity.snapshot()["live"]["status"] == "off"


# --- the jobs report what they do ----------------------------------------------------------------


def test_rendering_reports_its_work_and_the_result(conn, home) -> None:
    from tests.helpers import make_config

    add_streamer(conn, "alpha")
    seen = []

    def render(conn, config, clip_id):
        seen.append(activity.working_on())

    add_clip(conn, "c1", state="transcribed", file_path="alpha/c1.mp4")
    jobs.run_render(conn, make_config(home), render=render)
    assert seen == [{"c1": "editing"}]
    assert [e["kind"] for e in activity.snapshot()["events"]] == ["edited"]


def test_live_moments_are_downloaded_first(conn, home) -> None:
    from tests.helpers import FakeDownloader, make_config

    add_streamer(conn, "alpha")
    add_clip(conn, "popular", view_count=99_000)
    add_clip(conn, "live:v1:100", source="live_spike", view_count=None, vod_id="v1",
             vod_offset=100, url="https://www.twitch.tv/videos/v1")  # fmt: skip
    order = []
    downloader = FakeDownloader()

    def cut(url, dest, quality, start, end):
        order.append("live")
        return downloader(url, dest, quality)

    def download(url, dest, quality):
        order.append("viewer")
        return downloader(url, dest, quality)

    jobs.run_fetch(conn, make_config(home), download=download, cut=cut, is_silent=lambda p: False)
    assert order == ["live", "viewer"]


# --- /api/activity --------------------------------------------------------------------------------


def clips_on_their_way(conn, library):
    add_streamer(conn, "alpha", display_name="Alpha")
    soon = to_rfc3339(utc_now() + timedelta(seconds=150))
    add_clip(conn, "live:v1:100", source="live_spike", view_count=None, surge=5.2,
             created_at=to_rfc3339(utc_now()), retry_at=soon)  # fmt: skip
    add_clip(conn, "c-saved", state="fetched", file_path="alpha/2026-11/c-saved.mp4",
             fetched_at=to_rfc3339(utc_now()))  # fmt: skip
    saved_clip(conn, library, "c-subtitled")


def test_the_live_view_lists_clips_on_their_way(client, conn, home, library, monkeypatch) -> None:
    finish_setup(home)
    login(client)
    clips_on_their_way(conn, library)
    monkeypatch.setattr(power, "on_battery", lambda: True)
    data = client.get("/api/activity").json()
    by_id = {p["id"]: p for p in data["pipeline"]}
    assert data["waiting"] == 3
    moment = by_id["live:v1:100"]
    assert moment["status"] == "waiting_stream" and moment["steps"] == 1
    assert moment["surge"] == 5.2 and moment["name"] == "Alpha"
    assert 100 < moment["wait_until"] - time.time() <= 150
    assert by_id["c-saved"]["status"] == "power" and by_id["c-saved"]["steps"] == 2
    assert by_id["c-subtitled"]["steps"] == 3 and by_id["c-subtitled"]["has_file"]
    assert data["pipeline"][0]["id"] == "live:v1:100"  # live moments first
    assert data["power"] == {"battery": True, "heavy_ok": False}
    assert data["counts"]["editing"] == 1  # only the subtitled one can be edited now


def test_work_in_progress_comes_first_with_its_timing(client, conn, home, library) -> None:
    finish_setup(home)
    login(client)
    clips_on_their_way(conn, library)
    with activity.working("c-subtitled", "editing", "alpha"):
        data = client.get("/api/activity").json()
    top = data["pipeline"][0]
    assert top["id"] == "c-subtitled" and top["status"] == "editing" and top["started"]
    assert data["work"][0]["name"] == "Alpha"


def test_a_finished_clip_comes_back_as_its_card(client, conn, home, library) -> None:
    finish_setup(home)
    login(client)
    add_streamer(conn, "alpha")
    saved_clip(conn, library, "c1")
    before = to_rfc3339(utc_now() - timedelta(minutes=1))
    conn.execute(
        "INSERT INTO renders (clip_id, layout, path, qa_passed, created_at) "
        "VALUES ('c1', 'letterbox', 'alpha/2026-11/vertical/c1_9x16.mp4', 1, ?)",
        (to_rfc3339(utc_now()),),
    )
    data = client.get("/api/activity", params={"after": before}).json()
    assert [d["id"] for d in data["done"]] == ["c1"]
    assert 'href="/clips/c1"' in data["done"][0]["html"] and data["after"] > before
    again = client.get("/api/activity", params={"after": data["after"]}).json()
    assert again["done"] == []
    activity.event("edited", "Phone version ready", clip_id="c1")
    named = client.get("/api/activity").json()["events"][-1]
    assert named["login"] == "alpha" and named["name"] == "alpha"


def test_the_live_view_needs_the_panel_login(client, home) -> None:
    finish_setup(home)
    login(client)
    client.cookies.clear()
    assert client.get("/api/activity").status_code == 303


def test_home_and_clips_carry_the_first_answer(client, conn, home, library) -> None:
    finish_setup(home)
    login(client)
    clips_on_their_way(conn, library)
    home_page = client.get("/").text
    assert 'id="activity-initial"' in home_page and "data-activity-now" in home_page
    assert "live:v1:100" in home_page
    assert "data-activity-grid" in client.get("/clips").text
    assert "data-activity-grid" not in client.get("/clips?streamer=alpha").text  # filtered


def test_live_moments_can_be_switched_off_and_on(client, conn, home) -> None:
    from clipvault.live import LIVE_SETTING

    finish_setup(home)
    login(client)
    token = csrf(client)
    assert 'name="on" value="1" checked' in client.get("/settings").text
    client.post("/settings/live", data={"csrf": token, "next": "/"})
    assert alerts.get_setting(conn, LIVE_SETTING) == "0"
    assert client.get("/api/activity").json()["live"]["on"] is False
    client.post("/settings/live", data={"csrf": token, "on": "1"})
    assert alerts.get_setting(conn, LIVE_SETTING) == "1"


def test_editing_on_battery_can_be_switched_on_from_home(client, home) -> None:
    from clipvault import config as cfg

    finish_setup(home)
    login(client)
    reply = client.post("/settings/heavy-on-battery", data={"csrf": csrf(client), "next": "/"})
    assert reply.status_code == 303
    assert cfg.load_config(home / "config.yaml").heavy_jobs_on_battery is True
