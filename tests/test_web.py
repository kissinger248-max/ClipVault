from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from clipvault import alerts, db
from clipvault import config as cfg
from clipvault.web import auth
from tests.helpers import add_clip, add_streamer

PASSWORD = "correct horse battery"


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
    for step in range(1, 7):
        page = client.get(f"/setup/{step}")
        assert page.status_code == 200 and f"Step {step} of 6" in page.text


def test_dashboard_shows_phone_versions_with_the_original_one_tap_away(
    client, home, library
) -> None:
    login(client)
    finish_setup(home)
    conn = db.open_db(home / "clipvault.db")
    add_streamer(conn, "alpha", display_name="Alpha")
    saved_clip(conn, library, title="Huge <b>play</b>", view_count=1234)
    conn.execute(
        "INSERT INTO transcripts (clip_id, text) VALUES ('clip1', 'no way that was insane')"
    )
    # Not edited yet: listed as "being edited", not shown as a clip.
    home_page = client.get("/").text
    assert "Running" in home_page and "Still being edited" in home_page
    assert "kind=vertical" not in home_page  # nothing edited to show yet
    assert "1 more clip is still being edited" in client.get("/clips").text
    original_only = client.get("/clips/clip1").text
    assert "still being made" in original_only and "Download Original" in original_only

    vertical = library / "alpha/2026-11/vertical/clip1_9x16.mp4"
    vertical.parent.mkdir()
    vertical.write_bytes(b"phone video")
    conn.execute(
        "INSERT INTO renders (clip_id, layout, path, headline, qa_passed) "
        "VALUES ('clip1', 'letterbox', 'alpha/2026-11/vertical/clip1_9x16.mp4', 'Huge play', 1)"
    )
    conn.close()
    home_page = client.get("/").text
    assert "/clips/clip1" in home_page and "still being edited" not in home_page
    assert "kind=vertical" in home_page  # the card shows the phone version
    listing = client.get("/clips", params={"q": "insane"})
    assert "<mark>insane</mark>" in listing.text and "1 clip saying" in listing.text
    assert "0 clips" in client.get("/clips", params={"q": "zebra"}).text
    page = client.get("/clips/clip1").text
    assert "phone-player" in page and "Save Video" in page and "See Original" in page
    assert "Huge &lt;b&gt;play&lt;/b&gt;" in page  # titles are escaped
    assert "no way that was insane" in page
    assert client.get("/media/clip1", params={"kind": "vertical"}).content == b"phone video"
    original = client.get("/clips/clip1", params={"view": "original"}).text
    assert "See Phone Version" in original and "Download Original" in original
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
    page = client.get("/setup/6").text  # the code to scan is on the phone step
    assert "<svg" in page and "Send Test" in client.get("/settings").text


def test_estimate_grows_with_streamers_and_quality() -> None:
    from clipvault.web.settings_routes import estimate_gb_per_month

    one = [{"save_enabled": 1, "top_n": 5, "min_views": None, "vod_moments": 3}]
    small = estimate_gb_per_month(one, "720p")
    assert 10 < small < 11.5  # mostly the phone versions
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


def test_look_is_saved_and_previews_are_cached(client, home, library, monkeypatch) -> None:
    from clipvault.render import engine

    login(client)
    token = csrf(client)
    client.post(
        "/settings/look",
        data={"csrf": token, "style": "outline", "accent": "#ff0055", "size": "80"},
    )
    look = cfg.load_config(home / "config.yaml").look
    assert (look.headline_style, look.accent, look.caption_size) == ("outline", "#FF0055", 80)
    bad = client.post("/settings/look", data={"csrf": token, "size": "200"})
    assert "Not+saved" in bad.headers["location"]
    # No saved clip yet: a grey placeholder instead of an error.
    assert client.get("/look/preview/letterbox.jpg").headers["content-type"] == "image/svg+xml"
    assert client.get("/look/preview/nonsense.jpg").headers["content-type"] == "image/svg+xml"

    conn = db.open_db(home / "clipvault.db")
    add_streamer(conn, "alpha")
    saved_clip(conn, library)
    conn.close()
    drawn: list[tuple] = []

    def fake_frame(conn, config, look, layout, dest):
        drawn.append((layout, look.headline_style, look.accent, look.caption.size))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\xff\xd8jpeg")
        return dest

    monkeypatch.setattr(engine, "preview_frame", fake_frame)
    query = "style=box&accent=%2300FF00&size=64"
    first = client.get(f"/look/preview/letterbox.jpg?{query}")
    again = client.get(f"/look/preview/letterbox.jpg?{query}")
    assert first.headers["content-type"] == "image/jpeg" and again.content == first.content
    assert drawn == [("letterbox", "box", "#00FF00", 64)]  # made once, then served from cache
    assert "Look of phone videos" in client.get("/settings").text


def test_twitch_login_from_the_panel_shows_the_code(client, monkeypatch) -> None:
    import threading

    from clipvault.twitch import user_auth

    login(client)
    approved = threading.Event()
    login_code = user_auth.DeviceLogin(
        "dev", "WXYZ-9876", "https://www.twitch.tv/activate", 5, 1800
    )
    monkeypatch.setattr(user_auth, "start_login", lambda http: login_code)
    monkeypatch.setattr(user_auth, "finish_login", lambda http, code: approved.wait(5) or None)
    client.post("/settings/twitch-login", data={"csrf": csrf(client)})
    page = client.get("/settings").text
    assert "WXYZ-9876" in page and "twitch.tv/activate" in page
    future = "2099-01-01T00:00:00Z"
    user_auth.store(user_auth.UserToken("a", "r", future, "42", "viewer"))
    approved.set()
    for _ in range(50):
        if client.app.state.panel.twitch_login is None:
            break
        threading.Event().wait(0.05)
    assert "Logged in. Used only to read chat" in client.get("/settings").text
    client.post("/settings/twitch-logout", data={"csrf": csrf(client)})
    assert user_auth.stored() is None


def test_phone_layout_can_be_chosen_per_streamer(client, home) -> None:
    login(client)
    conn = db.open_db(home / "clipvault.db")
    streamer_id = add_streamer(conn, "alpha")
    token = csrf(client)
    form = {"csrf": token, "save": "1", "top_n": "5", "vod_moments": "3", "min_views": ""}
    client.post(f"/streamers/{streamer_id}/update", data={**form, "layout": "native"})
    assert conn.execute("SELECT layout_override FROM streamers").fetchone()[0] == "native"
    client.post(f"/streamers/{streamer_id}/update", data={**form, "layout": ""})
    assert conn.execute("SELECT layout_override FROM streamers").fetchone()[0] is None
    client.post(f"/streamers/{streamer_id}/update", data={**form, "layout": "nonsense"})
    assert conn.execute("SELECT layout_override FROM streamers").fetchone()[0] is None
    conn.close()


def test_every_page_uses_the_new_design_and_shows_the_problem_badge(client, home) -> None:
    login(client)
    finish_setup(home)
    conn = db.open_db(home / "clipvault.db")
    alerts.raise_alert(conn, "storage", "Disk almost full")
    conn.close()
    for path in ("/", "/clips", "/streamers", "/settings", "/alerts", "/setup/3"):
        text = client.get(path).text
        assert "/static/tokens.css" in text and "/static/motion.css" in text, path
        assert "style.css" not in text, path
    home_page = client.get("/").text
    assert 'aria-label="1 problem"' in home_page and "Needs you" in home_page
    assert home_page.count('class="count-badge"') == 2  # sidebar and phone tab bar
    assert 'name="csrf"' in home_page
    assert client.get("/static/style.css").status_code == 404


def test_inter_is_bundled_with_its_licence(client) -> None:
    tokens = client.get("/static/tokens.css").text
    assert "font-family: Inter" in tokens
    for name in ("Inter-latin.woff2", "Inter-latin-ext.woff2"):
        assert f"fonts/{name}" in tokens
        reply = client.get(f"/static/fonts/{name}")
        assert reply.status_code == 200 and reply.content[:4] == b"wOF2", name
    assert "SIL Open Font License" in client.get("/static/fonts/OFL-Inter.txt").text


def test_static_links_change_when_the_files_change(tmp_path) -> None:
    from clipvault.web.server import _asset_version

    (tmp_path / "fonts").mkdir()
    (tmp_path / "panel.css").write_text("a { color: red; }", encoding="utf-8")
    (tmp_path / "fonts" / "x.woff2").write_bytes(b"wOF2")
    before = _asset_version(tmp_path)
    assert before == _asset_version(tmp_path)
    (tmp_path / "panel.css").write_text("a { color: blue; }", encoding="utf-8")
    assert _asset_version(tmp_path) != before
