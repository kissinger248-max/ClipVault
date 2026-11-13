"""Small client for the Twitch Helix API (official API only: hard rule 3).

Docs checked 2026-11-12:
- https://dev.twitch.tv/docs/api/reference/clips.md
- https://dev.twitch.tv/docs/api/reference/users.md
- https://dev.twitch.tv/docs/api/reference/games.md
- https://dev.twitch.tv/docs/api/reference/videos (checked 2026-11-13: app token works;
  ``duration`` like "3h8m33s"; ``muted_segments`` is always null with an app token)
- https://dev.twitch.tv/docs/api/reference/streams
- https://dev.twitch.tv/docs/api/guide/ (rate limits, pagination)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from typing import Any, Protocol

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt

from clipvault.logging_setup import ctx
from clipvault.timeutil import to_rfc3339
from clipvault.twitch.auth import TwitchAuthError

API_BASE = "https://api.twitch.tv/helix"
MAX_IDS_PER_REQUEST = 100
MAX_PAGE_SIZE = 100
MAX_RATE_LIMIT_WAIT_S = 60.0

log = logging.getLogger(__name__)


class TwitchAPIError(Exception):
    """A Helix request failed for good (after retries, or with a 4xx answer)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TokenSource(Protocol):
    """What the client needs from a token provider."""

    def get(self) -> str: ...

    def invalidate(self) -> None: ...


class _Transient(Exception):
    """A failure worth retrying; ``wait_s`` overrides the default backoff."""

    def __init__(self, message: str, wait_s: float | None = None) -> None:
        super().__init__(message)
        self.wait_s = wait_s


def _wait(state: RetryCallState) -> float:
    exc = state.outcome.exception() if state.outcome else None
    if isinstance(exc, _Transient) and exc.wait_s is not None:
        return exc.wait_s
    return float(min(2 ** (state.attempt_number - 1), 30))


class TwitchClient:
    """Read-only Helix calls with retries, rate-limit waits and token renewal."""

    def __init__(
        self,
        client_id: str,
        tokens: TokenSource,
        http: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        max_attempts: int = 4,
    ) -> None:
        self._client_id = client_id
        self._tokens = tokens
        self._http = http
        self._sleep = sleep
        self._clock = clock
        self._max_attempts = max_attempts

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    # --- endpoints ---------------------------------------------------------

    def get_users(
        self, *, logins: Iterable[str] = (), ids: Iterable[str] = ()
    ) -> list[dict[str, Any]]:
        """Look up users by login and/or id. Deleted or suspended accounts are left out."""
        found: list[dict[str, Any]] = []
        for key, values in (("login", list(logins)), ("id", list(ids))):
            for chunk in _chunks(values, MAX_IDS_PER_REQUEST):
                found += self._get("/users", [(key, v) for v in chunk]).get("data", [])
        return found

    def get_games(self, ids: Iterable[str]) -> dict[str, str]:
        """Return ``{game_id: name}`` for the ids Twitch knows."""
        names: dict[str, str] = {}
        for chunk in _chunks(sorted(set(ids)), MAX_IDS_PER_REQUEST):
            for game in self._get("/games", [("id", v) for v in chunk]).get("data", []):
                names[game["id"]] = game["name"]
        return names

    def get_archives(self, user_id: str, first: int = 20) -> list[dict[str, Any]]:
        """A broadcaster's most recent past broadcasts (VODs), newest first."""
        params = [("user_id", user_id), ("type", "archive"), ("sort", "time")]
        params.append(("first", str(min(max(first, 1), MAX_PAGE_SIZE))))
        return self._get("/videos", params).get("data", [])

    def get_live_stream_ids(self, user_ids: Iterable[str]) -> dict[str, str]:
        """``{user_id: stream_id}`` for the broadcasters that are live right now."""
        live: dict[str, str] = {}
        for chunk in _chunks(sorted(set(user_ids)), MAX_IDS_PER_REQUEST):
            params = [("user_id", v) for v in chunk] + [("first", str(MAX_PAGE_SIZE))]
            for stream in self._get("/streams", params).get("data", []):
                live[stream["user_id"]] = stream["id"]
        return live

    def iter_clips(
        self,
        broadcaster_id: str,
        started_at: datetime,
        ended_at: datetime,
        *,
        page_size: int = MAX_PAGE_SIZE,
        max_pages: int = 10,
    ) -> Iterator[dict[str, Any]]:
        """Yield a broadcaster's clips created in the window, highest view count first.

        Pages are fetched lazily, so the caller can stop early. Twitch returns at
        most about 1,000 results per window; use shorter windows to see more.
        The list is live, so a clip can show up on two pages: each id is yielded once.
        """
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(max_pages):
            params = [
                ("broadcaster_id", broadcaster_id),
                ("started_at", to_rfc3339(started_at)),
                ("ended_at", to_rfc3339(ended_at)),
                ("first", str(min(page_size, MAX_PAGE_SIZE))),
            ]
            if cursor:
                params.append(("after", cursor))
            body = self._get("/clips", params)
            for clip in body.get("data", []):
                if clip["id"] not in seen:
                    seen.add(clip["id"])
                    yield clip
            cursor = (body.get("pagination") or {}).get("cursor")
            if not cursor or not body.get("data"):
                return

    # --- plumbing ----------------------------------------------------------

    def _get(self, path: str, params: list[tuple[str, str]]) -> dict[str, Any]:
        state = {"token_renewed": False}
        retryer = Retrying(
            retry=retry_if_exception_type(_Transient),
            stop=stop_after_attempt(self._max_attempts),
            wait=_wait,
            sleep=self._sleep,
            reraise=True,
        )
        try:
            return retryer(self._get_once, path, params, state)
        except _Transient as exc:
            raise TwitchAPIError(f"GET {path} kept failing: {exc}") from exc

    def _get_once(
        self, path: str, params: list[tuple[str, str]], state: dict[str, bool]
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._tokens.get()}", "Client-Id": self._client_id}
        try:
            response = self._http.get(API_BASE + path, params=params, headers=headers)
        except httpx.TransportError as exc:
            raise _Transient(f"network error ({type(exc).__name__})") from exc

        status = response.status_code
        if status == 401:
            if state["token_renewed"]:
                raise TwitchAuthError("Twitch rejected a freshly issued access token (HTTP 401)")
            state["token_renewed"] = True
            self._tokens.invalidate()
            raise _Transient("access token rejected (HTTP 401)", wait_s=0)
        if status == 429:
            wait_s = self._rate_limit_wait(response)
            log.warning(
                "Twitch rate limit hit; waiting %.0f s", wait_s, extra=ctx(job="twitch_api")
            )
            raise _Transient("rate limited (HTTP 429)", wait_s=wait_s)
        if status >= 500:
            raise _Transient(f"Twitch server error (HTTP {status})")
        if status >= 400:
            raise TwitchAPIError(f"GET {path} failed: HTTP {status} {_message(response)}", status)
        try:
            return response.json()
        except ValueError as exc:
            raise _Transient("unreadable response body") from exc

    def _rate_limit_wait(self, response: httpx.Response) -> float:
        """Seconds until the bucket refills, from the Ratelimit-Reset header (epoch seconds)."""
        try:
            wait_s = float(response.headers.get("Ratelimit-Reset", "")) - self._clock()
        except ValueError:
            wait_s = 5.0
        return max(1.0, min(wait_s, MAX_RATE_LIMIT_WAIT_S))


def _chunks(values: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message", ""))[:300]
    except (ValueError, AttributeError):
        return ""
