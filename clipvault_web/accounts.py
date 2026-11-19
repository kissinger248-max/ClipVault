"""Signing in with Twitch (authorization code flow) and the website's sessions.

Two reasons to sign in, and only these:
- **My channel**: a streamer makes clips of their own channel with Twitch's official
  download, which needs the ``channel:manage:clips`` scope. The token is kept with the
  session (a few hours at most) and copied to the run, which deletes it when it ends.
- **Opting out**: proving a channel is yours. No scope; the token is revoked at once.

Docs checked 2026-11-19: https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/
(authorization code grant) and .../revoke-tokens/.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import httpx

from clipvault.timeutil import parse_rfc3339, to_rfc3339, utc_now

from . import store, twitch_web
from .settings import WebSettings

AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
REVOKE_URL = "https://id.twitch.tv/oauth2/revoke"
SCOPES = {"mine": "channel:manage:clips", "optout": ""}
SESSION_COOKIE = "cv_session"
STATE_COOKIE = "cv_oauth"
SESSION_DAYS = 30


class SignInFailed(Exception):
    """The sign-in didn't work; the message is shown."""


@dataclass(frozen=True)
class Session:
    id: str
    user_id: str
    login: str
    name: str
    token: str | None  # only for "My channel"; None once expired

    @property
    def can_download(self) -> bool:
        return bool(self.token)


def redirect_uri(settings: WebSettings) -> str:
    return f"{settings.public_url}/auth/twitch/callback"


def authorize_url(settings: WebSettings, purpose: str, state: str) -> str:
    params = {
        "client_id": settings.twitch_client_id or "",
        "redirect_uri": redirect_uri(settings),
        "response_type": "code",
        "scope": SCOPES[purpose],
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def new_state(purpose: str) -> tuple[str, str]:
    """(state for Twitch, value for the short-lived cookie that must match it)."""
    state = secrets.token_urlsafe(24)
    return state, f"{state}:{purpose}"


def check_state(cookie: str | None, state: str | None) -> str:
    """The purpose of a sign-in whose state matches its cookie (CSRF protection)."""
    expected, _, purpose = (cookie or "").partition(":")
    if not state or not expected or not secrets.compare_digest(expected, state):
        raise SignInFailed("The sign-in expired or didn't come from this page. Please try again.")
    if purpose not in SCOPES:
        raise SignInFailed("Unknown sign-in.")
    return purpose


def finish_sign_in(
    conn: sqlite3.Connection, settings: WebSettings, code: str, purpose: str, http: httpx.Client
) -> Session:
    """Swap the code for a token, see whose it is, and start a session."""
    response = http.post(TOKEN_URL, data={
        "client_id": settings.twitch_client_id, "client_secret": settings.twitch_client_secret,
        "code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri(settings),
    })  # fmt: skip
    if response.status_code != 200:
        raise SignInFailed("Twitch didn't accept the sign-in. Please try again.")
    body = response.json()
    token = body.get("access_token")
    if not token:
        raise SignInFailed("Twitch didn't accept the sign-in. Please try again.")
    client = twitch_web.user_client(settings, token, http)
    user = client.get_token_user()
    if not user:
        raise SignInFailed("Twitch didn't say who signed in. Please try again.")
    keep = purpose == "mine" and "channel:manage:clips" in (body.get("scope") or [])
    if not keep:
        revoke(settings, token, http)
    now = utc_now()
    session_id = secrets.token_urlsafe(24)
    expires = int(body.get("expires_in") or 0)
    conn.execute(
        "INSERT INTO sessions (id, twitch_user_id, login, display_name, access_token, "
        "token_expires_at, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, user["id"], user["login"], user.get("display_name") or user["login"],
         token if keep else None,
         to_rfc3339(now + timedelta(seconds=expires)) if keep and expires else None,
         to_rfc3339(now), to_rfc3339(now + timedelta(days=SESSION_DAYS))),
    )  # fmt: skip
    return Session(session_id, user["id"], user["login"],
                   user.get("display_name") or user["login"], token if keep else None)  # fmt: skip


def revoke(settings: WebSettings, token: str, http: httpx.Client) -> None:
    """Tell Twitch to forget a token (best effort)."""
    try:
        http.post(REVOKE_URL, data={"client_id": settings.twitch_client_id, "token": token})
    except httpx.HTTPError:
        pass


def current(conn: sqlite3.Connection, session_id: str | None) -> Session | None:
    """The signed-in streamer for this cookie, if any (an expired token reads as None)."""
    if not session_id:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None or parse_rfc3339(row["expires_at"]) <= utc_now():
        return None
    token = row["access_token"]
    if token and row["token_expires_at"] and parse_rfc3339(row["token_expires_at"]) <= utc_now():
        token = None
    return Session(row["id"], row["twitch_user_id"], row["login"],
                   row["display_name"] or row["login"], token)  # fmt: skip


def sign_out(conn: sqlite3.Connection, settings: WebSettings, session_id: str | None,
             http: httpx.Client | None = None) -> None:  # fmt: skip
    row = conn.execute(
        "SELECT access_token FROM sessions WHERE id = ?", (session_id or "",)
    ).fetchone()
    if row and row["access_token"] and http is not None:
        revoke(settings, row["access_token"], http)
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id or "",))


def form_token(conn: sqlite3.Connection, session_id: str) -> str:
    """The hidden form value that proves a POST came from our own page."""
    return hashlib.sha256(f"{store.salt(conn)}|form|{session_id}".encode()).hexdigest()[:32]


def check_form(conn: sqlite3.Connection, session_id: str | None, value: str | None) -> bool:
    return bool(session_id and value) and secrets.compare_digest(
        form_token(conn, session_id or ""), value or ""
    )
