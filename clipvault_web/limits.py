"""Who may start a run right now (W4): blocked visitors, the CAPTCHA, the queue cap,
one run at a time, and the free runs per day.

Visitors are told apart only by a salted hash of their address (``store.salt``), never
by anything stored on their device. The CAPTCHA is Cloudflare Turnstile (free), used
only when its keys are set; docs checked 2026-11-19:
https://developers.cloudflare.com/turnstile/get-started/server-side-validation/
(``POST .../turnstile/v0/siteverify`` with ``secret``, ``response``, ``remoteip``;
answers ``success``; tokens are single use and live 5 minutes).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

import httpx
from fastapi import Request

from clipvault.timeutil import to_rfc3339, utc_now

from . import accounts
from .settings import WebSettings

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
WORKING = ("queued", "finding", "making")
TOKEN_HEADER = "x-turnstile"


@dataclass(frozen=True)
class Refusal:
    message: str
    status: int
    kind: str  # blocked | check | busy | one-at-a-time | limit


def refusal(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    visitor: str,
    *,
    http: httpx.Client | None = None,
) -> Refusal | None:
    """Why this visitor can't start a run now, or None when they can."""
    from . import store

    if store.is_blocked(conn, visitor):
        return Refusal("Making clips here isn't available to you any more.", 403, "blocked")
    if settings.turnstile_secret and not human(settings, request, http):
        return Refusal("Please complete the quick check below the button, then try again.",
                       400, "check")  # fmt: skip
    waiting = conn.execute(
        f"SELECT count(*) FROM runs WHERE status IN {WORKING} AND embed_only = 0"
    ).fetchone()[0]
    if waiting >= settings.max_queue:
        return Refusal("We're very busy right now. Please try again in a few minutes.", 503,
                       "busy")  # fmt: skip
    mine = conn.execute(
        f"SELECT count(*) FROM runs WHERE visitor = ? AND status IN {WORKING} AND embed_only = 0",
        (visitor,),
    ).fetchone()[0]
    if mine and settings.one_run_at_a_time:
        return Refusal("Your clips are still being made. Start the next ones when they're done.",
                       429, "one-at-a-time")  # fmt: skip
    session = accounts.current(conn, request.cookies.get(accounts.SESSION_COOKIE))
    allowed = settings.runs_per_day_signed_in if session else settings.runs_per_day
    since = to_rfc3339(utc_now() - timedelta(days=1))
    today = conn.execute(
        "SELECT count(*) FROM runs WHERE visitor = ? AND created_at > ?", (visitor, since)
    ).fetchone()[0]
    if today >= allowed:
        more = "" if session else " Sign in with Twitch for a few more."
        return Refusal(f"That's your {allowed} free runs for today.{more} The free app has no "
                       "limits.", 429, "limit")  # fmt: skip
    return None


def human(settings: WebSettings, request: Request, http: httpx.Client | None = None) -> bool:
    """Ask Cloudflare whether the page's Turnstile token is good (single use)."""
    token = request.headers.get(TOKEN_HEADER, "")
    if not token or len(token) > 2048:
        return False
    data = {"secret": settings.turnstile_secret, "response": token}
    if request.client:
        data["remoteip"] = request.client.host
    client = http or httpx.Client(timeout=10)
    try:
        reply = client.post(SITEVERIFY_URL, data=data)
        return bool(reply.json().get("success"))
    except (httpx.HTTPError, ValueError):
        return False  # can't tell: better to ask again than to let a flood through
    finally:
        if http is None:
            client.close()


class RateLimiter:
    """At most ``per_minute`` calls a minute per visitor (streamer search: Twitch's quota is
    shared by everyone using the website). In memory; resets on restart."""

    def __init__(self, per_minute: int = 30) -> None:
        import threading

        self.per_minute = per_minute
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, visitor: str, now: float | None = None) -> bool:
        import time

        now = time.monotonic() if now is None else now
        with self._lock:
            recent = [t for t in self._hits.get(visitor, []) if now - t < 60]
            if len(recent) >= self.per_minute:
                self._hits[visitor] = recent
                return False
            recent.append(now)
            self._hits[visitor] = recent
            if len(self._hits) > 10_000:  # forget idle visitors
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < 60}
            return True


def busy(conn: sqlite3.Connection, settings: WebSettings) -> bool:
    """Is the queue full? (The home page says so before anyone tries.)"""
    waiting = conn.execute(
        f"SELECT count(*) FROM runs WHERE status IN {WORKING} AND embed_only = 0"
    ).fetchone()[0]
    return waiting >= settings.max_queue
