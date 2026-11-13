"""Sign in with Twitch: the built-in public app, used when there are no own developer keys."""

from __future__ import annotations

import re
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from clipvault import config as cfg
from clipvault import doctor, twitch
from clipvault.timeutil import to_rfc3339, utc_now
from clipvault.twitch import user_auth
from clipvault.twitch.auth import AppTokenProvider
from tests.test_web import csrf, login

BUILT_IN = "publicclientid"


@pytest.fixture
def built_in_app(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(cfg, "bundled_twitch_client_id", lambda: BUILT_IN)
    return BUILT_IN


def signed_in(user: str = "viewer", minutes: int = 240) -> user_auth.UserToken:
    token = user_auth.UserToken("a1", "r1", to_rfc3339(utc_now() + timedelta(minutes=minutes)),
                                "42", user)  # fmt: skip
    user_auth.store(token)
    return token


def test_the_id_file_takes_the_first_line_that_is_not_a_comment() -> None:
    assert cfg.first_setting_line("# note\n\n  abc123  \nxyz\n") == "abc123"
    assert cfg.first_setting_line("# only comments\n\n") is None


def test_sign_in_uses_the_built_in_app_without_a_secret(built_in_app: str) -> None:
    bodies: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        if request.url.path == "/oauth2/device":
            return httpx.Response(
                200,
                json={
                    "device_code": "dev",
                    "user_code": "ABCD",
                    "verification_uri": "https://www.twitch.tv/activate",
                },
            )
        if request.url.path == "/oauth2/validate":
            return httpx.Response(200, json={"login": "viewer", "user_id": "42"})
        return httpx.Response(200, json={"access_token": "a1", "refresh_token": "r1"})

    http = httpx.Client(transport=httpx.MockTransport(handle))
    user_auth.finish_login(http, user_auth.start_login(http), sleep=lambda s: None)
    posts = [b for b in bodies if b]
    assert all(b["client_id"] == built_in_app for b in posts)
    assert not any("client_secret" in b for b in posts)
    assert user_auth.stored().login == "viewer"


def test_own_keys_win_over_the_built_in_app(built_in_app: str) -> None:
    cfg.set_secret("twitch_client_id", "mine")
    cfg.set_secret("twitch_client_secret", "shh")
    assert user_auth.client_id() == "mine"
    client = twitch.build_client()
    try:
        assert client._client_id == "mine" and isinstance(client._tokens, AppTokenProvider)
    finally:
        client.close()


def test_without_keys_clip_finding_needs_a_sign_in_then_uses_it(built_in_app: str) -> None:
    with pytest.raises(twitch.MissingCredentials, match="Sign in with Twitch"):
        twitch.build_client()
    assert not twitch.connected()
    signed_in()
    assert twitch.connected()
    seen: list[httpx.Request] = []

    def helix(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "1", "login": "alpha",
                                                   "display_name": "Alpha"}]})  # fmt: skip

    client = twitch.build_client(httpx.Client(transport=httpx.MockTransport(helix)))
    try:
        client.get_users(logins=["alpha"])
    finally:
        client.close()
    assert seen[0].headers["Client-Id"] == built_in_app
    assert seen[0].headers["Authorization"] == "Bearer a1"


def test_with_neither_keys_nor_a_built_in_app_the_old_message_stays() -> None:
    with pytest.raises(twitch.MissingCredentials, match="dev.twitch.tv/console"):
        twitch.build_client()


def test_a_renewal_made_by_one_part_is_reused_by_the_other(built_in_app: str) -> None:
    signed_in(minutes=2)  # due for renewal
    renewed = iter([httpx.Response(200, json={"access_token": "a2", "refresh_token": "r2",
                                              "expires_in": 14400})])  # fmt: skip
    finder = user_auth.UserTokenProvider(httpx.Client(transport=httpx.MockTransport(
        lambda request: next(renewed))))  # fmt: skip

    def must_not_post(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the used refresh token was sent again")

    chat = user_auth.UserTokenProvider(httpx.Client(transport=httpx.MockTransport(must_not_post)))
    assert finder.get() == "a2"
    assert chat.get() == "a2"  # took the stored renewal instead of spending r1 again
    assert user_auth.stored().refresh_token == "r2"


def test_doctor_reports_the_sign_in(built_in_app: str) -> None:
    assert doctor.check_twitch_keys().status == doctor.WARN
    signed_in("friend")
    result = doctor.check_twitch_keys()
    assert result.status == doctor.OK and "friend" in result.detail


def test_setup_step_2_offers_sign_in_then_shows_who_is_signed_in(
    client: TestClient, built_in_app: str
) -> None:
    login(client)
    page = client.get("/setup/2").text
    assert "Sign In with Twitch" in page and 'action="/settings/twitch-login"' in page
    assert 'name="next" value="/setup/2"' in page
    assert 'name="client_id"' in page  # own developer keys stay possible
    signed_in("friend")
    page = client.get("/setup/2").text
    assert "Signed in as friend" in page and 'href="/setup/3"' in page


def test_setup_step_2_without_a_built_in_app_asks_for_keys(client: TestClient) -> None:
    login(client)
    page = client.get("/setup/2").text
    assert "Sign In with Twitch" not in page and "Check and Continue" in page


def test_settings_show_the_twitch_account_in_sign_in_mode(
    client: TestClient, built_in_app: str
) -> None:
    login(client)
    assert "Not signed in. ClipVault needs your Twitch sign-in" in client.get("/settings").text
    signed_in("friend")
    page = client.get("/settings").text
    assert "Signed in as friend" in page and "Sign Out" in page
    assert "Optional. ClipVault uses your Twitch sign-in instead." in page


def test_a_sign_in_can_only_return_into_the_panel(
    client: TestClient, built_in_app: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    monkeypatch.setattr(
        user_auth,
        "start_login",
        lambda http: (_ for _ in ()).throw(user_auth.LoginNeeded("offline")),
    )
    token = csrf(client)
    reply = client.post("/settings/twitch-login", data={"csrf": token, "next": "//evil.example"})
    assert reply.headers["location"].startswith("/settings?msg=")
    assert reply.headers["location"].endswith("#twitch-login")  # message before the section
    reply = client.post("/settings/twitch-login", data={"csrf": token, "next": "/setup/2"})
    assert re.match(r"^/setup/2\?msg=", reply.headers["location"])
