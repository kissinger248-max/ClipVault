"""Phone notifications through ntfy (SPEC §14).

Notifications go into the ``notifications`` table first and are sent by
:func:`flush`, which the scheduler calls every minute. That way nothing is lost
while the computer is offline, and quiet hours only delay messages. (A crash in the
middle of sending can, at worst, send one message twice.)

Messages are published as JSON (``POST <server>`` with a ``topic`` field), so
titles with accents or emoji need no header encoding. The topic name is random
and kept in the OS keychain: on ntfy.sh anyone who knows a topic can read it.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from clipvault import config as cfg
from clipvault.logging_setup import ctx
from clipvault.timeutil import parse_rfc3339, to_rfc3339, utc_now

TOPIC_SECRET = "ntfy_topic"
TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
KINDS = ("summary", "approve", "relogin", "storage", "broken", "campaign", "downloader", "library",
         "test")  # fmt: skip
# These ignore quiet hours: they need an answer (approve), are about to cost data
# (storage), or the user is waiting for them (test).
URGENT_KINDS = ("approve", "storage", "test")
MAX_MESSAGE_BYTES = 3500  # ntfy.sh turns anything over 4,096 bytes into an attachment
GIVE_UP_AFTER = timedelta(hours=48)
SEND_BATCH = 10
JOB = "notify"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Action:
    """A button on the notification: ``view`` opens a page, ``http`` calls one."""

    action: str
    label: str
    url: str
    method: str | None = None

    def to_json(self) -> dict[str, str]:
        data = {"action": self.action, "label": self.label, "url": self.url}
        if self.method:
            data["method"] = self.method
        return data


def get_topic() -> str | None:
    """The stored topic, or None when notifications were never set up."""
    topic = cfg.get_secret(TOPIC_SECRET)
    return topic if topic and TOPIC_RE.match(topic) else None


def ensure_topic() -> str:
    """Return the topic, creating a random one on first use."""
    topic = get_topic()
    if topic is None:
        topic = "clipvault-" + secrets.token_urlsafe(24)
        cfg.set_secret(TOPIC_SECRET, topic)
    return topic


def subscribe_url(config: cfg.Config, topic: str) -> str:
    """Web address to open (or paste into the ntfy app) to receive the notifications."""
    return f"{config.notifications.server}/{topic}"


# --- quiet hours ---------------------------------------------------------------


def _clock(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def quiet_until(now: datetime, config: cfg.Config) -> datetime | None:
    """When the current quiet hours end (UTC), or None when it isn't quiet now."""
    start, end = (_clock(t) for t in config.notifications.quiet_hours)
    if start == end:
        return None
    tz = ZoneInfo(config.timezone)
    local = now.astimezone(tz)
    clock = local.time().replace(tzinfo=None)
    overnight = start > end
    quiet = (clock >= start or clock < end) if overnight else (start <= clock < end)
    if not quiet:
        return None
    day = local.date() + timedelta(days=1) if overnight and clock >= start else local.date()
    return datetime.combine(day, end, tzinfo=tz).astimezone(now.tzinfo)


# --- the outbox ------------------------------------------------------------------


def queue(
    conn: sqlite3.Connection,
    config: cfg.Config,
    kind: str,
    title: str,
    message: str,
    *,
    priority: int = 3,
    tags: tuple[str, ...] = (),
    click: str | None = None,
    actions: tuple[Action, ...] = (),
    now: datetime | None = None,
) -> int | None:
    """Add a notification to the outbox; returns its id, or None when ntfy isn't set up.

    During quiet hours it waits until they end, except for urgent kinds.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown notification kind: {kind!r}")
    if get_topic() is None:
        return None
    now = now or utc_now()
    send_after = now
    if kind not in URGENT_KINDS:
        send_after = quiet_until(now, config) or now
    body = message.encode("utf-8")[:MAX_MESSAGE_BYTES].decode("utf-8", errors="ignore")
    cursor = conn.execute(
        "INSERT INTO notifications (kind, title, message, priority, tags_json, click, "
        "actions_json, created_at, send_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            kind,
            title[:200],
            body,
            min(max(priority, 1), 5),
            json.dumps(list(tags)),
            click,
            json.dumps([a.to_json() for a in actions[:3]]),
            to_rfc3339(now),
            to_rfc3339(send_after),
        ),
    )
    return cursor.lastrowid


def payload(row: sqlite3.Row, topic: str) -> dict:
    """The JSON body ntfy expects for one outbox row."""
    data: dict = {
        "topic": topic,
        "title": row["title"],
        "message": row["message"],
        "priority": row["priority"],
    }
    tags = json.loads(row["tags_json"])
    actions = json.loads(row["actions_json"])
    if tags:
        data["tags"] = tags
    if actions:
        data["actions"] = actions
    if row["click"]:
        data["click"] = row["click"]
    return data


@dataclass
class FlushResult:
    sent: int = 0
    failed: int = 0
    dropped: int = 0


def flush(
    conn: sqlite3.Connection,
    config: cfg.Config,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> FlushResult:
    """Send every notification that is due. Failures stay queued for the next call."""
    result = FlushResult()
    topic = get_topic()
    if topic is None:
        return result
    now = now or utc_now()
    rows = conn.execute(
        "SELECT * FROM notifications WHERE status = 'queued' AND send_after <= ? "
        "ORDER BY send_after, id LIMIT ?",
        (to_rfc3339(now), SEND_BATCH),
    ).fetchall()
    if not rows:
        return result
    own_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        for row in rows:
            if now - parse_rfc3339(row["created_at"]) > GIVE_UP_AFTER:
                conn.execute(
                    "UPDATE notifications SET status = 'dropped', last_error = "
                    "COALESCE(last_error, 'too old') WHERE id = ?",
                    (row["id"],),
                )
                result.dropped += 1
                continue
            error = _send(client, config.notifications.server, payload(row, topic))
            if error is None:
                conn.execute(
                    "UPDATE notifications SET status = 'sent', sent_at = ?, "
                    "attempts = attempts + 1 WHERE id = ?",
                    (to_rfc3339(now), row["id"]),
                )
                result.sent += 1
            else:
                conn.execute(
                    "UPDATE notifications SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                    (error, row["id"]),
                )
                result.failed += 1
                log.warning(
                    "notification not sent: %s", error, extra=ctx(job=JOB, kind=row["kind"])
                )
                break  # offline or rate-limited: try the rest next time
    finally:
        if own_client:
            client.close()
    return result


def _send(client: httpx.Client, server: str, data: dict) -> str | None:
    """POST one message; returns None on success or a short error text."""
    try:
        response = client.post(server + "/", json=data)
    except httpx.HTTPError as exc:
        return f"could not reach {server}: {exc.__class__.__name__}"
    if response.status_code == 429:
        return "ntfy rate limit reached"
    if response.status_code >= 400:
        return f"ntfy answered {response.status_code}: {response.text[:200]}"
    return None


# --- alerts become notifications ------------------------------------------------------

ALERT_TITLES = {
    "relogin": "ClipVault: please reconnect an account",
    "storage": "ClipVault: disk almost full",
    "broken": "ClipVault: something keeps failing",
    "campaign": "ClipVault: new campaign",
    "downloader": "ClipVault: downloads are failing",
    "library": "ClipVault: library folder problem",
}


def queue_new_alerts(
    conn: sqlite3.Connection, config: cfg.Config, now: datetime | None = None
) -> int:
    """Queue one notification for each open alert not yet sent to the phone."""
    if get_topic() is None:
        return 0
    rows = conn.execute(
        "SELECT * FROM alerts WHERE resolved_at IS NULL AND notified_at IS NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        title = ALERT_TITLES.get(row["kind"], "ClipVault needs you")
        queue(conn, config, row["kind"], title, row["message"], priority=4, tags=("warning",),
              now=now)  # fmt: skip
        conn.execute(
            "UPDATE alerts SET notified_at = ? WHERE id = ?",
            (to_rfc3339(now or utc_now()), row["id"]),
        )
    return len(rows)
