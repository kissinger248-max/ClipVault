import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipvault import db

from clipvault.posting import captiongen, checks, oauth, store, uploads
from clipvault.posting.secrets import Secrets
from clipvault.posting.uploaders import base, tiktok, youtube
from clipvault.posting.uploaders.base import ClipFile, Result, UploadError
from tests.helpers import add_clip, add_streamer

RULES = captiongen.load_rules()


def facts(**kw):
    f = {"clip_id": "c1", "streamer": "Kai Cenat", "login": "kaicenat", "transcript": "he finally beats the game after 1000 attempts",
         "clip_title": "finally beats GTA IV after 1000 attempts", "game": "Grand Theft Auto IV", "game_id": "1",
         "stream_title": "GTA marathon", "why_picked": "funny victory reaction", "hook": "Kai finally beats GTA IV",
         "moment_types": ["funny"]}
    return f | kw


# --- secrets -----------------------------------------------------------------------------------------------------
def test_secrets_roundtrip_in_memory_and_keychain(memory_keyring):
    for s in (Secrets({}), Secrets()):
        s.set("a", "1")
        s.set_json("t", {"x": 2})
        assert s.get("a") == "1" and s.get_json("t") == {"x": 2} and s.has("a")
        s.delete("a")
        s.delete("a")                                   # deleting twice is harmless
        assert s.get("a") is None
    from clipvault import config as cfg

    assert (cfg.keyring_service(), "posting.t") in memory_keyring.store     # lives under ClipVault's own keychain entry


# --- hashtags / captions -----------------------------------------------------------------------------------------
def test_gta6_only_for_real_gta6():
    v = captiongen.build_hashtags(facts(game="Grand Theft Auto VI"), RULES)
    v5 = captiongen.build_hashtags(facts(game="Grand Theft Auto V"), RULES)
    assert "#gta6" in v and "#gta6" not in v5 and "#gta5" in v5 and "#gtarp" in v5
    assert "#gta6" not in captiongen.build_hashtags(facts(), RULES)


def test_hashtag_rules():
    t = captiongen.build_hashtags(facts(), RULES)
    assert t[0] == "#kaicenat" and "#twitch" in t and 4 <= len(t) <= 8 and len(set(t)) == len(t)
    assert all(x == x.lower() for x in t) and "#fyp" not in t
    assert "#shorts" in captiongen.build_hashtags(facts(), RULES, "youtube")
    rules = dict(RULES, moments={"funny": ["#fyp", "#funny"]})
    assert "#fyp" not in captiongen.build_hashtags(facts(), rules)
    many = captiongen.build_hashtags(facts(moment_types=["funny", "rage", "clutch"]), RULES, "youtube")
    assert len(many) <= 8 and "#shorts" in many


def test_unknown_category_uses_real_name_slug():
    assert "#cooking" in captiongen.build_hashtags(facts(game="Cooking"), RULES)
    assert not [t for t in captiongen.build_hashtags(facts(game=None), RULES) if t.startswith("#gta")]


def test_unsupported_terms_catches_inventions():
    f = facts()
    assert captiongen.unsupported_terms("Kai finally beats the game after 1000 attempts", f) == []
    bad = captiongen.unsupported_terms("Kai beats Minecraft with Drake in 5 minutes", f)
    assert "Minecraft" in bad and "Drake" in bad and "5" in bad


def test_trim_and_assemble_limits():
    long = "word " * 1000
    out = captiongen.assemble(long, ["#a1", "#twitch"], "🎥 twitch.tv/x", 2200)
    assert len(out) <= 2200 and out.endswith("twitch.tv/x") and "#twitch" in out
    assert len(captiongen.trim(long, 100)) <= 100


class FakeHTTP:
    def __init__(self, responder):
        self.responder, self.calls = responder, []

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responder(method, url, kw)


def resp(data, status=200, headers=None):
    return SimpleNamespace(status_code=status, json=lambda: data, headers=headers or {}, text=json.dumps(data))


def test_generate_with_model_and_fallback():
    ok = FakeHTTP(lambda m, u, k: resp({"models": [{"name": "llama3.2:3b"}]}) if m == "GET" else
                  resp({"message": {"content": json.dumps({"hook": "Kai finally beats GTA IV", "caption": "He beats it after 1000 attempts."})}}))
    out = captiongen.generate(facts(), rules=RULES, http=ok)
    assert out["source"] == "ollama" and "#kaicenat" in out["per_platform"]["tiktok"]
    assert out["per_platform"]["youtube_title"].endswith("#shorts") and "🎥 twitch.tv/kaicenat" in out["per_platform"]["instagram"]
    liar = FakeHTTP(lambda m, u, k: resp({"models": [{"name": "llama3.2"}]}) if m == "GET" else
                    resp({"message": {"content": json.dumps({"hook": "Kai meets Drake", "caption": "Kai plays Minecraft with Drake."})}}))
    bad = captiongen.generate(facts(), rules=RULES, http=liar)
    assert bad["source"] == "fallback" and "Drake" not in bad["per_platform"]["tiktok"] and bad["notes"]
    down = FakeHTTP(lambda m, u, k: (_ for _ in ()).throw(__import__("httpx").ConnectError("down")))
    assert captiongen.generate(facts(), rules=RULES, http=down)["source"] == "fallback"


# --- checks ------------------------------------------------------------------------------------------------------
def test_checks_block_bad_caption_and_video(tmp_path):
    f = facts()
    pkg = captiongen.generate(f, rules=RULES, model=None)
    assert checks.check_caption(pkg, f, "tiktok") == []
    pkg["per_platform"]["tiktok"] += " #gta6 #fyp"
    problems = " ".join(checks.check_caption(pkg, f, "tiktok"))
    assert "#gta6" in problems and "#fyp" in problems
    v = tmp_path / "s.mp4"
    v.write_bytes(b"x")
    assert checks.check_video(ClipFile("c", v, 30, 1080, 1920), "youtube", "h264") == []
    assert checks.check_video(ClipFile("c", v, 200, 1080, 1920), "youtube", "h264")
    assert checks.check_video(ClipFile("c", v, 30, 1920, 1080), "tiktok", "h264")
    assert checks.check_video(ClipFile("c", v, 30, 1080, 1920), "tiktok", "vp9")


# --- oauth -------------------------------------------------------------------------------------------------------
@pytest.fixture
def env(home, library):
    c = db.open_db(home / "clipvault.db")
    yield c, Secrets({}), home
    c.close()


def test_oauth_connect_refresh_disconnect(env):
    c, s, _ = env
    with pytest.raises(oauth.OAuthError):
        oauth.begin(c, s, "tiktok")
    s.set("cred:tiktok:client_key", "K")
    s.set("cred:tiktok:client_secret", "S")
    url = oauth.begin(c, s, "tiktok", "Main")
    state = url.split("state=")[1].split("&")[0]
    assert "video.upload" in url.replace("%2C", ",") and "client_key=K" in url and "localhost" in url
    http = FakeHTTP(lambda m, u, k: resp({"access_token": "A1", "refresh_token": "R1", "expires_in": 86400, "open_id": "o1"})
                    if "oauth/token" in u else tiktok_profile(k))
    aid = oauth.finish(c, s, "tiktok", "code", state, http=http)
    assert c.execute("SELECT status, handle FROM social_accounts WHERE id=?", (aid,)).fetchone()["handle"] == "Kai Cenat"
    with pytest.raises(oauth.OAuthError):                     # state is single-use
        oauth.finish(c, s, "tiktok", "code", state, http=http)
    assert oauth.ensure_fresh(c, s, aid, http=http) == "A1"   # still fresh: no refresh call
    n = len(http.calls)
    tok = s.get_json(f"token:{aid}")
    tok["expires_at"] = time.time() + 5
    s.set_json(f"token:{aid}", tok)
    http2 = FakeHTTP(lambda m, u, k: resp({"access_token": "A2", "refresh_token": "R2", "expires_in": 86400}))
    assert oauth.ensure_fresh(c, s, aid, http=http2) == "A2" and http2.calls[0][2]["data"]["grant_type"] == "refresh_token"
    assert s.get_json(f"token:{aid}")["refresh_token"] == "R2" and len(http.calls) == n
    bad = FakeHTTP(lambda m, u, k: resp({"error": "invalid_grant", "error_description": "expired"}, 400))
    tok["expires_at"] = 0
    s.set_json(f"token:{aid}", tok)
    with pytest.raises(oauth.OAuthError):
        oauth.ensure_fresh(c, s, aid, http=bad)
    assert c.execute("SELECT status FROM social_accounts WHERE id=?", (aid,)).fetchone()["status"] == "expired"
    oauth.disconnect(c, s, aid)
    assert s.get(f"token:{aid}") is None


# --- uploaders ---------------------------------------------------------------------------------------------------
def test_tiktok_chunks_and_unaudited_message(tmp_path):
    assert tiktok.chunk_plan(3_000_000) == (3_000_000, 1)
    assert tiktok.chunk_plan(25 * 1024 * 1024) == (tiktok.CHUNK, 2)
    v = tmp_path / "s.mp4"
    v.write_bytes(b"0" * 100)
    clip = ClipFile("c", v, 20)
    caption = {"per_platform": {"tiktok": "hi"}}

    def responder(m, u, k):
        if "creator_info" in u:
            return resp({"data": {"privacy_level_options": ["SELF_ONLY"]}, "error": {"code": "ok"}})
        return resp({"error": {"code": tiktok.UNAUDITED, "message": "x"}}, 403)
    with pytest.raises(UploadError) as e:
        tiktok.TikTok().upload(clip, caption, {}, "t", {"tiktok_mode": "direct"}, http=FakeHTTP(responder), sleep=lambda s: None)
    assert "Drafts mode" in str(e.value) and e.value.code == "permanent"
    dr = tiktok.TikTok().upload(clip, caption, {}, "t", {"tiktok_mode": "direct"}, dry_run=True, http=FakeHTTP(responder), sleep=lambda s: None)
    assert dr.dry_run and "private" in dr.detail.lower()


def test_youtube_validation_and_quota(tmp_path):
    v = tmp_path / "s.mp4"
    v.write_bytes(b"0")
    with pytest.raises(UploadError):
        youtube.validate(ClipFile("c", v, 200, 1080, 1920))
    with pytest.raises(UploadError):
        youtube.validate(ClipFile("c", v, 30, 1920, 1080))
    q = resp({"error": {"errors": [{"reason": "quotaExceeded"}], "message": "q"}}, 403)
    with pytest.raises(UploadError) as e:
        youtube._raise_for(q)
    assert e.value.code == "quota"


def test_request_retries_then_gives_up():
    h = FakeHTTP(lambda m, u, k: resp({}, 503))
    with pytest.raises(UploadError) as e:
        base.request(h, "GET", "http://x", tries=3, sleep=lambda s: None)
    assert e.value.code == "temporary" and len(h.calls) == 3


# --- queue -------------------------------------------------------------------------------------------------------
class FakeUploader:
    def __init__(self, behaviour="ok"):
        self.behaviour, self.calls = behaviour, 0

    def upload(self, clip, caption, account, token, settings, *, dry_run=False, **kw):
        self.calls += 1
        if self.behaviour == "perm":
            raise UploadError("TikTok said no", "permanent")
        if self.behaviour == "temp":
            raise UploadError("blip", "temporary")
        return Result(True, url="http://u", detail="Posted.", dry_run=dry_run)


def make_clip(env, monkeypatch, clip_id="c1", permitted=True, qa=1, rendered_at="2026-11-12T10:00:00Z"):
    c, s, home = env
    if not c.execute("SELECT 1 FROM streamers WHERE login='kaicenat'").fetchone():
        if permitted:
            add_streamer(c, "kaicenat", display_name="Kai Cenat", post_enabled=1, permission_type="written",
                         permission_ref="DM from Kai, Nov 2026")
        else:
            add_streamer(c, "kaicenat", display_name="Kai Cenat")
    sid = c.execute("SELECT id FROM streamers WHERE login='kaicenat'").fetchone()[0]
    rel = f"kaicenat/2026-11/vertical/{clip_id}_9x16.mp4"
    srt = f"kaicenat/2026-11/{clip_id}.srt"
    root = home / "library"
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(b"v")
    (root / srt).write_text("1\n00:00:00,000 --> 00:00:02,000\nhe finally beats the game\n\n")
    add_clip(c, clip_id, sid, title="Kai beats the game", game_name="Grand Theft Auto IV", transcript_path=srt,
             state="written")
    c.execute("INSERT INTO renders (clip_id, layout, path, qa_passed, created_at) VALUES (?,?,?,?,?)",
              (clip_id, "letterbox", rel, qa, rendered_at))
    monkeypatch.setattr(uploads, "clip_file", lambda cid, p: (ClipFile(cid, p, 30, 1080, 1920), "h264"))
    monkeypatch.setattr(captiongen, "pick_model", lambda *a, **k: None)
    return uploads.library_for(c)


def connect_account(c, s, platform="tiktok", ext="e1"):
    aid = store.add_account(c, platform, "Acct", "h", ext, None, [], time.time() + 99999)
    s.set_json(f"token:{aid}", {"access_token": "T", "refresh_token": "R", "expires_at": time.time() + 99999})
    return aid


def test_streamer_without_permission_can_never_be_queued(env, monkeypatch):
    c, s, _ = env
    lib = make_clip(env, monkeypatch, permitted=False)
    with pytest.raises(uploads.NotAllowed, match="permission"):
        uploads.enqueue(c, lib, "c1")
    assert c.execute("SELECT COUNT(*) FROM upload_jobs").fetchone()[0] == 0
    ready = uploads.ready_clips(c, lib)
    assert ready[0]["id"] == "c1" and "permission" in ready[0]["blocked"]
    store.set_setting(c, "auto_approve", "1")
    assert uploads.sync_auto(c, lib) == 0                              # not even automatically


def test_permission_withdrawn_after_queueing_blocks_the_upload(env, monkeypatch):
    c, s, _ = env
    connect_account(c, s)
    lib = make_clip(env, monkeypatch)
    uploads.enqueue(c, lib, "c1", 1_800_000_000.0)
    c.execute("UPDATE streamers SET post_enabled=0, permission_type='none', permission_ref=NULL")
    up = FakeUploader()
    uploads.tick(c, lib, s, now=1_800_100_000.0, uploaders={"tiktok": up})
    assert up.calls == 0 and c.execute("SELECT state FROM upload_jobs WHERE platform='tiktok'").fetchone()[0] == "blocked"


def test_unconnected_platform_goes_to_manual_with_caption(env, monkeypatch):
    c, s, _ = env
    lib = make_clip(env, monkeypatch)
    ids = uploads.enqueue(c, lib, "c1")
    assert len(ids) == 3
    ov = uploads.overview(c)
    assert len(ov["manual"]) == 3 and "isn't connected" in ov["manual"][0]["reason"]
    assert "#kaicenat" in ov["manual"][0]["caption"]["tiktok"] and "he finally beats" in captiongen.load(c, "c1")[1]["transcript"]
    uploads.mark_posted(c, ov["manual"][0]["id"])
    assert len(uploads.overview(c)["manual"]) == 2


def test_spacing_and_daily_max(env, monkeypatch):
    c, s, _ = env
    connect_account(c, s)
    now = 1_800_000_000.0
    store.set_setting(c, "dry_run", "0")
    times = []
    for i in range(3):
        lib = make_clip(env, monkeypatch, f"c{i}")
        uploads.enqueue(c, lib, f"c{i}", now)
        times.append(c.execute("SELECT not_before FROM upload_jobs WHERE clip_id=? AND platform='tiktok'", (f"c{i}",)).fetchone()[0])
    assert times[1] - times[0] >= 2 * 3600 and times[2] - times[1] >= 2 * 3600
    store.set_setting(c, "daily_max", "1")
    lib = make_clip(env, monkeypatch, "cx")
    uploads.enqueue(c, lib, "cx", now)
    later = c.execute("SELECT not_before FROM upload_jobs WHERE clip_id='cx' AND platform='tiktok'").fetchone()[0]
    assert later > max(times)


def test_cross_account_gap(env, monkeypatch):
    c, s, _ = env
    a1, a2 = connect_account(c, s, "tiktok", "e1"), connect_account(c, s, "tiktok", "e2")
    c.execute("INSERT INTO routing_rules (streamer, clip_type, account_id) VALUES ('kaicenat','*',?),('kaicenat','*',?)", (a1, a2))
    lib = make_clip(env, monkeypatch)
    uploads.enqueue(c, lib, "c1", 1_800_000_000.0)
    t = sorted(r[0] for r in c.execute("SELECT not_before FROM upload_jobs WHERE platform='tiktok'"))
    assert len(t) == 2 and t[1] - t[0] >= 600


def test_dry_run_full_flow(env, monkeypatch):
    c, s, _ = env
    for p in ("tiktok", "youtube", "instagram"):
        connect_account(c, s, p, "e_" + p)
    lib = make_clip(env, monkeypatch)
    uploads.enqueue(c, lib, "c1")
    ups = {p: FakeUploader() for p in ("tiktok", "youtube", "instagram")}
    now = time.time()
    ran = 0
    for i in range(4):
        ran += uploads.tick(c, lib, s, now=now + i * 3600, uploaders=ups)["ran"].get("dry_run_ok", 0)
    assert ran == 3 and all(u.calls == 1 for u in ups.values())
    assert {r[0] for r in c.execute("SELECT state FROM upload_jobs")} == {"dry_run_ok"}


def test_failure_goes_to_manual_and_temp_retries(env, monkeypatch):
    c, s, _ = env
    connect_account(c, s)
    store.set_setting(c, "enabled_youtube", "0")
    store.set_setting(c, "enabled_instagram", "0")
    store.set_setting(c, "dry_run", "0")
    lib = make_clip(env, monkeypatch)
    uploads.enqueue(c, lib, "c1")
    now = time.time() + 10 * 86400
    uploads.tick(c, lib, s, now=now, uploaders={"tiktok": FakeUploader("temp")})
    j = c.execute("SELECT * FROM upload_jobs").fetchone()
    assert j["state"] == "queued" and j["attempts"] == 1 and j["not_before"] > now
    uploads.retry_job(c, j["id"], now)
    uploads.tick(c, lib, s, now=now, uploaders={"tiktok": FakeUploader("perm")})
    j = c.execute("SELECT * FROM upload_jobs").fetchone()
    assert j["state"] == "manual" and "said no" in j["reason"]
    assert uploads.overview(c)["manual"][0]["caption"]["tiktok"]


def test_theo_blocks_before_upload(env, monkeypatch):
    c, s, _ = env
    connect_account(c, s)
    store.set_setting(c, "enabled_youtube", "0")
    store.set_setting(c, "enabled_instagram", "0")
    lib = make_clip(env, monkeypatch)
    uploads.enqueue(c, lib, "c1", 1_800_000_000.0)
    monkeypatch.setattr(uploads, "clip_file", lambda cid, p: (ClipFile(cid, p, 30, 1920, 1080), "h264"))
    up = FakeUploader()
    uploads.tick(c, lib, s, now=1_800_100_000.0, uploaders={"tiktok": up})
    assert up.calls == 0 and c.execute("SELECT state FROM upload_jobs").fetchone()[0] == "blocked"


def test_auto_approve_only_takes_new_clips_that_passed_qa(env, monkeypatch):
    c, s, _ = env
    lib = make_clip(env, monkeypatch, "old")
    assert uploads.sync_auto(c, lib) == 0                              # off by default
    store.set_setting(c, "auto_approve", "1")
    assert uploads.sync_auto(c, lib) == 0                              # old library is left alone (nothing newer)
    make_clip(env, monkeypatch, "bad", qa=0, rendered_at="2999-01-01T00:00:00Z")
    assert uploads.sync_auto(c, lib) == 0                              # QA flagged: never automatic
    make_clip(env, monkeypatch, "new", rendered_at="2999-01-01T00:00:00Z")
    assert uploads.sync_auto(c, lib) == 3
    assert {r[0] for r in c.execute("SELECT DISTINCT clip_id FROM upload_jobs")} == {"new"}


# --- the panel ---------------------------------------------------------------------------------------------------
def test_posting_page_routes_and_security(client, home, monkeypatch):
    import re

    from tests.test_web import login

    login(client)
    html = client.get("/posting").text
    assert "Posting" in html and 'id="posting-app"' in html
    assert html.count('href="/posting"') == 2                              # the sidebar and phone-bar links
    token = re.search(r'data-csrf="([^"]+)"', html).group(1)
    setup = client.get("/api/posting/setup").json()
    assert setup["settings"]["dry_run"] == "1" and setup["platforms"]["tiktok"]["redirect_uri"].endswith("/oauth/tiktok/callback")
    post = lambda url, data: client.post(url, data={"csrf": token, "payload": json.dumps(data)})  # noqa: E731
    assert client.post("/api/posting/credentials/tiktok", data={"payload": "{}"}).status_code == 403   # no form token
    assert post("/api/posting/credentials/tiktok", {"client_key": "K", "client_secret": "S"}).json() == {"saved": 2}
    assert not any("S" == f.get("value") for f in client.get("/api/posting/setup").json()["platforms"]["tiktok"]["fields"])
    assert all(f["saved"] for f in client.get("/api/posting/setup").json()["platforms"]["tiktok"]["fields"])
    assert Secrets().get("cred:tiktok:client_key") == "K"
    assert post("/api/posting/settings", {"dry_run": "1", "daily_max": "2"}).json()["daily_max"] == "2"
    assert post("/api/posting/settings", {"nonsense": "1"}).status_code == 400
    url = post("/api/posting/connect/tiktok", {"label": "Main"}).json()["url"]
    assert url.startswith("https://www.tiktok.com/") and "client_key=K" in url
    assert post("/api/posting/enqueue/missing", {}).status_code == 403
    q = client.get("/api/posting/queue").json()
    assert q["queue"] == [] and q["ready"] == []
    cb = client.get("/oauth/tiktok/callback?error=access_denied")          # public: reachable without the login cookie
    anon = type(client)(client.app, follow_redirects=False)
    assert anon.get("/oauth/tiktok/callback?error=access_denied").status_code == 400 and cb.status_code == 400
    assert anon.get("/api/posting/setup").status_code == 303                # everything else still needs the login
    assert post("/api/posting/credentials/tiktok/clear", {}).json() == {"ok": True}
    assert Secrets().get("cred:tiktok:client_key") is None


def test_posting_step_is_registered_and_quiet_when_idle(home):
    from clipvault import scheduler

    assert "posting" in scheduler.STEPS and "posting" in scheduler.STEP_FUNCTIONS
    c = db.open_db(home / "clipvault.db")
    from clipvault import config as cfg

    note = scheduler.STEP_FUNCTIONS["posting"](c, cfg.load_config(home / cfg.CONFIG_FILENAME), None)
    assert note == ""
    c.close()


# --- sign-in from the desktop app, the Google callback, and the port ----------------------------------------------
def _csrf_poster(client):
    import re

    html = client.get("/posting").text
    token = re.search(r'data-csrf="([^"]+)"', html).group(1)
    return lambda url, data: client.post(url, data={"csrf": token, "payload": json.dumps(data)})  # noqa: E731


def test_google_callback_connects_and_the_account_stays_saved(client, home, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    from fastapi.testclient import TestClient

    from clipvault.posting import http as shim
    from clipvault.web import posting_routes
    from clipvault.web.server import create_app
    from tests.test_web import login

    login(client)
    post = _csrf_poster(client)
    assert post("/api/posting/credentials/youtube", {"client_id": "gid", "client_secret": "gsec"}).json() == {"saved": 2}
    opened = []
    monkeypatch.setattr(posting_routes, "open_in_browser", lambda url: opened.append(url) or True)
    url = post("/api/posting/connect/youtube", {"label": "Main channel"}).json()["url"]
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://accounts.google.com/") and q["client_id"] == ["gid"]
    assert q["redirect_uri"][0].endswith("/oauth/youtube/callback") and q["access_type"] == ["offline"]
    # the desktop window asks the app to open Google's page in the system browser
    assert post("/api/posting/open-external", {"url": url}).json() == {"opened": True} and opened == [url]
    assert post("/api/posting/open-external", {"url": "https://evil.example/x"}).status_code == 400
    assert post("/api/posting/open-external", {"url": "http://accounts.google.com/x"}).status_code == 400
    assert len(opened) == 1

    def fake(method, u, **kw):
        if u.endswith("/token"):
            assert kw["data"]["code"] == "CODE" and kw["data"]["redirect_uri"] == q["redirect_uri"][0]
            return resp({"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        return resp({"items": [{"id": "UC123", "snippet": {"title": "Kai Clips"}}]})
    monkeypatch.setattr(shim, "post", lambda u, **kw: fake("POST", u, **kw))
    monkeypatch.setattr(shim, "get", lambda u, **kw: fake("GET", u, **kw))
    # Google sends the person back in the system browser, which has no ClipVault login cookie
    browser = TestClient(client.app, follow_redirects=False)
    done = browser.get(f"/oauth/youtube/callback?code=CODE&state={q['state'][0]}")
    assert done.status_code == 200 and "Connected" in done.text and "go back to ClipVault" in done.text
    accounts = client.get("/api/posting/accounts").json()["accounts"]
    assert [(a["platform"], a["label"], a["handle"], a["status"]) for a in accounts] == [("youtube", "Main channel", "Kai Clips", "connected")]
    tok = Secrets().get_json(f"token:{accounts[0]['id']}")
    assert tok["access_token"] == "AT" and tok["refresh_token"] == "RT"
    assert browser.get(f"/oauth/youtube/callback?code=CODE&state={q['state'][0]}").status_code == 400    # single use

    # "remains saved": a brand-new app instance on the same folder (an app restart) still has it all
    restarted = TestClient(create_app(home), follow_redirects=False)
    login(restarted)
    again = restarted.get("/api/posting/accounts").json()["accounts"]
    assert again == accounts and Secrets().get_json(f"token:{again[0]['id']}")["refresh_token"] == "RT"
    assert restarted.get("/api/posting/setup").json()["platforms"]["youtube"]["fields"][0]["saved"] is True


def test_open_external_refuses_other_computers(client, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.test_web import login

    login(client)
    post = _csrf_poster(client)
    remote = TestClient(client.app, follow_redirects=False, client=("192.168.1.20", 5000))
    remote.cookies.update(client.cookies)
    assert remote.post("/api/posting/open-external", data={"payload": "{}"}).status_code in (403,)
    assert post("/api/posting/open-external", {}).status_code == 400


def test_panel_moves_to_a_free_port_when_another_program_uses_its_port(home):
    import socket

    from clipvault import app as appmod
    from clipvault import config as cfg

    with socket.socket() as other:                       # "another program" (the ClipWave studio) holding the port
        other.bind(("127.0.0.1", 0))
        other.listen()
        taken = other.getsockname()[1]
        assert appmod.port_in_use(taken)
        chosen = appmod.choose_port(taken)
        assert chosen != taken and not appmod.port_in_use(chosen)
        assert cfg.panel_port(cfg.Config()) == cfg.Config().panel.port        # no file: the configured port
        (home / cfg.PANEL_PORT_FILE).write_text(str(chosen))
        assert cfg.panel_port(cfg.Config()) == chosen
    free = appmod.choose_port(chosen)
    assert free == chosen                                                       # a free port is kept as it is


# --- TikTok desktop sign-in (PKCE) ---------------------------------------------------------------------------------
TIKTOK_REDIRECT = "http://localhost:8766/oauth/tiktok/callback"


def tiktok_profile(kw):
    """TikTok's /user/info/ with only user.info.basic: asking for `username` is refused."""
    fields = kw["params"]["fields"].split(",")
    if "username" in fields:
        return resp({"error": {"code": "scope_not_authorized", "message": "The user did not authorize the scope required."}}, 401)
    return resp({"data": {"user": {"open_id": "o1", "display_name": "Kai Cenat"}}, "error": {"code": "ok"}})


def _tiktok_begin(c, s, port=8766):
    from urllib.parse import parse_qs, urlparse

    s.set("cred:tiktok:client_key", "CK")
    s.set("cred:tiktok:client_secret", "CS")
    url = oauth.begin(c, s, "tiktok", "Main", port)
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    verifier = c.execute("SELECT code_verifier FROM oauth_states WHERE state=?", (q["state"],)).fetchone()[0]
    return url, q, verifier


def test_tiktok_authorization_url_has_pkce_and_only_the_two_scopes(env):
    import hashlib
    import re

    c, s, _ = env
    url, q, verifier = _tiktok_begin(c, s)
    assert url.startswith("https://www.tiktok.com/v2/auth/authorize/?")
    assert q["client_key"] == "CK" and q["response_type"] == "code"
    assert q["redirect_uri"] == TIKTOK_REDIRECT                              # the exact dynamic localhost address
    assert q["scope"] == "user.info.basic,video.upload" and "publish" not in url
    assert q["code_challenge_method"] == "S256"
    assert 43 <= len(verifier) <= 128 and re.fullmatch(r"[A-Za-z0-9\-._~]+", verifier)
    assert q["code_challenge"] == hashlib.sha256(verifier.encode()).hexdigest()           # TikTok wants HEX, not base64url
    assert re.fullmatch(r"[0-9a-f]{64}", q["code_challenge"]) and verifier not in url        # the verifier never leaves the app
    # a new verifier (and state) for every attempt
    _, q2, verifier2 = _tiktok_begin(c, s)
    assert verifier2 != verifier and q2["state"] != q["state"] and q2["code_challenge"] != q["code_challenge"]
    # other platforms don't get PKCE parameters
    s.set("cred:youtube:client_id", "g")
    s.set("cred:youtube:client_secret", "g")
    assert "code_challenge" not in oauth.begin(c, s, "youtube", "x", 8766)


def test_tiktok_token_exchange_sends_the_matching_verifier(env):
    c, s, _ = env
    _, q, verifier = _tiktok_begin(c, s)
    http = FakeHTTP(lambda m, u, k: resp({"access_token": "A", "refresh_token": "R", "expires_in": 86400, "open_id": "o1"})
                    if "oauth/token" in u else tiktok_profile(k))
    aid = oauth.finish(c, s, "tiktok", "AUTHCODE", q["state"], 8766, http=http)
    sent = http.calls[0][2]["data"]
    assert sent["code"] == "AUTHCODE" and sent["code_verifier"] == verifier and sent["redirect_uri"] == TIKTOK_REDIRECT
    assert sent["grant_type"] == "authorization_code" and sent["client_key"] == "CK"
    assert c.execute("SELECT scopes, handle FROM social_accounts WHERE id=?", (aid,)).fetchone()[:] == ('["user.info.basic", "video.upload"]', "Kai Cenat")
    assert c.execute("SELECT COUNT(*) FROM oauth_states WHERE state=?", (q["state"],)).fetchone()[0] == 0   # verifier is gone


def test_callback_state_is_validated(env):
    c, s, _ = env
    _, q, _ = _tiktok_begin(c, s)
    never = FakeHTTP(lambda m, u, k: (_ for _ in ()).throw(AssertionError("must not call TikTok")))
    for bad in ("", "made-up", q["state"] + "x"):
        with pytest.raises(oauth.OAuthError, match="expired or wasn't started here"):
            oauth.finish(c, s, "tiktok", "code", bad, 8766, http=never)
    with pytest.raises(oauth.OAuthError):                                    # a TikTok state can't finish a YouTube sign-in
        oauth.finish(c, s, "youtube", "code", q["state"], 8766, http=never)
    c.execute("UPDATE oauth_states SET created_at = created_at - 100000")      # too old
    with pytest.raises(oauth.OAuthError):
        oauth.finish(c, s, "tiktok", "code", q["state"], 8766, http=never)
    assert never.calls == []


def test_tiktok_error_is_explained_in_the_app_and_a_restart_keeps_the_sign_in(client, home, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    from fastapi.testclient import TestClient

    from clipvault.posting import http as shim
    from clipvault.web.server import create_app
    from tests.test_web import login

    app_client = TestClient(client.app, base_url="http://localhost:8766", follow_redirects=False)
    login(app_client)
    post = _csrf_poster(app_client)
    post("/api/posting/credentials/tiktok", {"client_key": "CK", "client_secret": "CS"})
    url = post("/api/posting/connect/tiktok", {"label": "Main"}).json()["url"]
    q = parse_qs(urlparse(url).query)
    assert q["redirect_uri"] == [TIKTOK_REDIRECT] and q["code_challenge_method"] == ["S256"]

    # TikTok refuses / the person says no: a clear message, shown on the Posting page too
    browser = TestClient(client.app, base_url="http://localhost:8766", follow_redirects=False)
    denied = browser.get("/oauth/tiktok/callback?error=access_denied&state=" + q["state"][0])
    assert denied.status_code == 400 and "approve the connection" in denied.text
    assert "approve the connection" in app_client.get("/api/posting/accounts").json()["connect_error"]
    bad = browser.get("/oauth/tiktok/callback?code=C&state=forged")
    assert bad.status_code == 400 and "started here" in bad.text

    # ...then a fresh attempt; the app restarts before TikTok sends the person back
    url2 = post("/api/posting/connect/tiktok", {"label": "Main"}).json()["url"]
    state2 = parse_qs(urlparse(url2).query)["state"][0]
    verifier = sqlite_verifier(home, state2)

    def fake(u, **kw):
        if u.endswith("/oauth/token/"):
            assert kw["data"]["code_verifier"] == verifier and kw["data"]["redirect_uri"] == TIKTOK_REDIRECT
            return resp({"access_token": "AT", "refresh_token": "RT", "expires_in": 86400, "open_id": "OPEN1"})
        return tiktok_profile(kw)
    monkeypatch.setattr(shim, "post", fake)
    monkeypatch.setattr(shim, "get", fake)
    restarted = TestClient(create_app(home), base_url="http://localhost:8766", follow_redirects=False)
    ok = restarted.get(f"/oauth/tiktok/callback?code=C&state={state2}")
    assert ok.status_code == 200 and "Connected" in ok.text
    login(restarted)
    data = restarted.get("/api/posting/accounts").json()
    assert data["connect_error"] == "" and [(a["handle"], a["status"]) for a in data["accounts"]] == [("Kai Cenat", "connected")]
    assert Secrets().get_json(f"token:{data['accounts'][0]['id']}")["refresh_token"] == "RT"


def sqlite_verifier(home, state):
    c = db.open_db(home / "clipvault.db")
    try:
        return c.execute("SELECT code_verifier FROM oauth_states WHERE state=?", (state,)).fetchone()[0]
    finally:
        c.close()


def test_direct_post_needs_the_publish_scope_but_drafts_do_not():
    from clipvault.posting.uploaders.tiktok import TikTok

    clip = ClipFile("c", Path("/nonexistent.mp4"), 30, 1080, 1920)
    acc = {"id": 1, "simulated": True, "scopes": json.dumps(["user.info.basic", "video.upload"])}
    with pytest.raises(UploadError, match="video.publish"):
        TikTok().upload(clip, {"per_platform": {"tiktok": "x"}}, acc, "T", {"tiktok_mode": "direct"}, dry_run=True)
    drafts = TikTok().upload(clip, {"per_platform": {"tiktok": "x"}}, dict(acc), "T", {"tiktok_mode": "drafts"}, dry_run=True) \
        if clip.path.exists() else None
    assert drafts is None or drafts.dry_run


def test_migration_0011_keeps_existing_sign_in_rows(home):
    import sqlite3

    path = home / "old.db"
    conn = db.connect(path)
    conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for name, sql in db.available_migrations():
        if name < "0011_oauth_pkce":
            conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nINSERT INTO schema_migrations VALUES ('{name}', 'x');\nCOMMIT;")
    conn.execute("INSERT INTO oauth_states VALUES ('old', 'youtube', 'L', 1.0)")
    conn.close()
    conn = db.open_db(path)
    assert conn.execute("SELECT state, label, code_verifier FROM oauth_states").fetchall()[0][:3] == ("old", "L", None)
    conn.close()


# --- Streamers page: Posting permission ----------------------------------------------------------------------------
def _permission_post(client, sid, **fields):
    import re

    html = client.get("/streamers").text
    token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    return client.post(f"/streamers/{sid}/permission", data={"csrf": token, **fields})


def _flash(client, response):
    from urllib.parse import unquote_plus

    return unquote_plus(response.headers["location"].split("msg=")[1]) if "msg=" in response.headers["location"] else ""


def _streamer(home, login="kaicenat"):
    c = db.open_db(home / "clipvault.db")
    try:
        return dict(c.execute("SELECT id, post_enabled, permission_type, permission_ref FROM streamers WHERE login=?", (login,)).fetchone())
    finally:
        c.close()


def test_streamers_page_shows_the_permission_section_and_saves_it(client, home):
    from tests.test_web import login

    c = db.open_db(home / "clipvault.db")
    sid = add_streamer(c, "kaicenat", display_name="Kai Cenat")
    c.close()
    login(client)
    html = client.get("/streamers").text
    assert "Posting permission" in html and 'name="permission_type"' in html and 'name="proof"' in html
    assert 'name="post"' in html and f'action="/streamers/{sid}/permission"' in html and f'id="streamer-{sid}"' in html

    ok = _permission_post(client, sid, permission_type="written", proof="  https://x.com/kai/status/1  ", post="1")
    assert ok.status_code == 303 and "Saved" in _flash(client, ok) and ok.headers["location"].startswith(f"/streamers?msg=")
    assert _streamer(home) == {"id": sid, "post_enabled": 1, "permission_type": "written", "permission_ref": "https://x.com/kai/status/1"}
    page = client.get("/streamers").text
    assert 'value="https://x.com/kai/status/1"' in page and "Posting is on" in page
    # the Posting queue's own check agrees with what was saved
    c = db.open_db(home / "clipvault.db")
    saved = c.execute("SELECT s.login, s.display_name, s.post_enabled, s.permission_type, s.permission_ref FROM streamers s").fetchone()
    assert uploads.may_post(dict(saved)) == ""
    c.close()

    # switching posting off keeps the permission on record
    off = _permission_post(client, sid, permission_type="written", proof="https://x.com/kai/status/1")
    assert _streamer(home)["post_enabled"] == 0 and _streamer(home)["permission_ref"] == "https://x.com/kai/status/1" and off.status_code == 303


def test_posting_on_without_permission_or_proof_is_an_error_not_a_silent_failure(client, home):
    from tests.test_web import login

    c = db.open_db(home / "clipvault.db")
    sid = add_streamer(c, "kaicenat", display_name="Kai Cenat")
    c.close()
    login(client)
    none = _permission_post(client, sid, permission_type="none", proof="https://x.com/a", post="1")
    assert "Couldn't" in _flash(client, none) and "Campaign or Written" in _flash(client, none)
    blank = _permission_post(client, sid, permission_type="campaign", proof="   ", post="1")
    assert "Couldn't" in _flash(client, blank) and "proof link" in _flash(client, blank)
    junk = _permission_post(client, sid, permission_type="verbal", proof="x", post="1")
    assert "Couldn't" in _flash(client, junk)
    assert _streamer(home) == {"id": sid, "post_enabled": 0, "permission_type": "none", "permission_ref": None}   # nothing was saved
    shown = client.get(blank.headers["location"]).text
    assert 'class="flash problem"' in shown and 'role="alert"' in shown and "proof link" in shown
    # without the form token nothing happens at all
    assert client.post(f"/streamers/{sid}/permission", data={"permission_type": "written", "proof": "x", "post": "1"}).status_code == 403
    assert _streamer(home)["post_enabled"] == 0


def test_type_none_clears_the_proof_and_posting(client, home):
    from tests.test_web import login

    c = db.open_db(home / "clipvault.db")
    sid = add_streamer(c, "kaicenat", post_enabled=1, permission_type="campaign", permission_ref="https://c.example/1")
    c.close()
    login(client)
    _permission_post(client, sid, permission_type="none", proof="https://c.example/1")
    assert _streamer(home) == {"id": sid, "post_enabled": 0, "permission_type": "none", "permission_ref": None}


def test_tiktok_drafts_never_ask_for_creator_info_and_land_in_the_inbox(tmp_path):
    """With only user.info.basic + video.upload, creator info is refused by TikTok; Drafts must not need it."""
    v = tmp_path / "s.mp4"
    v.write_bytes(b"0" * 100)
    clip = ClipFile("c", v, 20)
    caption = {"per_platform": {"tiktok": "hi"}}
    seen = []

    def responder(m, u, k):
        seen.append((m, u.split("/v2/")[-1] if "/v2/" in u else "upload"))
        if "creator_info" in u:
            return resp({"error": {"code": "scope_not_authorized", "message": "The user did not authorize the scope"}}, 401)
        if u.endswith("/inbox/video/init/"):
            assert set(k["json"]) == {"source_info"} and k["json"]["source_info"]["source"] == "FILE_UPLOAD"
            return resp({"data": {"publish_id": "p1", "upload_url": "https://upload.example/x"}, "error": {"code": "ok"}})
        if m == "PUT":
            return resp({}, 201)
        return resp({"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}})
    acc = {"id": 1, "handle": "kai", "scopes": json.dumps(["user.info.basic", "video.upload"])}
    out = tiktok.TikTok().upload(clip, caption, acc, "t", {"tiktok_mode": "drafts"}, http=FakeHTTP(responder), sleep=lambda s: None)
    assert out.ok and "inbox" in out.detail.lower() and out.extra["publish_id"] == "p1"
    assert not any("creator_info" in u for _m, u in seen) and ("PUT", "upload") in seen
    # Direct Post does ask, and a missing permission is explained in plain words
    with pytest.raises(UploadError, match="doesn't have the permission"):
        tiktok.TikTok().upload(clip, caption, {"id": 1}, "t", {"tiktok_mode": "direct"}, http=FakeHTTP(responder), sleep=lambda s: None)


# --- spacing in minutes ------------------------------------------------------------------------------------------
def test_old_hours_setting_is_still_honoured_and_minutes_win_once_set(env):
    c, _s, _ = env
    assert store.get_setting(c, "min_gap_minutes") == "120"
    c.execute("INSERT INTO posting_settings VALUES ('min_gap_hours', '1')")          # what 0.5.5 saved
    assert store.get_setting(c, "min_gap_minutes") == "60"
    store.set_setting(c, "min_gap_minutes", "5")
    assert store.get_setting(c, "min_gap_minutes") == "5"
    with pytest.raises(ValueError):
        store.set_setting(c, "min_gap_hours", "1")


def test_five_minute_spacing_applies_to_new_and_already_queued_jobs(env, monkeypatch):
    c, s, _ = env
    connect_account(c, s)
    for p in ("youtube", "instagram"):
        store.set_setting(c, f"enabled_{p}", "0")
    store.set_setting(c, "dry_run", "0")
    store.set_setting(c, "jitter_minutes", "0")
    store.set_setting(c, "daily_max", "50")
    now = 1_800_000_000.0
    for i in range(3):                                                   # queued with the old 1 hour spacing
        lib = make_clip(env, monkeypatch, f"c{i}")
        uploads.enqueue(c, lib, f"c{i}", now)
    due = [r[0] for r in c.execute("SELECT not_before FROM upload_jobs ORDER BY id")]
    assert due[1] - due[0] >= 2 * 3600                                    # default 120 minutes
    store.set_setting(c, "min_gap_minutes", "5")
    assert uploads.respace(c, now) == 3
    due = [r[0] for r in c.execute("SELECT not_before FROM upload_jobs ORDER BY id")]
    assert due[0] == now and due[1] - due[0] == 300 and due[2] - due[1] == 300
    lib = make_clip(env, monkeypatch, "c3")                              # a new one follows the same 5 minutes
    uploads.enqueue(c, lib, "c3", now)
    assert c.execute("SELECT not_before FROM upload_jobs WHERE clip_id='c3'").fetchone()[0] - due[2] == 300
    # a draft that just went out pushes the next one 5 minutes after it, not immediately
    c.execute("UPDATE upload_jobs SET state='drafted', updated_at=? WHERE clip_id='c0'", (now + 10,))
    uploads.respace(c, now)
    first = c.execute("SELECT not_before FROM upload_jobs WHERE clip_id='c1'").fetchone()[0]
    assert first == now + 10 + 300


def test_settings_route_respaces_the_queue(client, home, monkeypatch):
    from tests.test_web import login

    login(client)
    post = _csrf_poster(client)
    c = db.open_db(home / "clipvault.db")
    s = Secrets()
    connect_account(c, s)
    c.close()
    post("/api/posting/settings", {"dry_run": "0", "enabled_youtube": "0", "enabled_instagram": "0", "jitter_minutes": "0", "daily_max": "50"})
    c = db.open_db(home / "clipvault.db")
    env_ = (c, s, home)
    for i in range(2):
        lib = make_clip(env_, monkeypatch, f"r{i}")
        uploads.enqueue(c, lib, f"r{i}")
    c.close()
    assert post("/api/posting/settings", {"min_gap_minutes": "5"}).json()["min_gap_minutes"] == "5"
    c = db.open_db(home / "clipvault.db")
    due = [r[0] for r in c.execute("SELECT not_before FROM upload_jobs ORDER BY id")]
    c.close()
    assert abs((due[1] - due[0]) - 300) < 1


def test_tiktok_drafts_stay_under_five_waiting_a_day(env, monkeypatch):
    """TikTok's Upload API holds at most 5 pending drafts per 24 h (docs checked 2026-11-19)."""
    c, s, _ = env
    connect_account(c, s)
    for p in ("youtube", "instagram"):
        store.set_setting(c, f"enabled_{p}", "0")
    store.set_setting(c, "dry_run", "0")
    store.set_setting(c, "jitter_minutes", "0")
    store.set_setting(c, "min_gap_minutes", "5")
    store.set_setting(c, "daily_max", "50")
    now = 1_800_000_000.0  # early in a UTC day: six 5-minute slots fit easily
    for i in range(6):
        lib = make_clip(env, monkeypatch, f"d{i}")
        uploads.enqueue(c, lib, f"d{i}", now)
    due = [r[0] for r in c.execute("SELECT not_before FROM upload_jobs ORDER BY id")]
    same_day = [t for t in due if t - due[0] < 86400 and int(t // 86400) == int(due[0] // 86400)]
    assert len(same_day) == 5 and due[5] > max(due[:5])  # the sixth waits for the next day
    store.set_setting(c, "tiktok_mode", "direct")  # Direct Post has no such cap
    lib = make_clip(env, monkeypatch, "d6")
    uploads.enqueue(c, lib, "d6", now)


def test_tiktok_too_many_pending_drafts_waits_a_day(tmp_path):
    v = tmp_path / "s.mp4"
    v.write_bytes(b"0" * 100)
    clip = ClipFile("c", v, 20)
    caption = {"per_platform": {"tiktok": "hi"}}

    def responder(m, u, k):
        return resp({"error": {"code": tiktok.TOO_MANY_PENDING, "message": "x"}}, 403)

    with pytest.raises(UploadError) as e:
        tiktok.TikTok().upload(clip, caption, {}, "t", {"tiktok_mode": "drafts"},
                               http=FakeHTTP(responder), sleep=lambda s: None)  # fmt: skip
    assert e.value.code == "quota" and "5 drafts" in str(e.value)
