"""Twitch app access token (client credentials flow) with automatic renewal.

Docs: https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/
An app token is enough for public data (users, clips, games). It cannot be
refreshed; when it nears expiry or Twitch rejects it, a new one is requested.
The token is kept in the OS keychain so each CLI run doesn't request a new one.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from clipvault import config as cfg
from clipvault.logging_setup import ctx
from clipvault.timeutil import parse_rfc3339, to_rfc3339, utc_now

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
RENEW_MARGIN = timedelta(minutes=10)
_SECRET_NAME = "twitch_app_token"

log = logging.getLogger(__name__)


class TwitchAuthError(Exception):
    """Twitch refused the app's credentials; retrying won't help until they are fixed."""


class _TokenEndpointDown(Exception):
    """Temporary failure reaching the token endpoint."""


class AppTokenProvider:
    """Hands out a valid app access token, requesting a new one when needed.

    Safe to share between threads.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        http: httpx.Client,
        *,
        load: Callable[[], str | None] | None = None,
        save: Callable[[str | None], None] | None = None,
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._load = load or _load_from_keyring
        self._save = save or _save_to_keyring
        self._now = now
        self._sleep = sleep
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._loaded = False

    def get(self) -> str:
        """Return a token valid for at least the next 10 minutes."""
        with self._lock:
            if not self._loaded:
                self._read_stored()
            if self._token and self._expires_at and self._now() < self._expires_at - RENEW_MARGIN:
                return self._token
            return self._request_new()

    def invalidate(self) -> None:
        """Forget the current token (call when Twitch answers 401)."""
        with self._lock:
            self._loaded = True
            self._token = None
            self._expires_at = None
            self._save(None)

    def _read_stored(self) -> None:
        self._loaded = True
        try:
            stored = json.loads(self._load() or "null")
            if stored and stored.get("client_id") == self._client_id:
                self._token = stored["access_token"]
                self._expires_at = parse_rfc3339(stored["expires_at"])
        except (ValueError, KeyError, TypeError):
            self._token = None
            self._expires_at = None

    def _request_new(self) -> str:
        retry_kwargs = {"sleep": self._sleep} if self._sleep else {}
        retryer = Retrying(
            retry=retry_if_exception_type(_TokenEndpointDown),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, max=10),
            reraise=True,
            **retry_kwargs,
        )
        try:
            payload = retryer(self._post_once)
        except _TokenEndpointDown as exc:
            raise TwitchAuthError(f"could not reach Twitch to get an access token: {exc}") from exc
        try:
            token = payload["access_token"]
            expires_at = self._now() + timedelta(seconds=int(payload["expires_in"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise TwitchAuthError("Twitch returned an unexpected token response") from exc
        self._token, self._expires_at = token, expires_at
        self._save(
            json.dumps(
                {
                    "client_id": self._client_id,
                    "access_token": token,
                    "expires_at": to_rfc3339(expires_at),
                }
            )
        )
        log.info("new Twitch app token obtained", extra=ctx(job="twitch_auth"))
        return token

    def _post_once(self) -> dict:
        try:
            # Credentials go in the form body, never in the URL (URLs end up in logs).
            response = self._http.post(
                TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credentials",
                },
            )
        except httpx.TransportError as exc:
            raise _TokenEndpointDown(type(exc).__name__) from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise _TokenEndpointDown(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise TwitchAuthError(
                f"Twitch rejected the client ID or secret (HTTP {response.status_code}: "
                f"{_message(response)}). Set them again with "
                "'python -m clipvault secrets set twitch_client_id' and "
                "'python -m clipvault secrets set twitch_client_secret'."
            )
        try:
            return response.json()
        except ValueError as exc:
            raise TwitchAuthError("Twitch returned an unreadable token response") from exc


def _message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message", ""))[:200]
    except (ValueError, AttributeError):
        return ""


def _load_from_keyring() -> str | None:
    return cfg.get_secret(_SECRET_NAME)


def _save_to_keyring(value: str | None) -> None:
    if value is None:
        cfg.delete_secret(_SECRET_NAME)
    else:
        cfg.set_secret(_SECRET_NAME, value)
