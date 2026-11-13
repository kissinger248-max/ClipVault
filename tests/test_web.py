from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipvault import alerts, db
from clipvault import config as cfg
from clipvault.web import auth
from clipvault.web.server import create_app
from tests.helpers import add_clip, add_streamer

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def no_lan_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clipvault.web.settings_routes.lan_address", lambda: "192.168.1.5")


@pytest.fixture
def library(home: Path) -> Path:
    root = home / "library"
    root.mkdir()
    (root / ".clipvault-library").write_text("ClipVault library\n")
    cfg.save_settings({"library_root": str(root)}, home / "config.yaml")
    return root


@pytest.fixture
def client(home: Path, library: Path) -> TestClient:
    db.open_db(home / "clipvault.db").close()
    return TestClient(create_app(home), follow_redirects=False)


def login(client: TestClient) -> TestClient:
    if not auth.has_password():
        auth.set_password(PASSWORD)
    response = client.post("/login", data={"password": PASSWORD, "next": "/"})
    assert response.status_code == 303
    return client


def csrf(client: TestClient, path: str = "/settings") -> str:
    html = client.get(path).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def finish_setup(home: Path) -> None:
    conn = db.open_db(home / "clipvault.db")
    alerts.set_setting(conn, "setup_done", "1")
    conn.close()


def saved_clip(conn, library: Path, clip_id: str = "clip1", **kw) -> Path:
    relpath = f"alpha/2026-11/{clip_id}.mp4"
    video = library / relpath
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    for suffix in (".json", ".srt", ".words.json"):
        video.with_suffix(suffix).write_text("{}")
    add_clip(conn, clip_id, state="transcribed", file_path=relpath,
             sidecar_path=relpath[:-4] + ".json", transcript_path=relpath[:-4] + ".srt",
             fetched_at="2026-11-12T10:00:00Z", **kw)  # fmt: skip
    return video


# --- security ---------------------------------------------------------------------


def test_first_password_is_chosen_on_this_computer_only(client) -> None:
    assert client.get("/").headers["location"] == "/setup-password"
    remote = TestClient(client.app, follow_redirects=False, client=("192.168.1.20", 5000))
    refused = remote.post("/setup-password", data={"password": PASSWORD, "again": PASSWORD})
    assert refused.status_code == 403 and not auth.has_password()
    short = client.post("/setup-password", data={"password": "short", "again": "short"})
    assert "at least 8" in short.text and not auth.has_password()
    done = client.post("/setup-password", data={"password": PASSWORD, "again": PASSWORD})
    assert done.status_code == 303 and auth.COOKIE in done.cookies
    assert auth.check_password(PASSWORD) and PASSWORD not in (cfg.get_secret("panel_password_hash"))


def test_pages_need_login_and_wrong_passwords_are_refused(client, monkeypatch) -> None:
    import clipvault.web.server as server

    monkeypatch.setattr(server, "FAILED_LOGIN_DELAY_S", 0)
    auth.set_password(PASSWORD)
    assert client.get("/clips").headers["location"] == "/login?next=/clips"
    assert "Wrong password" in client.post("/login", data={"password": "nope"}).text
    client.cookies.set(auth.COOKIE, "123.abc.forged")
    assert client.get("/clips").status_code == 303
    client.cookies.clear()
    login(client)
    assert client.get("/clips").status_code == 200


def test_posts_without_the_form_token_are_refused(client, home) -> None:
    login(client)
    finish_setup(home)
    assert client.post("/pause").status_code == 403
    assert client.post("/pause", data={"csrf": "wrong"}).status_code == 403
    assert client.post("/pause", data={"csrf": csrf(client)}).status_code == 303
    conn = db.open_db(home / "clipvault.db")
    assert alerts.is_paused(conn)
    conn.close()


def test_login_redirect_cannot_leave_the_site(client) -> None:
    auth.set_password(PASSWORD)
    response = client.post("/login", data={"password": PASSWORD, "next": "//evil.example"})
    assert response.headers["location"] == "/"


def test_session_expires_and_password_change_logs_everyone_out() -> None:
    session = auth.make_session(now=1000)
    assert auth.session_valid(session, now=2000)
    assert not auth.session_valid(session, now=1000 + 31 * 86400)
    auth.set_password(PASSWORD)
    fresh = auth.make_session()
    auth.set_password("another password")
    assert not auth.session_valid(fresh)


# --- pages --------------------------------------------------------------------------


def test_first_visit_after_login_goes_to_the_wizard(client) -> None:
    login(client)
    assert client.get("/").headers["location"] == "/setup/1"
    for step in range(1, 6):
        page = client.get(f"/setup/{step}")
        assert page.status_code == 200 and f"Step {step} of 5" in page.text


def test_home_clips_and_clip_pages_show_saved_clips(client, home, library) -> None:
    login(client)
    finish_setup(home)
    conn = db.open_db(home / "clipvault.db")
    add_streamer(conn, "alpha", display_name="Alpha")
    saved_clip(conn, library, title="Huge <b>play</b>", view_count=1234)
    conn.execute(
        "INSERT INTO transcripts (clip_id, text) VALUES ('clip1', 'no way that was insane')"
    )
    conn.close()
    home_page = client.get("/")
    assert "Running" in home_page.text and "Huge &lt;b&gt;play&lt;/b&gt;" in home_page.text
    listing = client.get("/clips", params={"q": "insane"})
    assert "<mark>insane</mark>" in listing.text and "1 clip saying" in listing.text
    assert "0 clips" in client.get("/clips", params={"q": "zebra"}).text
    page = client.get("/clips/clip1")
    assert page.status_code == 200 and "no way that was insane" in page.text
    assert client.get("/media/clip1").content == b"video"
    assert client.get("/clips/missing").status_code == 404


def test_star_then_delete_removes_every_file_of_the_clip(client, home, library) -> None:
    login(client)
    finish_setup(home)
    conn = db.open_db(home / "clipvault.db")
    add_streamer(conn, "alpha")
    video = saved_clip(conn, library)
    vertical = library / "alpha/2026-11/vertical/clip1_9x16.mp4"
    vertical.parent.mkdir()
    vertical.write_bytes(b"v")
    conn.execute("INSERT INTO renders (clip_id, layout, path) VALUES ('clip1', 'letterbox', ?)",
                 ("alpha/2026-11/vertical/clip1_9x16.mp4",))  # fmt: skip
    token = csrf(client)
    client.post("/clips/clip1/star", data={"csrf": token})
    refused = client.post("/clips/clip1/delete", data={"csrf": token})
    assert "Starred" in refused.headers["location"] and video.exists()
    client.post("/clips/clip1/star", data={"csrf": token})
    client.post("/clips/clip1/delete", data={"csrf": token})
    leftovers = [
        p.name for p in library.rglob("*") if p.is_file() and p.name != ".clipvault-library"
    ]
    assert leftovers == []
    row = conn.execute("SELECT removed_reason FROM clips WHERE id = 'clip1'").fetchone()
    assert row[0] == "user"
    conn.close()


def test_streamers_can_be_added_changed_and_removed(client, home) -> None:
    login(client)
    token = csrf(client)
    added = client.post("/streamers/add", data={"csrf": token, "login": "https://twitch.tv/NewOne"})
    assert "newone+added" in added.headers["location"].lower()
    conn = db.open_db(home / "clipvault.db")
    streamer_id = conn.execute("SELECT id FROM streamers WHERE login = 'newone'").fetchone()[0]
    client.post(
        f"/streamers/{streamer_id}/update",
        data={"csrf": token, "top_n": "7", "vod_moments": "0", "min_views": ""},
    )
    row = conn.execute("SELECT save_enabled, top_n, vod_moments FROM streamers").fetchone()
    assert tuple(row) == (0, 7, 0)
    bad = client.post(f"/streamers/{streamer_id}/update", data={"csrf": token, "top_n": "0"})
    assert "between" in bad.headers["location"]
    assert "Streamers" in client.get("/streamers").text
    client.post(f"/streamers/{streamer_id}/remove", data={"csrf": token})
    assert conn.execute("SELECT count(*) FROM streamers").fetchone()[0] == 0
    conn.close()


def test_a_streamer_with_clips_is_only_switched_off(client, home, library) -> None:
    login(client)
    conn = db.open_db(home / "clipvault.db")
    add_streamer(conn, "alpha")
    saved_clip(conn, library)
    client.post("/streamers/1/remove", data={"csrf": csrf(client)})
    assert conn.execute("SELECT save_enabled FROM streamers").fetchone()[0] == 0
    conn.close()


def test_settings_are_checked_and_saved(client, home) -> None:
    login(client)
    token = csrf(client)
    client.post("/settings/schedule",
                data={"csrf": token, "interval": "45", "timezone": "Europe/Berlin"})  # fmt: skip
    config = cfg.load_config(home / "config.yaml")
    assert config.discover_interval_min == 45 and config.timezone == "Europe/Berlin"
    assert not config.transcription.enabled
    bad = client.post(
        "/settings/schedule", data={"csrf": token, "interval": "5", "timezone": "Mars"}
    )
    assert "Not+saved" in bad.headers["location"]
    assert cfg.load_config(home / "config.yaml").timezone == "Europe/Berlin"
    client.post("/settings/phone-access", data={"csrf": token, "allow": "1"})
    assert cfg.load_config(home / "config.yaml").panel.host == "0.0.0.0"


def test_twitch_keys_are_stored_but_never_shown(client) -> None:
    login(client)
    client.post(
        "/settings/twitch",
        data={"csrf": csrf(client), "client_id": "cid123", "client_secret": "sec456"},
    )
    assert cfg.get_secret("twitch_client_id") == "cid123"
    page = client.get("/settings").text
    assert "cid123" not in page and "sec456" not in page and "stored" in page


def test_notifications_setup_shows_a_qr_code(client) -> None:
    login(client)
    client.post("/settings/ntfy", data={"csrf": csrf(client)})
    page = client.get("/settings").text
    assert "<svg" in page and cfg.get_secret("ntfy_topic") in page


def test_estimate_grows_with_streamers_and_quality() -> None:
    from clipvault.web.settings_routes import estimate_gb_per_month

    one = [{"save_enabled": 1, "top_n": 5, "min_views": None, "vod_moments": 3}]
    small = estimate_gb_per_month(one, "720p")
    assert 2 < small < 3
    assert estimate_gb_per_month(one * 2, "1080p") > 2 * small
    assert estimate_gb_per_month([{**one[0], "save_enabled": 0}], "720p") == 0


def test_make_vertical_copy_runs_in_the_background(client, home, library, monkeypatch) -> None:
    import threading

    from clipvault.render import engine

    login(client)
    conn = db.open_db(home / "clipvault.db")
    add_streamer(conn, "alpha")
    saved_clip(conn, library)
    conn.close()
    done = threading.Event()
    calls: list[tuple] = []

    def fake_render(conn, config, clip_id, layout=None, **kw):
        calls.append((clip_id, layout))
        done.set()
        raise RuntimeError("no FFmpeg in tests")

    monkeypatch.setattr(engine, "render_clip", fake_render)
    response = client.post("/clips/clip1/vertical", data={"csrf": csrf(client), "layout": "split"})
    assert "half+a+minute" in response.headers["location"]
    assert done.wait(5) and calls == [("clip1", "split")]
    state = client.app.state.panel
    for _ in range(50):
        if "clip1" not in state.rendering:
            break
        threading.Event().wait(0.05)
    assert "no FFmpeg in tests" in client.get("/clips/clip1").text


def test_reset_password_command_forgets_the_password(capsys) -> None:
    from clipvault.__main__ import main

    auth.set_password(PASSWORD)
    assert main(["panel", "reset-password"]) == 0
    assert not auth.has_password() and cfg.get_secret("panel_session_key") is None
