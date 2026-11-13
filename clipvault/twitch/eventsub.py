"""Live chat over Twitch EventSub WebSocket (docs checked 2026-11-13).

- Connect to ``wss://eventsub.wss.twitch.tv/ws``; the first message is
  ``session_welcome`` with the session id. Subscriptions must be created within
  10 seconds, through Helix ``POST /eventsub/subscriptions`` with a user token.
- ``channel.chat.message`` v1, condition ``broadcaster_user_id`` + ``user_id``
  (the logged-in user, whose token has ``user:read:chat``).
- ``session_keepalive`` arrives when nothing else does; pings are answered by the
  websockets library. ``session_reconnect`` gives a new URL to move to within 30 s;
  subscriptions carry over. Events missed while disconnected are not replayed.
- The client must never send anything (only pongs).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from clipvault.logging_setup import ctx

URL = "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=30"
HELIX = "https://api.twitch.tv/helix"
WELCOME_TIMEOUT_S = 15.0

Connect = Callable[..., Any]
log = logging.getLogger(__name__)


class EventSubError(Exception):
    """The connection or a subscription failed; reconnect and try again."""


class SubscriptionRefused(Exception):
    """Twitch refused a subscription. ``login_problem``: the user token is the cause."""

    def __init__(self, message: str, login_problem: bool = False) -> None:
        super().__init__(message)
        self.login_problem = login_problem


@dataclass(frozen=True)
class ChatMessage:
    broadcaster_id: str
    text: str
    when: float  # UTC seconds


def _default_connect(url: str, **kwargs: Any) -> Any:
    from websockets.sync.client import connect

    return connect(url, open_timeout=15, close_timeout=5, max_size=2**20, **kwargs)


def parse_timestamp(text: str | None) -> float | None:
    """Twitch's RFC 3339 timestamps carry nanoseconds; Python takes microseconds."""
    match = re.fullmatch(r"(.+?)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", text or "")
    if not match:
        return None
    head, fraction, zone = match.groups()
    zone = "+00:00" if zone == "Z" else zone
    try:
        return datetime.fromisoformat(f"{head}.{(fraction or '0')[:6]}{zone}").timestamp()
    except ValueError:
        return None


class Session:
    """One EventSub WebSocket session, following reconnects."""

    def __init__(self, connect: Connect = _default_connect, url: str = URL) -> None:
        self._connect = connect
        self.url = url
        self.conn: Any = None
        self.session_id: str | None = None

    def open(self) -> str:
        """Connect and wait for the welcome; returns the session id."""
        self.conn, self.session_id = self._open(self.url)
        return self.session_id

    def _open(self, url: str) -> tuple[Any, str]:
        try:
            conn = self._connect(url)
            welcome = json.loads(conn.recv(timeout=WELCOME_TIMEOUT_S))
        except Exception as exc:
            raise EventSubError(f"could not open the chat connection: {exc}") from exc
        if welcome.get("metadata", {}).get("message_type") != "session_welcome":
            conn.close()
            raise EventSubError("Twitch did not send a welcome message")
        return conn, welcome["payload"]["session"]["id"]

    def receive(self, timeout: float = 1.0) -> list[ChatMessage]:
        """Chat messages that arrived within ``timeout`` seconds (empty on silence)."""
        if self.conn is None:
            raise EventSubError("not connected")
        try:
            raw = self.conn.recv(timeout=timeout)
        except TimeoutError:
            return []
        except Exception as exc:
            raise EventSubError(f"chat connection lost: {exc}") from exc
        message = json.loads(raw)
        kind = message.get("metadata", {}).get("message_type")
        if kind == "notification":
            return _chat(message)
        if kind == "session_reconnect":
            new_url = message["payload"]["session"]["reconnect_url"]
            old = self.conn
            self.conn, self.session_id = self._open(new_url)
            old.close()
            log.info("chat connection moved", extra=ctx(job="live"))
        elif kind == "revocation":
            subscription = message.get("payload", {}).get("subscription", {})
            log.warning("chat subscription revoked: %s", subscription.get("status"),
                        extra=ctx(job="live"))  # fmt: skip
        return []

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None
                self.session_id = None


def _chat(message: dict) -> list[ChatMessage]:
    payload = message.get("payload", {})
    if payload.get("subscription", {}).get("type") != "channel.chat.message":
        return []
    event = payload.get("event", {})
    text = (event.get("message") or {}).get("text")
    broadcaster = event.get("broadcaster_user_id")
    when = parse_timestamp(message.get("metadata", {}).get("message_timestamp"))
    if not isinstance(text, str) or not broadcaster or when is None:
        return []
    return [ChatMessage(broadcaster, text, when)]


def subscribe_chat(
    http: httpx.Client,
    client_id: str,
    token: str,
    session_id: str,
    broadcaster_id: str,
    user_id: str,
) -> str:
    """Subscribe the session to one channel's chat; returns the subscription id."""
    body = {
        "type": "channel.chat.message",
        "version": "1",
        "condition": {"broadcaster_user_id": broadcaster_id, "user_id": user_id},
        "transport": {"method": "websocket", "session_id": session_id},
    }
    response = http.post(f"{HELIX}/eventsub/subscriptions", json=body,
                         headers=_headers(client_id, token))  # fmt: skip
    if response.status_code == 202:
        return response.json()["data"][0]["id"]
    text = response.text[:300]
    if response.status_code == 401:
        raise SubscriptionRefused(f"Twitch rejected the login: {text}", login_problem=True)
    if response.status_code == 403:
        raise SubscriptionRefused(f"not allowed to read this chat: {text}",
                                  login_problem="scope" in text.lower())  # fmt: skip
    if response.status_code == 409:
        raise SubscriptionRefused("already subscribed")
    raise EventSubError(f"subscription failed ({response.status_code}): {text}")


def unsubscribe(http: httpx.Client, client_id: str, token: str, subscription_id: str) -> None:
    """Remove a subscription (best effort: a closed session drops them anyway)."""
    try:
        http.delete(f"{HELIX}/eventsub/subscriptions", params={"id": subscription_id},
                    headers=_headers(client_id, token))  # fmt: skip
    except httpx.HTTPError:
        pass


def _headers(client_id: str, token: str) -> dict[str, str]:
    return {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
