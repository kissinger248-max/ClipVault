"""ClipVault Web, W4: who may start a run (limits, the CAPTCHA, the queue cap) and the headers."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from clipvault.render import ffmpeg
from clipvault.render.ffmpeg import MediaInfo
from clipvault_web import limits, store
from clipvault_web.server import create_app
from clipvault_web.settings import WebSettings

UPLOAD = {"X-File-Name": "stream.mp4", "X-Rights": "yes"}


@pytest.fixture(autouse=True)
def probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ffmpeg, "probe", lambda p: MediaInfo(1280, 720, 30.0, 600.0, True, "h264"))


def site(tmp_path: Path, http: httpx.Client | None = None, **overrides) -> TestClient:
    settings = WebSettings(data=tmp_path / "web", **overrides)
    return TestClient(create_app(settings, start_worker=False, http=http))


def upload(client: TestClient, **headers) -> httpx.Response:
    return client.post("/api/upload", content=b"video", headers={**UPLOAD, **headers})


def finish_all(client: TestClient) -> None:
    conn = store.open_store(client.app.state.worker.settings)
    conn.execute("UPDATE runs SET status = 'done'")
    conn.close()


def test_one_run_at_a_time_then_the_daily_limit(tmp_path: Path) -> None:
    client = site(tmp_path, runs_per_day=2)
    assert upload(client).status_code == 200
    busy = upload(client)
    assert busy.status_code == 429 and busy.json()["kind"] == "one-at-a-time"
    finish_all(client)
    assert upload(client).status_code == 200
    finish_all(client)
    limited = upload(client)
    assert limited.status_code == 429 and limited.json()["kind"] == "limit"
    assert (
        "2 free runs for today" in limited.json()["error"] and "free app" in limited.json()["error"]
    )


def test_a_full_queue_turns_new_runs_away_and_the_home_page_says_so(tmp_path: Path) -> None:
    client = site(tmp_path, max_queue=1, one_run_at_a_time=False)
    assert "very busy" not in client.get("/").text
    assert upload(client).status_code == 200
    full = upload(client)
    assert full.status_code == 503 and full.json()["kind"] == "busy"
    assert "very busy" in client.get("/").text


def test_a_blocked_visitor_can_not_start_runs(tmp_path: Path) -> None:
    client = site(tmp_path)
    run_id = upload(client).json()["id"]
    conn = store.open_store(client.app.state.worker.settings)
    visitor = store.get_run(conn, run_id)["visitor"]
    conn.execute("INSERT INTO blocked (visitor, reason, created_at) VALUES (?, 'test', 'now')",
                 (visitor,))  # fmt: skip
    conn.close()
    refused = upload(client)
    assert refused.status_code == 403 and refused.json()["kind"] == "blocked"


def turnstile(answer: dict, seen: list) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(dict(x.split("=", 1) for x in request.content.decode().split("&")))
        return httpx.Response(200, json=answer)

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_the_captcha_is_checked_with_cloudflare_when_its_keys_are_set(tmp_path: Path) -> None:
    seen: list = []
    keys = {"turnstile_site_key": "site-key", "turnstile_secret": "secret-key"}
    client = site(tmp_path / "a", turnstile({"success": True}, seen), **keys)
    assert 'data-sitekey="site-key"' in client.get("/").text
    missing = upload(client)
    assert missing.status_code == 400 and missing.json()["kind"] == "check" and seen == []
    assert upload(client, **{"X-Turnstile": "good-token"}).status_code == 200
    assert seen[0]["secret"] == "secret-key" and seen[0]["response"] == "good-token"

    failing = site(tmp_path / "b", turnstile({"success": False}, []), **keys)
    assert upload(failing, **{"X-Turnstile": "bad-token"}).json()["kind"] == "check"
    plain = site(tmp_path / "c")  # no keys: no check, no widget
    assert "cf-turnstile" not in plain.get("/").text and upload(plain).status_code == 200


def test_search_is_rate_limited_per_visitor() -> None:
    limiter = limits.RateLimiter(per_minute=3)
    assert [limiter.allow("a", now=t) for t in (0, 1, 2, 3)] == [True, True, True, False]
    assert limiter.allow("b", now=3)  # someone else is fine
    assert limiter.allow("a", now=61)  # a minute later again


def test_pages_carry_safety_headers(tmp_path: Path) -> None:
    client = site(tmp_path)
    headers = client.get("/").headers
    policy = headers["content-security-policy"]
    assert "default-src 'self'" in policy and "frame-ancestors 'none'" in policy
    assert "https://clips.twitch.tv" in policy and "unsafe-inline" not in policy
    assert headers["x-frame-options"] == "DENY" and headers["x-content-type-options"] == "nosniff"


def test_the_salt_survives_a_restart(tmp_path: Path) -> None:
    first = site(tmp_path)
    run_a = upload(first).json()["id"]
    second = site(tmp_path, one_run_at_a_time=False)  # the same data folder, a new process
    run_b = upload(second).json()["id"]
    conn = store.open_store(second.app.state.worker.settings)
    visitors = {store.get_run(conn, r)["visitor"] for r in (run_a, run_b)}
    conn.close()
    assert len(visitors) == 1  # the same visitor, so daily limits still count


def test_old_runs_forget_their_visitor_and_ended_sign_ins_go(tmp_path: Path) -> None:
    from datetime import timedelta

    from clipvault.timeutil import to_rfc3339, utc_now

    client = site(tmp_path)
    run_id = upload(client).json()["id"]
    conn = store.open_store(client.app.state.worker.settings)
    ended = to_rfc3339(utc_now() - timedelta(days=1))
    conn.execute(
        "INSERT INTO sessions (id, twitch_user_id, login, created_at, expires_at) "
        "VALUES ('old', '1', 'kai', 'x', ?)",
        (ended,),
    )
    store.forget(conn)
    assert store.get_run(conn, run_id)["visitor"]  # still needed for today's limit
    store.forget(conn, utc_now() + timedelta(days=8))
    assert store.get_run(conn, run_id)["visitor"] is None
    assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    conn.close()
