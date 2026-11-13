from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from clipvault import config as cfg
from clipvault.alerts import raise_alert
from clipvault.notify import ntfy

CONFIG = cfg.Config()  # America/Chicago, quiet 23:00-08:00
NOON = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)  # 12:00 in Chicago
LATE = datetime(2026, 11, 13, 5, 30, tzinfo=UTC)  # 23:30 in Chicago


class Recorder:
    """An ntfy server stand-in (httpx MockTransport: no network)."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.bodies: list[dict] = []

    def client(self) -> httpx.Client:
        def handle(request: httpx.Request) -> httpx.Response:
            self.bodies.append(json.loads(request.content))
            return httpx.Response(self.status, json={"id": "x"})

        return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture
def topic() -> str:
    return ntfy.ensure_topic()


def statuses(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT status FROM notifications ORDER BY id")]


def test_topic_is_random_private_and_stable() -> None:
    assert ntfy.get_topic() is None
    first = ntfy.ensure_topic()
    assert ntfy.TOPIC_RE.match(first) and len(first) > 30
    assert ntfy.ensure_topic() == first == cfg.get_secret("ntfy_topic")


def test_nothing_is_queued_before_setup(conn) -> None:
    assert ntfy.queue(conn, CONFIG, "summary", "t", "m", now=NOON) is None
    assert statuses(conn) == []


def test_quiet_hours_overnight() -> None:
    assert ntfy.quiet_until(NOON, CONFIG) is None
    end = ntfy.quiet_until(LATE, CONFIG)
    assert end == datetime(2026, 11, 13, 14, 0, tzinfo=UTC)  # 08:00 Chicago next morning
    early = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)  # 06:00 Chicago
    assert ntfy.quiet_until(early, CONFIG) == end
    off = cfg.Config(notifications=cfg.NotificationsConfig(quiet_hours=("08:00", "08:00")))
    assert ntfy.quiet_until(LATE, off) is None


def test_quiet_hours_delay_all_but_urgent_kinds(conn, topic) -> None:
    ntfy.queue(conn, CONFIG, "summary", "s", "m", now=LATE)
    ntfy.queue(conn, CONFIG, "storage", "d", "m", now=LATE)
    recorder = Recorder()
    result = ntfy.flush(conn, CONFIG, client=recorder.client(), now=LATE)
    assert result.sent == 1 and [b["title"] for b in recorder.bodies] == ["d"]
    morning = datetime(2026, 11, 13, 14, 0, tzinfo=UTC)
    ntfy.flush(conn, CONFIG, client=recorder.client(), now=morning)
    assert [b["title"] for b in recorder.bodies] == ["d", "s"]
    assert statuses(conn) == ["sent", "sent"]


def test_payload_is_json_with_topic_tags_and_actions(conn, topic) -> None:
    ntfy.queue(conn, CONFIG, "approve", "Café ✓", "ok", priority=9, tags=("tv",),
               click="http://x", actions=(ntfy.Action("view", "Open", "http://y"),),
               now=NOON)  # fmt: skip
    recorder = Recorder()
    ntfy.flush(conn, CONFIG, client=recorder.client(), now=NOON)
    body = recorder.bodies[0]
    assert body["topic"] == topic and body["title"] == "Café ✓" and body["priority"] == 5
    assert body["tags"] == ["tv"] and body["click"] == "http://x"
    assert body["actions"] == [{"action": "view", "label": "Open", "url": "http://y"}]


def test_failed_sends_stay_queued_and_old_ones_are_dropped(conn, topic) -> None:
    ntfy.queue(conn, CONFIG, "summary", "a", "m", now=NOON)
    ntfy.queue(conn, CONFIG, "summary", "b", "m", now=NOON)
    failing = Recorder(status=429)
    result = ntfy.flush(conn, CONFIG, client=failing.client(), now=NOON)
    assert (result.sent, result.failed) == (0, 1) and len(failing.bodies) == 1  # stops at once
    assert statuses(conn) == ["queued", "queued"]
    later = NOON + timedelta(minutes=5)
    assert ntfy.flush(conn, CONFIG, client=Recorder().client(), now=later).sent == 2
    ntfy.queue(conn, CONFIG, "summary", "c", "m", now=NOON)
    much_later = NOON + timedelta(hours=49)
    assert ntfy.flush(conn, CONFIG, client=Recorder().client(), now=much_later).dropped == 1


def test_open_alerts_are_sent_once(conn, topic) -> None:
    raise_alert(conn, "storage", "Disk almost full")
    assert ntfy.queue_new_alerts(conn, CONFIG, now=NOON) == 1
    assert ntfy.queue_new_alerts(conn, CONFIG, now=NOON) == 0
    recorder = Recorder()
    ntfy.flush(conn, CONFIG, client=recorder.client(), now=NOON)
    assert recorder.bodies[0]["message"] == "Disk almost full"


def test_long_messages_are_cut_to_fit(conn, topic) -> None:
    ntfy.queue(conn, CONFIG, "summary", "t", "é" * 5000, now=NOON)
    stored = conn.execute("SELECT message FROM notifications").fetchone()[0]
    assert len(stored.encode()) <= ntfy.MAX_MESSAGE_BYTES
