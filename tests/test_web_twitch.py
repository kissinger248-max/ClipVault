"""ClipVault Web, W3: Twitch links, search, the download switch, opt-outs, sign-in, reports.

A fake Twitch stands in for Helix and the OAuth endpoints; nothing goes on the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from clipvault import config as cfg
from clipvault.process.transcribe import Transcript, Word
from clipvault.render import engine, ffmpeg, qa
from clipvault.render.ffmpeg import MediaInfo
from clipvault.twitch.api import TwitchClient
from clipvault_web import accounts, making, store, twitch_making, twitch_web
from clipvault_web import moments as finder
from clipvault_web.server import create_app
from clipvault_web.settings import WebSettings
from clipvault_web.twitch_web import Link, LinkProblem, parse_link

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def clip(cid, views, duration=30.0, video="v1", offset=100.0, user="1", title=None, days=1):
    return {
        "id": cid, "url": f"https://clips.twitch.tv/{cid}", "embed_url": f"https://clips.twitch.tv/embed?clip={cid}",
        "broadcaster_id": user, "video_id": video, "vod_offset": offset, "duration": duration,
        "view_count": views, "title": title or f"Clip {cid}",
        "created_at": (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }  # fmt: skip


class FakeTwitch:
    """The Helix calls the website makes, answered from memory."""

    def __init__(self) -> None:
        self.users = {"kaicenat": {"id": "1", "login": "kaicenat", "display_name": "KaiCenat"},
                      "quiet": {"id": "2", "login": "quiet", "display_name": "Quiet"}}  # fmt: skip
        self.clips = {
            "1": [clip("a", 9000), clip("b", 5000, offset=105.0), clip("c", 4000, offset=900.0),
                  clip("d", 3000, duration=8.0, offset=2000.0), clip("e", 2000, offset=3000.0),
                  clip("f", 1000, video="v2", offset=50.0)],
            "2": [],
        }  # fmt: skip
        self.live = {"1"}
        self.calls: list[str] = []

    def search_channels(self, query, first=8):
        self.calls.append("search")
        return [{"broadcaster_login": u["login"], "display_name": u["display_name"],
                 "id": u["id"], "is_live": u["id"] in self.live, "thumbnail_url": ""}
                for u in self.users.values() if query in u["login"]]  # fmt: skip

    def get_users(self, *, logins=(), ids=()):
        return [u for u in self.users.values() if u["login"] in logins or u["id"] in ids]

    def iter_clips(self, user_id, started, ended, **kw):
        return iter(self.clips.get(user_id, []))

    def get_clips(self, ids):
        return [c for cs in self.clips.values() for c in cs if c["id"] in ids]

    def get_videos(self, ids):
        return [{"id": "v1", "user_id": "1", "user_login": "kaicenat", "user_name": "KaiCenat",
                 "created_at": "2026-11-18T10:00:00Z", "duration": "2h0m0s", "title": "GTA day",
                 "url": "https://www.twitch.tv/videos/v1"}] if "v1" in ids else []  # fmt: skip

    def get_live_stream_ids(self, ids):
        return {i: "s" for i in ids if i in self.live}


@pytest.fixture
def settings(tmp_path: Path) -> WebSettings:
    return WebSettings(data=tmp_path / "web", twitch_client_id="web-app", runs_per_day=100,
                       twitch_client_secret="web-secret", one_run_at_a_time=False,
                       public_url="https://clips.example")  # fmt: skip


@pytest.fixture
def fake() -> FakeTwitch:
    return FakeTwitch()


@pytest.fixture
def client(settings, fake) -> TestClient:
    return TestClient(create_app(settings, start_worker=False, twitch=fake), follow_redirects=False)


@pytest.fixture
def fake_media(monkeypatch: pytest.MonkeyPatch) -> list:
    """FFmpeg, the speech model and the renderer stand in for the real ones."""
    made: list = []
    monkeypatch.setattr(
        ffmpeg, "probe", lambda path: MediaInfo(1280, 720, 30.0, 30.0, True, "h264")
    )

    def listen(video, start, end, providers):
        words = (Word("no", 1.0, 1.2), Word("way!", 1.2, 1.5))
        return Transcript("no way!", words, "en", "fake")

    def make_vertical(**kw):
        made.append(kw)
        kw["dest"].write_bytes(b"phone version")
        report = qa.Report()
        report.add("duration", True, "30 s")
        return report

    monkeypatch.setattr(finder, "listen", listen)
    monkeypatch.setattr(engine, "make_vertical", make_vertical)
    monkeypatch.setattr(
        ffmpeg, "extract_frame", lambda path, s, dest: dest.write_bytes(b"j") and dest
    )
    monkeypatch.setattr(twitch_making, "_find_cam", lambda conn, settings, run_id, look: None)
    return made


def downloader(log: list):
    def fetch(run, row, dest):
        log.append(row["source_url"])
        dest.write_bytes(b"twitch video")
        return dest

    return fetch


def start(client: TestClient, link: str) -> dict:
    reply = client.post("/api/twitch", json={"link": link})
    return {"status": reply.status_code, **reply.json()}


def run_state(client: TestClient, run_id: str) -> dict:
    return client.get(f"/api/runs/{run_id}").json()


# --- links and choosing clips --------------------------------------------------------------------


def test_pasted_links_are_understood() -> None:
    assert parse_link("https://clips.twitch.tv/FunnyClipSlug-abc123") == Link(
        "clip", "FunnyClipSlug-abc123"
    )
    assert parse_link("twitch.tv/kaicenat/clip/Slug_123-xyz") == Link("clip", "Slug_123-xyz")
    assert parse_link("https://www.twitch.tv/videos/2932113307?t=1h") == Link("vod", "2932113307")
    assert parse_link("https://m.twitch.tv/KaiCenat") == Link("channel", "kaicenat")
    assert parse_link("  xQc ") == Link("name", "xqc")
    for bad in ("", "https://youtube.com/watch?v=x", "twitch.tv/directory", "a b c"):
        with pytest.raises(LinkProblem):
            parse_link(bad)


def test_a_channel_gets_its_most_watched_usable_clips(fake) -> None:
    plan = twitch_web.plan_run(fake, Link("channel", "kaicenat"), 3, NOW)
    assert plan.kind == "twitch_channel" and plan.is_live and plan.name == "KaiCenat"
    # b is the same moment as a (5 s apart in the same VOD); d is too short for a Short.
    assert [i.slug for i in plan.items] == ["a", "c", "e"]
    assert plan.more_found == 1  # f
    found = twitch_web.plan_run(fake, Link("name", "kai"), 3, NOW)
    assert found.login == "kaicenat"
    with pytest.raises(LinkProblem, match="no clips"):
        twitch_web.plan_run(fake, Link("channel", "quiet"), 3, NOW)
    with pytest.raises(LinkProblem, match="no Twitch channel"):
        twitch_web.plan_run(fake, Link("channel", "nobody"), 3, NOW)


def test_a_clip_link_starts_with_that_clip_and_a_vod_uses_its_own_clips(fake) -> None:
    plan = twitch_web.plan_run(fake, Link("clip", "c"), 3, NOW)
    assert plan.kind == "twitch_clip" and [i.slug for i in plan.items] == ["c", "a", "e"]
    vod = twitch_web.plan_run(fake, Link("vod", "v1"), 5, NOW)
    assert vod.kind == "twitch_vod" and "f" not in [i.slug for i in vod.items]
    assert vod.vod_length == 7200 and vod.title == "GTA day"
    with pytest.raises(LinkProblem):
        twitch_web.plan_run(fake, Link("clip", "nope"), 3, NOW)


def test_only_the_websites_own_twitch_app_is_used(settings, monkeypatch) -> None:
    def no_keychain(name):
        raise AssertionError(f"the desktop app's secret {name} was read")

    monkeypatch.setattr(cfg, "get_secret", no_keychain)
    monkeypatch.setattr(cfg, "own_twitch_keys", lambda: no_keychain("twitch keys"))
    client = twitch_web.build_client(settings)
    assert isinstance(client, TwitchClient) and client._client_id == "web-app"
    client.close()
    with pytest.raises(LinkProblem):
        twitch_web.build_client(WebSettings(data=settings.data))


# --- the download switch ------------------------------------------------------------------------


def test_with_downloads_off_twitch_links_show_twitchs_own_players(
    client, settings, fake_media
) -> None:
    got: list = []
    reply = start(client, "https://www.twitch.tv/kaicenat")
    assert reply["status"] == 200
    state = run_state(client, reply["id"])
    assert state["status"] == "done" and state["embed_only"] and state["nudge"] == "embed"
    assert [c["status"] for c in state["clips"]] == ["embed"] * 4  # a, c, e, f
    assert "parent=testserver" in state["clips"][0]["embed"] and state["zip"] is None
    assert making.make_run(settings, reply["id"], transcribers=[], fetch=downloader(got)) == "done"
    assert got == []  # nothing was downloaded


def test_with_downloads_on_the_moments_are_saved_captioned_and_made(client, settings, fake_media):
    conn = store.open_store(settings)
    store.set_downloads(conn, True, "test")
    conn.close()
    got: list = []
    run_id = start(client, "kaicenat")["id"]
    assert run_state(client, run_id)["status"] == "queued"
    conn = store.open_store(settings)
    assert store.get_run(conn, run_id)["median_views"] == 3500  # every clip looked at, not 4
    conn.close()
    assert making.make_run(settings, run_id, transcribers=[], fetch=downloader(got)) == "done"
    assert sorted(got) == [f"https://clips.twitch.tv/{c}" for c in ("a", "c", "e", "f")]
    state = run_state(client, run_id)
    assert [c["status"] for c in state["clips"]] == ["done"] * 4 and state["zip"]
    headlines = [c["headline"] for c in state["clips"]]
    assert len(set(headlines)) == 4
    assert fake_media[0]["words"][0].text == "no" and fake_media[0]["duration"] == 30.0
    folder = store.run_dir(settings, run_id)
    assert not list(folder.glob("src*.mp4"))  # the downloads are gone, the phone versions stay
    assert len(list(folder.glob("clip*.mp4"))) == 4


def test_turning_downloads_off_mid_run_stops_downloading(client, settings, fake_media) -> None:
    conn = store.open_store(settings)
    store.set_downloads(conn, True)
    run_id = start(client, "kaicenat")["id"]
    store.set_downloads(conn, False, "a takedown wave")
    conn.close()
    got: list = []
    making.make_run(settings, run_id, transcribers=[], fetch=downloader(got))
    assert got == [] and run_state(client, run_id)["embed_only"]


def test_a_downloader_that_keeps_failing_switches_itself_off(client, settings, fake_media) -> None:
    from clipvault.fetch.ytdlp import DownloadError

    conn = store.open_store(settings)
    store.set_downloads(conn, True)
    conn.close()

    def broken(run, row, dest):
        raise DownloadError("HTTP Error 403")

    for _ in range(2):
        run_id = start(client, "kaicenat")["id"]
        making.make_run(settings, run_id, transcribers=[], fetch=broken)
    conn = store.open_store(settings)
    assert not store.downloads_on(conn, settings)
    assert "failed in a row" in store.get_setting(conn, store.DOWNLOADS)
    conn.close()


# --- opting out, search, reports ----------------------------------------------------------------


def test_an_opted_out_channel_is_refused_and_hidden_from_search(client, settings) -> None:
    assert [r["login"] for r in client.get("/api/search?q=kai").json()["results"]] == ["kaicenat"]
    first = start(client, "kaicenat")["id"]
    conn = store.open_store(settings)
    assert store.opt_out(conn, settings, "1", "KaiCenat", "takedown") == 1
    assert store.get_run(conn, first)["status"] == "expired"  # made before: deleted now
    conn.close()
    reply = start(client, "https://www.twitch.tv/kaicenat/clip/Slug_abc123")
    assert reply["status"] in (400, 403)
    refused = start(client, "kaicenat")
    assert refused["status"] == 403 and "asked not to be clipped" in refused["error"]
    assert client.get("/api/search?q=kai").json()["results"] == []  # even from the cache


def test_a_report_is_kept_for_a_person_to_read(client, settings) -> None:
    run_id = start(client, "kaicenat")["id"]
    page = client.get(f"/report?run={run_id}")
    assert page.status_code == 200 and run_id in page.text
    form = {"run": run_id, "reason": "copyright", "details": "Mine", "contact": "me@example.com"}
    sent = client.post("/report", data=form)
    assert "Thank you" in sent.text
    conn = store.open_store(settings)
    row = conn.execute("SELECT * FROM reports").fetchone()
    conn.close()
    assert (row["run_id"], row["reason"], row["contact"]) == (run_id, "copyright", "me@example.com")


# --- signing in -----------------------------------------------------------------------------------


def oauth_transport(scope: list[str], seen: list) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/oauth2/token":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            assert form["grant_type"] == "authorization_code" and form["code"] == "the-code"
            assert form["client_secret"] == "web-secret"
            return httpx.Response(200, json={"access_token": "user-token", "expires_in": 14000,
                                             "scope": scope})  # fmt: skip
        if request.url.path == "/helix/users":
            assert request.headers["Authorization"] == "Bearer user-token"
            return httpx.Response(200, json={"data": [
                {"id": "1", "login": "kaicenat", "display_name": "KaiCenat"}]})  # fmt: skip
        if request.url.path == "/oauth2/revoke":
            return httpx.Response(200)
        if request.url.path == "/helix/clips/downloads":
            assert request.url.params["editor_id"] == "1"
            ids = request.url.params.get_list("clip_id")
            return httpx.Response(200, json={"data": [
                {"clip_id": i, "landscape_download_url": f"https://cdn.example/{i}.mp4",
                 "portrait_download_url": None} for i in ids]})  # fmt: skip
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=b"official clip bytes")
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def sign_in(settings, fake, purpose: str, scope: list[str], seen: list) -> TestClient:
    http = httpx.Client(transport=oauth_transport(scope, seen))
    browser = TestClient(create_app(settings, start_worker=False, twitch=fake, http=http),
                         follow_redirects=False, base_url=settings.public_url)  # fmt: skip
    go = browser.get(f"/auth/twitch?for={purpose}")
    assert go.status_code == 303 and go.headers["location"].startswith(accounts.AUTHORIZE_URL)
    assert (
        "redirect_uri=https%3A%2F%2Fclips.example%2Fauth%2Ftwitch%2Fcallback"
        in go.headers["location"]
    )
    state = go.headers["location"].split("state=")[1].split("&")[0]
    assert browser.get("/auth/twitch/callback?code=the-code&state=wrong").status_code == 400
    back = browser.get(f"/auth/twitch/callback?code=the-code&state={state}")
    assert back.status_code == 303
    return browser


def test_a_streamer_signs_in_to_opt_out(settings, fake) -> None:
    seen: list = []
    browser = sign_in(settings, fake, "optout", [], seen)
    assert ("POST", "/oauth2/revoke") in seen  # no scope needed: the token is dropped at once
    page = browser.get("/optout").text
    assert "Signed in as <b>KaiCenat</b>" in page
    form = page.split('name="csrf" value="')[1].split('"')[0]
    assert browser.post("/optout", data={"action": "block", "csrf": "forged"}).status_code == 303
    conn = store.open_store(settings)
    assert not store.opted_out(conn, "1", "kaicenat")
    browser.post("/optout", data={"action": "block", "csrf": form})
    assert store.opted_out(conn, "1", "kaicenat")
    browser.post("/optout", data={"action": "allow", "csrf": form})
    assert not store.opted_out(conn, "1", "kaicenat")
    conn.close()


def test_my_channel_uses_twitchs_official_download_even_with_the_switch_off(
    settings, fake, fake_media, monkeypatch
) -> None:
    seen: list = []
    browser = sign_in(settings, fake, "mine", ["channel:manage:clips"], seen)
    transport = oauth_transport(["channel:manage:clips"], seen)
    monkeypatch.setattr(twitch_making, "download_http", lambda: httpx.Client(transport=transport))
    real_user_client = twitch_web.user_client
    monkeypatch.setattr(twitch_web, "user_client", lambda s, token, http=None: real_user_client(
        s, token, httpx.Client(transport=transport)))  # fmt: skip
    page = browser.get("/mine").text
    form = page.split('data-form="')[1].split('"')[0]
    assert browser.post("/api/mine", json={}, headers={"X-Form": "forged"}).status_code == 401
    reply = browser.post("/api/mine", json={}, headers={"X-Form": form}).json()
    conn = store.open_store(settings)
    assert not store.downloads_on(conn, settings)
    assert conn.execute("SELECT count(*) FROM grants").fetchone()[0] == 1
    conn.close()
    assert making.make_run(settings, reply["id"], transcribers=[]) == "done"
    assert ("GET", "/helix/clips/downloads") in seen
    state = browser.get(f"/api/runs/{reply['id']}").json()
    assert state["kind"] == "twitch_mine" and [c["status"] for c in state["clips"]] == ["done"] * 4
    conn = store.open_store(settings)
    assert conn.execute("SELECT count(*) FROM grants").fetchone()[0] == 0  # the token is gone
    conn.close()


def test_the_live_nudge_shows_while_a_live_streamers_clips_are_made(client, settings) -> None:
    conn = store.open_store(settings)
    store.set_downloads(conn, True)
    conn.close()
    run_id = start(client, "kaicenat")["id"]
    state = run_state(client, run_id)
    assert state["nudge"] == "live" and state["streamer"] == "KaiCenat"
    assert json.dumps(state)  # plain data


def test_sign_in_starts_on_the_public_address(settings, fake) -> None:
    """Opened as another address (127.0.0.1), sign-in first moves to the public one, or the
    sign-in cookie would be on a different host than the one Twitch comes back to."""
    browser = TestClient(create_app(settings, start_worker=False, twitch=fake),
                         follow_redirects=False, base_url="http://127.0.0.1:8800")  # fmt: skip
    hop = browser.get("/auth/twitch?for=mine")
    assert hop.status_code == 303
    assert hop.headers["location"] == "https://clips.example/auth/twitch?for=mine"
    assert accounts.STATE_COOKIE not in hop.headers.get("set-cookie", "")
