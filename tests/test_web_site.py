"""ClipVault Web: uploading, making a run, resuming, downloads, expiry (no FFmpeg, no network)."""

from __future__ import annotations

import json
import zipfile
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipvault.process.transcribe import Word
from clipvault.render import engine, ffmpeg, qa
from clipvault.render.ffmpeg import MediaInfo, RenderError
from clipvault.timeutil import utc_now
from clipvault_web import making, store
from clipvault_web import moments as finder
from clipvault_web.server import create_app, download_name
from clipvault_web.settings import WebSettings, from_env
from clipvault_web.worker import Worker

VIDEO = MediaInfo(1280, 720, 30.0, 600.0, True, "h264")
UPLOAD = {"X-File-Name": "My%20stream.mp4", "X-Rights": "yes", "X-Who": "KaiCenat"}


@pytest.fixture
def settings(tmp_path: Path) -> WebSettings:
    return WebSettings(data=tmp_path / "web", max_upload_mb=1, keep_hours=24)


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> dict:
    state = {"info": VIDEO}

    def fake(path: Path) -> MediaInfo:
        if isinstance(state["info"], Exception):
            raise state["info"]
        return state["info"]

    monkeypatch.setattr(ffmpeg, "probe", fake)
    return state


@pytest.fixture
def client(settings: WebSettings, probe) -> TestClient:
    return TestClient(create_app(settings, start_worker=False))


def upload(client: TestClient, body: bytes = b"a video", **headers) -> dict:
    reply = client.post("/api/upload", content=body, headers={**UPLOAD, **headers})
    return {"status": reply.status_code, **reply.json()}


def fake_moments(count: int = 3):
    peak = finder.Peak(100.0, 9.0, -18.0)
    words = (
        Word("no", 0.5, 0.7),
        Word("way!", 0.7, 1.0),
        Word("let's", 2.0, 2.2),
        Word("go!", 2.2, 2.6),
    )
    return [finder.Moment(60.0 * n, 60.0 * n + 30, 90 - n, peak, "no way! let's go!", words, "en",
                          ["+9.0 dB"]) for n in range(count)]  # fmt: skip


@pytest.fixture
def fake_render(monkeypatch: pytest.MonkeyPatch) -> list:
    """The renderer stands in for FFmpeg: it writes small files and passes QA."""
    made = []

    def make_vertical(**kw) -> qa.Report:
        made.append(kw)
        kw["dest"].write_bytes(b"phone version")
        report = qa.Report()
        report.add("duration", True, "30 s")
        return report

    def extract_frame(path: Path, second: float, dest: Path) -> Path:
        dest.write_bytes(b"jpeg")
        return dest

    monkeypatch.setattr(engine, "make_vertical", make_vertical)
    monkeypatch.setattr(ffmpeg, "extract_frame", extract_frame)
    monkeypatch.setattr(making, "find_cam", lambda source, info, look: None)
    return made


def find(moments_list, calls=None):
    def fake(video, duration, count, providers, progress):
        if calls is not None:
            calls.append(video)
        progress("Reading what is said", 1, 2)
        return moments_list[:count], 7

    return fake


# --- uploading ------------------------------------------------------------------------------


def test_the_upload_page_and_its_limits(client: TestClient) -> None:
    page = client.get("/").text
    assert "Make my clips" in page and "Get the free app" in page and "up to 60 minutes" in page
    assert upload(client, **{"X-Rights": "no"})["status"] == 400
    assert upload(client, **{"X-File-Name": "virus.exe"})["status"] == 415
    too_big = upload(client, body=b"x" * (1024 * 1024 + 1))
    assert too_big["status"] == 413 and "1 MB" in too_big["error"]


def test_a_file_that_is_not_a_usable_video_is_refused(client, settings, probe) -> None:
    probe["info"] = RenderError("not a video")
    assert upload(client)["status"] == 415
    probe["info"] = MediaInfo(1280, 720, 30.0, 9.0, True, "h264")
    assert "at least 15 seconds" in upload(client)["error"]
    probe["info"] = MediaInfo(1280, 720, 30.0, 61 * 60, True, "h264")
    assert "at most 60 minutes" in upload(client)["error"]
    probe["info"] = MediaInfo(1280, 720, 30.0, 600, False, "h264")
    assert "no sound" in upload(client)["error"]
    assert list(settings.incoming_dir.iterdir()) == []  # nothing left behind
    assert not any(settings.runs_dir.iterdir())


def test_an_upload_becomes_a_waiting_run_with_a_private_link(client, settings) -> None:
    reply = upload(client, body=b"the video bytes")
    assert reply["status"] == 200 and reply["url"] == f"/r/{reply['id']}"
    assert len(reply["id"]) >= 20
    conn = store.open_store(settings)
    run = store.get_run(conn, reply["id"])
    conn.close()
    assert run["status"] == "queued" and run["source_name"] == "My stream.mp4"
    assert run["name"] == "KaiCenat" and run["visitor"] and run["duration"] == 600.0
    assert (settings.data / run["source_path"]).read_bytes() == b"the video bytes"
    page = client.get(reply["url"])
    assert page.status_code == 200 and "Making your" in page.text
    assert page.headers["x-robots-tag"] == "noindex, nofollow"
    state = client.get(f"/api/runs/{reply['id']}").json()
    assert state["status"] == "queued" and state["clips"] == [] and state["zip"] is None


def test_unknown_or_malformed_links_say_so(client) -> None:
    assert client.get("/r/not-a-real-run-id-at-all").status_code == 404
    assert client.get("/r/..%2F..%2Fweb.db").status_code == 404
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/r/nope/clip/1.mp4").status_code == 404


# --- making ---------------------------------------------------------------------------------


def test_a_run_is_made_and_its_clips_can_be_downloaded(client, settings, fake_render) -> None:
    run_id = upload(client)["id"]
    status = making.make_run(settings, run_id, transcribers=[], find=find(fake_moments(3)))
    assert status == "done" and len(fake_render) == 3
    first = fake_render[0]
    assert first["duration"] == 30 and first["plan"].layout == "letterbox"
    assert first["look"].credit.text == "Made with ClipVault" and first["look"].credit.enabled
    assert first["words"][0].text == "no"

    state = client.get(f"/api/runs/{run_id}").json()
    assert state["status"] == "done" and state["more_found"] == 7 and state["zip"]
    assert [c["status"] for c in state["clips"]] == ["done", "done", "done"]
    headlines = [c["headline"] for c in state["clips"]]
    assert len(set(headlines)) == 3  # no two clips get the same headline
    assert any("Kai Cenat" in h for h in headlines)  # the name that was typed in
    assert not any("chat" in h.lower() for h in headlines)  # an upload has no chat

    clip = state["clips"][0]
    assert client.get(clip["video"]).content == b"phone version"
    download = client.get(clip["download"])
    assert "attachment" in download.headers["content-disposition"]
    assert client.get(clip["thumb"]).content == b"jpeg"
    archive = zipfile.ZipFile(BytesIO(client.get(state["zip"]).content))
    assert len(archive.namelist()) == 3 and archive.namelist()[0].startswith("ClipVault 1 - ")

    conn = store.open_store(settings)
    run = store.get_run(conn, run_id)
    conn.close()
    assert run["source_path"] is None  # the upload is deleted once the clips are made
    assert not list(store.run_dir(settings, run_id).glob("source.*"))


def test_without_a_name_headlines_say_bro() -> None:
    look = making.web_look()
    options = making.headline_options(
        "no way! what just happened", "en", None, look, clip_key="r:1"
    )
    assert options[0].text and not any("chat" in o.text.lower() for o in options)
    assert any(o.text.startswith("Bro") for o in options)


def test_a_restart_picks_up_where_the_run_stopped(client, settings, fake_render) -> None:
    run_id = upload(client)["id"]
    calls: list = []

    def crash(conn, settings, run_id, n, source, info, look):
        if n == 2:
            raise KeyboardInterrupt  # the server stops while the second clip is made
        making.render_clip(conn, settings, run_id, n, source, info, look)

    with pytest.raises(KeyboardInterrupt):
        making.make_run(settings, run_id, transcribers=[], find=find(fake_moments(3), calls),
                        render=crash)  # fmt: skip
    conn = store.open_store(settings)
    assert store.get_run(conn, run_id)["status"] == "making"
    assert store.requeue_interrupted(conn) == 1
    conn.close()

    making.make_run(settings, run_id, transcribers=[], find=find(fake_moments(3), calls))
    assert len(calls) == 1  # the moments were not looked for again
    made = [kw["dest"].name for kw in fake_render]
    assert made.count("clip1.mp4") == 1  # a finished clip is not made again
    assert sorted(set(made)) == ["clip1.mp4", "clip2.mp4", "clip3.mp4"]


def test_a_run_with_no_usable_clip_fails_politely(client, settings, monkeypatch) -> None:
    monkeypatch.setattr(making, "find_cam", lambda source, info, look: None)
    run_id = upload(client)["id"]
    assert making.make_run(settings, run_id, transcribers=[], find=find([])) == "failed"
    assert "couldn't find a moment" in client.get(f"/api/runs/{run_id}").json()["error"]

    other = upload(client)["id"]
    monkeypatch.setattr(engine, "make_vertical",
                        lambda **kw: (_ for _ in ()).throw(RenderError("FFmpeg")))  # fmt: skip
    assert making.make_run(settings, other, transcribers=[], find=find(fake_moments(2))) == "failed"
    state = client.get(f"/api/runs/{other}").json()
    assert [c["status"] for c in state["clips"]] == ["failed", "failed"]
    assert state["zip"] is None


# --- the worker and expiry ----------------------------------------------------------------------


def test_the_worker_makes_waiting_runs_and_retries_a_crash(client, settings) -> None:
    first = upload(client)["id"]
    second = upload(client)["id"]
    made = []

    def make(settings, run_id):
        made.append(run_id)
        if run_id == second:
            raise RuntimeError("out of memory")
        return "done"

    worker = Worker(settings, make=make)
    assert worker.run_once() and worker.run_once()
    assert made == [first, second]
    conn = store.open_store(settings)
    assert store.get_run(conn, second)["status"] == "queued"  # it will be tried again
    conn.execute("UPDATE runs SET attempts = ? WHERE id = ?", (store.MAX_ATTEMPTS, second))
    conn.close()
    worker._stop.set()  # don't wait between tries in the test
    assert worker.run_once()
    conn = store.open_store(settings)
    assert store.get_run(conn, second)["status"] == "failed"
    conn.close()
    assert worker.run_once() is False  # nothing left


def test_expired_runs_lose_every_file_and_say_so(client, settings, fake_render) -> None:
    run_id = upload(client)["id"]
    making.make_run(settings, run_id, transcribers=[], find=find(fake_moments(2)))
    folder = store.run_dir(settings, run_id)
    assert (folder / "clip1.mp4").is_file()
    outside = settings.data / "keep-me.txt"
    outside.write_text("not the website's")
    abandoned = settings.incoming_dir / "old.part"
    abandoned.write_bytes(b"half an upload")

    conn = store.open_store(settings)
    assert store.expire(conn, settings) == 0  # not yet
    assert store.expire(conn, settings, now=utc_now() + timedelta(hours=25)) == 1
    conn.close()
    assert not folder.exists() and outside.is_file()
    assert store.clear_incoming(settings, older_than_s=0, now=utc_now() + timedelta(hours=1)) == 1
    page = client.get(f"/r/{run_id}")
    assert page.status_code == 410 and "deleted" in page.text
    assert client.get(f"/r/{run_id}/clip/1.mp4").status_code == 404


def test_the_website_starts_and_stops_its_worker(settings, probe) -> None:
    with TestClient(create_app(settings)) as running:
        assert running.get("/healthz").json() == {"status": "ok"}
        assert any(t.is_alive() for t in running.app.state.worker._threads)
    assert not any(t.is_alive() for t in running.app.state.worker._threads)


def test_download_names_and_settings() -> None:
    assert (
        download_name(2, "Bro was NOT ready for this 😳")
        == "ClipVault 2 - Bro was NOT ready for this 😳.mp4"
    )
    assert download_name(1, 'a/b:c*"d?') == "ClipVault 1 - abcd.mp4"
    assert download_name(3, None) == "ClipVault 3.mp4"


def test_settings_come_from_the_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLIPVAULT_WEB_DATA", str(tmp_path / "d"))
    monkeypatch.setenv("CLIPVAULT_WEB_CLIPS_PER_RUN", "4")
    monkeypatch.setenv("CLIPVAULT_WEB_GROQ_API_KEY", "test-key")
    settings = from_env()
    assert settings.data == (tmp_path / "d").resolve() and settings.clips_per_run == 4
    assert settings.groq_api_key == "test-key" and "test-key" not in repr(settings)
    assert json.dumps({"ok": True})  # the module imports cleanly without a server


# --- W2: nudges and counting --------------------------------------------------------------------


def nudges_in(html: str) -> int:
    return html.count("data-nudge=")


def test_each_screen_shows_at_most_one_nudge(client, settings, fake_render) -> None:
    assert nudges_in(client.get("/").text) == 1
    run_id = upload(client)["id"]
    waiting = client.get(f"/api/runs/{run_id}").json()
    assert waiting["nudge"] == "working"
    making.make_run(settings, run_id, transcribers=[], find=find(fake_moments(2)))
    done = client.get(f"/api/runs/{run_id}").json()
    assert done["nudge"] == "more" and done["more_found"] == 7
    assert nudges_in(client.get(f"/r/{run_id}").text) == 0  # the page script adds just one card
    assert [c["steps"] for c in done["clips"]] == [4, 4]


def test_events_are_counted_for_a_test_run(client, settings, fake_render) -> None:
    from clipvault_web import events

    client.get("/")
    run_id = upload(client)["id"]
    client.get(f"/r/{run_id}")
    making.make_run(settings, run_id, transcribers=[], find=find(fake_moments(2)))
    state = client.get(f"/api/runs/{run_id}").json()
    client.get(state["clips"][0]["download"])
    client.get(state["zip"])
    client.post("/api/event", json={"name": "nudge_shown", "detail": "more"})
    client.post("/api/event", json={"name": "run_started", "detail": "fake"})  # not from pages
    client.post("/api/event", content=b"not json")
    reply = client.get("/go/app?from=more", follow_redirects=False)
    assert reply.status_code == 303 and reply.headers["location"] == settings.app_download_url
    client.get("/go/app?from=somewhere-else", follow_redirects=False)

    conn = store.open_store(settings)
    counted = {(name, detail): total for name, detail, total in events.summary(conn)}
    conn.close()
    assert counted == {
        ("clip_downloaded", ""): 1,
        ("nudge_clicked", "more"): 1,
        ("nudge_clicked", "other"): 1,
        ("nudge_shown", "more"): 1,
        ("page_view", "home"): 1,
        ("page_view", "run"): 1,
        ("run_finished", "done"): 1,
        ("run_started", "upload"): 1,
        ("zip_downloaded", ""): 1,
    }
