"""Twitch auth and API client, against recorded-shape fixtures and a mock transport."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from clipvault import twitch
from clipvault.twitch.api import TwitchAPIError, TwitchClient
from clipvault.twitch.auth import TOKEN_URL, AppTokenProvider, TwitchAuthError
from tests.twitch_fake import FakeTwitch, load_fixture, make_clip

START = datetime(2026, 11, 10, tzinfo=UTC)
END = datetime(2026, 11, 11, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class StaticTokens:
    def __init__(self) -> None:
        self.invalidated = 0

    def get(self) -> str:
        return f"tok{self.invalidated}"

    def invalidate(self) -> None:
        self.invalidated += 1


def scripted(responses: list) -> tuple[httpx.Client, list[httpx.Request]]:
    """An HTTP client that answers with ``responses`` in order (exceptions are raised)."""
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.Client(transport=httpx.MockTransport(handle)), seen


def make_client(responses: list, **kwargs) -> tuple[TwitchClient, list, list[float], StaticTokens]:
    http, seen = scripted(responses)
    sleeps: list[float] = []
    tokens = StaticTokens()
    client = TwitchClient("cid", tokens, http, sleep=sleeps.append, **kwargs)
    return client, seen, sleeps, tokens


# --- auth --------------------------------------------------------------------


def test_token_is_requested_once_cached_and_stored(memory_keyring) -> None:
    http, seen = scripted([httpx.Response(200, json=load_fixture("token.json"))])
    provider = AppTokenProvider("cid", "secret", http, now=Clock())
    assert provider.get() == "test-app-token-1"
    assert provider.get() == "test-app-token-1"
    assert len(seen) == 1

    request = seen[0]
    assert str(request.url) == TOKEN_URL  # nothing secret in the URL
    assert b"client_secret=secret" in request.content
    assert b"grant_type=client_credentials" in request.content

    stored = json.loads(memory_keyring.store[("clipvault", "twitch_app_token")])
    assert stored["access_token"] == "test-app-token-1"
    assert stored["client_id"] == "cid"


def test_stored_token_is_reused_by_a_new_provider() -> None:
    http, _ = scripted([httpx.Response(200, json=load_fixture("token.json"))])
    AppTokenProvider("cid", "secret", http, now=Clock()).get()
    idle_http, seen = scripted([])
    assert AppTokenProvider("cid", "secret", idle_http, now=Clock()).get() == "test-app-token-1"
    assert seen == []


def test_stored_token_for_another_client_id_is_ignored() -> None:
    http, _ = scripted([httpx.Response(200, json=load_fixture("token.json"))])
    AppTokenProvider("old-cid", "secret", http, now=Clock()).get()
    new_token = {"access_token": "fresh", "expires_in": 3600, "token_type": "bearer"}
    http2, _ = scripted([httpx.Response(200, json=new_token)])
    assert AppTokenProvider("cid", "secret", http2, now=Clock()).get() == "fresh"


def test_token_is_renewed_shortly_before_expiry() -> None:
    clock = Clock()
    first = {"access_token": "one", "expires_in": 3600, "token_type": "bearer"}
    second = {"access_token": "two", "expires_in": 3600, "token_type": "bearer"}
    http, seen = scripted([httpx.Response(200, json=first), httpx.Response(200, json=second)])
    provider = AppTokenProvider("cid", "secret", http, now=clock)
    assert provider.get() == "one"
    clock.now += timedelta(minutes=45)
    assert provider.get() == "one"
    clock.now += timedelta(minutes=10)  # 5 minutes left: inside the renewal margin
    assert provider.get() == "two"
    assert len(seen) == 2


def test_invalidate_forces_a_new_token(memory_keyring) -> None:
    second = {"access_token": "two", "expires_in": 3600, "token_type": "bearer"}
    http, _ = scripted(
        [httpx.Response(200, json=load_fixture("token.json")), httpx.Response(200, json=second)]
    )
    provider = AppTokenProvider("cid", "secret", http, now=Clock())
    provider.get()
    provider.invalidate()
    assert ("clipvault", "twitch_app_token") not in memory_keyring.store
    assert provider.get() == "two"


def test_bad_credentials_raise_with_fix_and_without_the_secret() -> None:
    http, seen = scripted([httpx.Response(403, json={"message": "invalid client secret"})])
    provider = AppTokenProvider("cid", "hunter2-secret", http, now=Clock())
    with pytest.raises(TwitchAuthError) as info:
        provider.get()
    assert "secrets set twitch_client_secret" in str(info.value)
    assert "hunter2-secret" not in str(info.value)
    assert len(seen) == 1  # a rejected secret is not retried


def test_token_endpoint_outage_is_retried_then_reported() -> None:
    ok = httpx.Response(200, json=load_fixture("token.json"))
    http, seen = scripted([httpx.ConnectError("down"), httpx.Response(503), ok])
    sleeps: list[float] = []
    provider = AppTokenProvider("cid", "secret", http, now=Clock(), sleep=sleeps.append)
    assert provider.get() == "test-app-token-1"
    assert len(seen) == 3 and len(sleeps) == 2

    http, _ = scripted([httpx.Response(500)] * 3)
    provider = AppTokenProvider("cid", "s", http, now=Clock(), sleep=sleeps.append, load=lambda: "")
    with pytest.raises(TwitchAuthError, match="could not reach Twitch"):
        provider.get()


# --- API client ----------------------------------------------------------------


def test_requests_carry_bearer_token_and_client_id() -> None:
    client, seen, _, _ = make_client([httpx.Response(200, json=load_fixture("users.json"))])
    users = client.get_users(logins=["valkyrae", "xqc", "kaicenat"])
    assert [(u["id"], u["display_name"]) for u in users] == [
        ("79615025", "Valkyrae"),
        ("71092938", "xQc"),
        ("641972806", "KaiCenat"),
    ]
    assert seen[0].headers["Authorization"] == "Bearer tok0"
    assert seen[0].headers["Client-Id"] == "cid"
    assert seen[0].url.query == b"login=valkyrae&login=xqc&login=kaicenat"


def test_user_lookups_are_split_into_groups_of_100() -> None:
    empty = httpx.Response(200, json={"data": []})
    client, seen, _, _ = make_client([empty, empty, empty])
    client.get_users(logins=[f"user{i}" for i in range(150)], ids=["7"])
    counts = [len(r.url.params.multi_items()) for r in seen]
    assert counts == [100, 50, 1]


def test_get_games_maps_ids_to_names() -> None:
    client, _, _, _ = make_client([httpx.Response(200, json=load_fixture("games.json"))])
    assert client.get_games(["32982", "32982"]) == {"32982": "Grand Theft Auto V"}


def test_clips_fixture_fields_are_passed_through() -> None:
    client, seen, _, _ = make_client([httpx.Response(200, json=load_fixture("clips.json"))])
    clips = list(client.iter_clips("79615025", START, END))
    assert [c["id"] for c in clips] == [
        "TawdryMuddyDogePJSalt-KjqqboPnH55MrIKY",
        "RoundKawaiiLyrebirdNononoCat-Y_5ujEcvPgBVzgtZ",
        "EsteemedUglySoybeanSoonerLater-fLOgvRkrxwzg4aKt",
    ]
    params = dict(seen[0].url.params)
    assert params == {
        "broadcaster_id": "79615025",
        "started_at": "2026-11-10T00:00:00Z",
        "ended_at": "2026-11-11T00:00:00Z",
        "first": "100",
    }


def test_clip_pagination_follows_cursor_and_drops_repeats() -> None:
    a, b, c = (
        make_clip(i, "1", "2026-11-10T10:00:00Z", v) for i, v in (("a", 3), ("b", 2), ("c", 1))
    )
    pages = [
        httpx.Response(200, json={"data": [a, b], "pagination": {"cursor": "p2"}}),
        httpx.Response(200, json={"data": [b, c], "pagination": {}}),
    ]
    client, seen, _, _ = make_client(pages)
    assert [clip["id"] for clip in client.iter_clips("1", START, END)] == ["a", "b", "c"]
    assert seen[1].url.params["after"] == "p2"


def test_clip_pagination_stops_at_max_pages() -> None:
    page = httpx.Response(
        200,
        json={
            "data": [make_clip("a", "1", "2026-11-10T10:00:00Z", 1)],
            "pagination": {"cursor": "x"},
        },
    )
    client, seen, _, _ = make_client([page, page, page])
    list(client.iter_clips("1", START, END, max_pages=2))
    assert len(seen) == 2


def test_401_renews_the_token_once_then_succeeds() -> None:
    client, seen, sleeps, tokens = make_client(
        [
            httpx.Response(401, json={"message": "Invalid OAuth token"}),
            httpx.Response(200, json={"data": []}),
        ]
    )
    assert client.get_users(logins=["x"]) == []
    assert tokens.invalidated == 1
    assert seen[1].headers["Authorization"] == "Bearer tok1"
    assert sleeps == [0]


def test_second_401_is_an_auth_error() -> None:
    client, _, _, _ = make_client([httpx.Response(401), httpx.Response(401)])
    with pytest.raises(TwitchAuthError):
        client.get_users(logins=["x"])


def test_429_waits_until_the_reset_time() -> None:
    limited = httpx.Response(429, headers={"Ratelimit-Reset": "1012"}, json={"message": "slow"})
    client, seen, sleeps, _ = make_client(
        [limited, httpx.Response(200, json={"data": []})], clock=lambda: 1000.0
    )
    client.get_users(logins=["x"])
    assert sleeps == [12.0]
    assert len(seen) == 2


def test_rate_limit_wait_is_bounded() -> None:
    far = httpx.Response(429, headers={"Ratelimit-Reset": "99999"})
    missing = httpx.Response(429)
    ok = httpx.Response(200, json={"data": []})
    client, _, sleeps, _ = make_client([far, missing, ok], clock=lambda: 1000.0)
    client.get_users(logins=["x"])
    assert sleeps == [60.0, 5.0]


def test_server_errors_and_network_errors_are_retried() -> None:
    client, seen, sleeps, _ = make_client(
        [httpx.Response(503), httpx.ReadTimeout("slow"), httpx.Response(200, json={"data": []})]
    )
    assert client.get_users(logins=["x"]) == []
    assert len(seen) == 3
    assert sleeps == [1.0, 2.0]


def test_gives_up_after_max_attempts() -> None:
    client, seen, _, _ = make_client([httpx.Response(500)] * 4)
    with pytest.raises(TwitchAPIError, match="kept failing"):
        client.get_users(logins=["x"])
    assert len(seen) == 4


def test_4xx_is_not_retried_and_reports_twitch_message() -> None:
    bad = httpx.Response(400, json={"status": 400, "message": "started_at must be RFC3339"})
    client, seen, _, _ = make_client([bad])
    with pytest.raises(TwitchAPIError, match="started_at must be RFC3339") as info:
        list(client.iter_clips("1", START, END))
    assert info.value.status == 400
    assert len(seen) == 1


# --- factory -------------------------------------------------------------------


def test_build_client_needs_stored_keys() -> None:
    with pytest.raises(twitch.MissingCredentials, match="secrets set twitch_client_id"):
        twitch.build_client()


def test_build_client_works_end_to_end_with_stored_keys(memory_keyring) -> None:
    memory_keyring.set_password("clipvault", "twitch_client_id", "cid")
    memory_keyring.set_password("clipvault", "twitch_client_secret", "secret")
    fake = FakeTwitch()
    fake.add_user("1", "somebody")
    client = twitch.build_client(http=fake.client())
    assert client.get_users(logins=["somebody"])[0]["id"] == "1"
    assert fake.tokens_issued == 1
