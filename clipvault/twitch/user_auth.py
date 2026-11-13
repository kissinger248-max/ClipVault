"""Logging the user in to Twitch, for reading live chat (M14).

Live chat comes through EventSub, which needs a *user* token with the
``user:read:chat`` scope; the app token can't read chat. The device-code flow is
used (docs checked 2026-11-13, /docs/authentication/getting-tokens-oauth/): the
app shows a short code, the user enters it at twitch.tv/activate, and the app
polls until it is approved. No redirect address and no HTTPS certificate needed.

Tokens are kept in the OS keychain (``twitch_user_token``) as JSON. Access
tokens last about 4 hours; refresh tokens are single-use, so every refresh
stores the new pair at once. A refused refresh means the user must log in again
(a "relogin" alert).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import timedelta

import httpx

from clipvault import config as cfg
from clipvault.logging_setup import ctx
from clipvault.timeutil import parse_rfc3339, to_rfc3339, utc_now

DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
SCOPES = ("user:read:chat",)
SECRET = "twitch_user_token"
RENEW_MARGIN = timedelta(minutes=10)
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

log = logging.getLogger(__name__)


class LoginNeeded(Exception):
    """No usable Twitch login: the user has to log in (again)."""


@dataclass(frozen=True)
class DeviceLogin:
    device_code: str
    user_code: str
    verification_uri: str
    interval: float
    expires_in: float


@dataclass(frozen=True)
class UserToken:
    access_token: str
    refresh_token: str
    expires_at: str  # RFC 3339
    user_id: str = ""
    login: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> UserToken:
        return cls(**json.loads(text))


def _credentials() -> tuple[str, str | None]:
    client_id = cfg.get_secret("twitch_client_id")
    if not client_id:
        raise LoginNeeded("the Twitch client ID is not set")
    return client_id, cfg.get_secret("twitch_client_secret")


def start_login(http: httpx.Client) -> DeviceLogin:
    """Ask Twitch for a code the user types in at twitch.tv/activate."""
    client_id, _secret = _credentials()
    response = http.post(DEVICE_URL, data={"client_id": client_id, "scopes": " ".join(SCOPES)})
    if response.status_code != 200:
        raise LoginNeeded(f"Twitch refused to start a login ({response.status_code}): "
                          f"{response.text[:200]}")  # fmt: skip
    body = response.json()
    return DeviceLogin(
        device_code=body["device_code"],
        user_code=body["user_code"],
        verification_uri=body["verification_uri"],
        interval=float(body.get("interval", 5)),
        expires_in=float(body.get("expires_in", 1800)),
    )


def finish_login(
    http: httpx.Client,
    login: DeviceLogin,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] = lambda: False,
) -> UserToken:
    """Wait until the user approves the code, then store and return the token."""
    client_id, secret = _credentials()
    data = {"client_id": client_id, "scopes": " ".join(SCOPES), "device_code": login.device_code,
            "grant_type": DEVICE_GRANT}  # fmt: skip
    if secret:
        data["client_secret"] = secret  # the app is registered as a confidential client
    deadline = clock() + login.expires_in
    while clock() < deadline and not cancelled():
        response = http.post(TOKEN_URL, data=data)
        if response.status_code == 200:
            token = _token_from(response.json(), http)
            store(token)
            log.info("Twitch login stored", extra=ctx(job="twitch_login", login=token.login))
            return token
        message = _message(response)
        if "authorization_pending" not in message and "slow_down" not in message:
            raise LoginNeeded(f"Twitch login failed: {message}")
        sleep(login.interval + (5 if "slow_down" in message else 0))
    raise LoginNeeded("the login code expired before it was entered")


def _token_from(body: dict, http: httpx.Client) -> UserToken:
    expires = utc_now() + timedelta(seconds=int(body.get("expires_in", 14400)))
    token = UserToken(body["access_token"], body.get("refresh_token", ""), to_rfc3339(expires))
    who = validate(http, token.access_token)
    return UserToken(token.access_token, token.refresh_token, token.expires_at,
                     who.get("user_id", ""), who.get("login", ""))  # fmt: skip


def validate(http: httpx.Client, access_token: str) -> dict:
    """Who the token belongs to (Twitch asks apps to validate user tokens)."""
    response = http.get(VALIDATE_URL, headers={"Authorization": f"OAuth {access_token}"})
    if response.status_code != 200:
        raise LoginNeeded("Twitch says the login is no longer valid")
    return response.json()


def _message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message", ""))
    except ValueError:
        return response.text[:200]


def stored() -> UserToken | None:
    text = cfg.get_secret(SECRET)
    if not text:
        return None
    try:
        return UserToken.from_json(text)
    except (ValueError, TypeError):
        return None


def store(token: UserToken) -> None:
    cfg.set_secret(SECRET, token.to_json())


def logout() -> None:
    cfg.delete_secret(SECRET)


class UserTokenProvider:
    """Hands out a valid user access token, refreshing it when needed (thread-safe)."""

    def __init__(self, http: httpx.Client) -> None:
        self._http = http
        self._lock = threading.Lock()
        self._token = stored()

    @property
    def logged_in(self) -> bool:
        return self._token is not None

    @property
    def user_id(self) -> str:
        if self._token is None:
            raise LoginNeeded("not logged in to Twitch")
        return self._token.user_id

    def get(self) -> str:
        with self._lock:
            if self._token is None:
                raise LoginNeeded("not logged in to Twitch")
            if parse_rfc3339(self._token.expires_at) - utc_now() < RENEW_MARGIN:
                self._refresh()
            return self._token.access_token

    def invalidate(self) -> None:
        """Twitch rejected the token: refresh it on the next ``get``."""
        with self._lock:
            if self._token is not None:
                self._token = UserToken(self._token.access_token, self._token.refresh_token,
                                        to_rfc3339(utc_now()), self._token.user_id,
                                        self._token.login)  # fmt: skip

    def _refresh(self) -> None:
        assert self._token is not None
        client_id, secret = _credentials()
        data = {"client_id": client_id, "grant_type": "refresh_token",
                "refresh_token": self._token.refresh_token}  # fmt: skip
        if secret:
            data["client_secret"] = secret
        try:
            response = self._http.post(TOKEN_URL, data=data)
        except httpx.HTTPError as exc:
            raise LoginNeeded(f"Twitch could not be reached to renew the login: {exc}") from exc
        if response.status_code != 200:
            self._token = None
            logout()
            raise LoginNeeded("Twitch refused to renew the login; please log in again")
        body = response.json()
        expires = utc_now() + timedelta(seconds=int(body.get("expires_in", 14400)))
        self._token = UserToken(body["access_token"], body.get("refresh_token", ""),
                                to_rfc3339(expires), self._token.user_id,
                                self._token.login)  # fmt: skip
        store(self._token)  # refresh tokens are single-use: save the new pair at once
