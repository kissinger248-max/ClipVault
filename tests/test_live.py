from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from clipvault import alerts, db
from clipvault import config as cfg
from clipvault.discover import live_spikes
from clipvault.live import LiveWatcher
from clipvault.timeutil import to_rfc3339, utc_now
from clipvault.twitch import eventsub, user_auth
from tests.helpers import add_streamer

T0 = datetime(2026, 11, 13, 20, 0, tzinfo=UTC).timestamp()


def chatter(detector, start: float, seconds: int, per_window: int, text: str = "hi"):
    """Feed steady chat; returns spikes reported along the way."""
    found = []
    for window in range(seconds // 10):
        for i in range(per_window):
            found += detector.add(start + window * 10 + i * (10 / per_window), text)
    return found


# --- the rolling detector ---------------------------------------------------------------


def test_steady_live_chat_has_no_spikes() -> None:
    detector = live_spikes.RollingDetector()
    assert chatter(detector, T0, 900, 5) == [] and detector.flush() == []


def test_a_burst_is_reported_once_after_it_ends() -> None:
    detector = live_spikes.RollingDetector()
    found = chatter(detector, T0, 600, 4)
    found += chatter(detector, T0 + 600, 20, 30, "KEKW")  # 20 s of laughing
    assert found == []  # still going: not reported yet
    found += chatter(detector, T0 + 620, 120, 4)
    assert len(found) == 1
    spike = found[0]
    assert T0 + 600 <= spike.peak <= T0 + 620 and spike.surge > 5
    cut = live_spikes.cut_for(spike, recording_started=T0 - 100)
    assert cut.end - cut.start == 40 and cut.start == int(spike.peak - T0 + 100 - 30)


def test_silence_still_closes_windows_and_flush_reports_an_open_run() -> None:
    detector = live_spikes.RollingDetector()
    chatter(detector, T0, 600, 4)
    chatter(detector, T0 + 600, 10, 40, "Pog")
    assert detector.advance(T0 + 615) == []  # one quiet window is allowed inside a burst
    assert len(detector.advance(T0 + 700)) == 1  # nobody typed, the run still ends
    chatter(detector, T0 + 700, 10, 40, "Pog")
    detector.advance(T0 + 711)
    assert len(detector.flush()) == 1 and detector.flush() == []


# --- EventSub -------------------------------------------------------------------------


class FakeSocket:
    def __init__(self, messages: list) -> None:
        self.messages = list(messages)
        self.closed = False

    def recv(self, timeout=None):
        if not self.messages:
            raise TimeoutError
        item = self.messages.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    def close(self):
        self.closed = True


def welcome(session_id: str = "s1") -> dict:
    return {"metadata": {"message_type": "session_welcome"},
            "payload": {"session": {"id": session_id}}}  # fmt: skip


def chat(broadcaster: str, text: str, when: str = "2026-11-13T20:00:05.123456789Z") -> dict:
    return {
        "metadata": {"message_type": "notification", "message_timestamp": when},
        "payload": {
            "subscription": {"type": "channel.chat.message"},
            "event": {"broadcaster_user_id": broadcaster, "message": {"text": text}},
        },
    }


def test_session_reads_chat_and_follows_reconnects() -> None:
    sockets = {
        "wss://first": FakeSocket([welcome("s1"), chat("100", "KEKW"),
                                   {"metadata": {"message_type": "session_keepalive"}},
                                   {"metadata": {"message_type": "session_reconnect"},
                                    "payload": {"session": {"reconnect_url": "wss://second"}}}]),
        "wss://second": FakeSocket([welcome("s2"), chat("100", "after move")]),
    }  # fmt: skip
    session = eventsub.Session(connect=lambda url: sockets[url], url="wss://first")
    assert session.open() == "s1"
    first = session.receive()
    assert first == [eventsub.ChatMessage("100", "KEKW", T0 + 5.123456)]
    assert session.receive() == []  # keepalive
    assert session.receive() == [] and session.session_id == "s2"  # moved
    assert sockets["wss://first"].closed
    assert session.receive()[0].text == "after move"
    assert session.receive() == []  # silence


def test_session_errors() -> None:
    bad = eventsub.Session(connect=lambda url: FakeSocket([{"metadata": {"message_type": "x"}}]))
    with pytest.raises(eventsub.EventSubError):
        bad.open()
    lost = eventsub.Session(connect=lambda url: FakeSocket([welcome(), ConnectionError("gone")]))
    lost.open()
    with pytest.raises(eventsub.EventSubError):
        lost.receive()


def test_subscriptions() -> None:
    sent: list[httpx.Request] = []
    status = {"code": 202}

    def handle(request):
        sent.append(request)
        return httpx.Response(status["code"], json={"data": [{"id": "sub-1"}], "message": "x"})

    http = httpx.Client(transport=httpx.MockTransport(handle))
    assert eventsub.subscribe_chat(http, "cid", "tok", "s1", "100", "42") == "sub-1"
    body = json.loads(sent[0].content)
    assert body["type"] == "channel.chat.message" and body["version"] == "1"
    assert body["condition"] == {"broadcaster_user_id": "100", "user_id": "42"}
    assert body["transport"] == {"method": "websocket", "session_id": "s1"}
    assert sent[0].headers["Authorization"] == "Bearer tok"
    status["code"] = 401
    with pytest.raises(eventsub.SubscriptionRefused) as caught:
        eventsub.subscribe_chat(http, "cid", "tok", "s1", "100", "42")
    assert caught.value.login_problem


# --- login -------------------------------------------------------------------------------


def twitch_id_server(token_answers: list[httpx.Response]) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/device":
            return httpx.Response(200, json={"device_code": "dev", "user_code": "ABCD-1234",
                                             "verification_uri": "https://www.twitch.tv/activate",
                                             "interval": 5, "expires_in": 1800})  # fmt: skip
        if request.url.path == "/oauth2/validate":
            return httpx.Response(200, json={"login": "viewer", "user_id": "42"})
        return token_answers.pop(0)

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_device_login_waits_for_approval_then_stores_the_token() -> None:
    cfg.set_secret("twitch_client_id", "cid")
    pending = httpx.Response(400, json={"status": 400, "message": "authorization_pending"})
    granted = httpx.Response(200, json={"access_token": "a1", "refresh_token": "r1",
                                        "expires_in": 14400})  # fmt: skip
    http = twitch_id_server([pending, pending, granted])
    login = user_auth.start_login(http)
    assert login.user_code == "ABCD-1234"
    waits: list[float] = []
    token = user_auth.finish_login(http, login, sleep=waits.append)
    assert (token.login, token.user_id, token.access_token) == ("viewer", "42", "a1")
    assert waits == [5.0, 5.0] and user_auth.stored() == token


def test_expiring_tokens_are_refreshed_and_refused_ones_need_a_new_login() -> None:
    cfg.set_secret("twitch_client_id", "cid")
    soon = to_rfc3339(utc_now() + timedelta(minutes=2))
    user_auth.store(user_auth.UserToken("a1", "r1", soon, "42", "viewer"))
    renewed = httpx.Response(200, json={"access_token": "a2", "refresh_token": "r2",
                                        "expires_in": 14400})  # fmt: skip
    provider = user_auth.UserTokenProvider(twitch_id_server([renewed]))
    assert provider.get() == "a2" and user_auth.stored().refresh_token == "r2"
    provider.invalidate()
    refused = user_auth.UserTokenProvider(twitch_id_server([httpx.Response(400, json={})]))
    refused.invalidate()
    with pytest.raises(user_auth.LoginNeeded):
        refused.get()
    assert user_auth.stored() is None


# --- the watcher ---------------------------------------------------------------------------


class FakeHelix:
    def __init__(self) -> None:
        self.live: dict[str, str] = {}
        self.archives: list[dict] = []

    def __call__(self):
        return self

    def get_live_stream_ids(self, user_ids):
        return {u: s for u, s in self.live.items() if u in set(user_ids)}

    def get_archives(self, user_id, first=20):
        return [a for a in self.archives if a["user_id"] == user_id]

    def close(self):
        pass


class FakeSession:
    def __init__(self) -> None:
        self.session_id = None
        self.queue: list[eventsub.ChatMessage] = []

    def open(self):
        self.session_id = "s1"
        return "s1"

    def receive(self, timeout=1.0):
        out, self.queue = self.queue, []
        return out

    def close(self):
        self.session_id = None


@pytest.fixture
def watcher(home, conn):
    cfg.set_secret("twitch_client_id", "cid")
    far = to_rfc3339(utc_now() + timedelta(hours=3))
    user_auth.store(user_auth.UserToken("a1", "r1", far, "42", "viewer"))
    add_streamer(conn, "alpha", twitch_user_id="100")
    helix = FakeHelix()
    helix.live = {"100": "stream-1"}
    helix.archives = [
        {
            "id": "v9",
            "user_id": "100",
            "stream_id": "stream-1",
            "title": "Live!",
            "url": "https://www.twitch.tv/videos/v9",
            "created_at": to_rfc3339(datetime.fromtimestamp(T0 - 3600, UTC)),
        }
    ]
    subscribed: list[dict] = []

    def handle(request):
        if request.method == "POST":
            subscribed.append(json.loads(request.content))
        return httpx.Response(202, json={"data": [{"id": f"sub-{len(subscribed)}"}]})

    clock = {"now": T0}
    w = LiveWatcher(cfg.Config(), db_path=home / "clipvault.db", app_client=helix,
                    http=httpx.Client(transport=httpx.MockTransport(handle)),
                    session=FakeSession(), clock=lambda: clock["now"])  # fmt: skip
    w.helix, w.subscribed, w.clock_state = helix, subscribed, clock
    return w


def feed(w, seconds: int, per_window: int, text: str = "hi") -> None:
    start = w.clock_state["now"]
    for window in range(seconds // 10):
        w.session.queue = [
            eventsub.ChatMessage("100", text, start + window * 10 + i * (10 / per_window))
            for i in range(per_window)
        ]
        w.clock_state["now"] = start + (window + 1) * 10
        w.step(receive_timeout=0)


def test_a_live_spike_becomes_a_clip_cut_from_the_recording(watcher, conn) -> None:
    watcher.step(receive_timeout=0)
    assert watcher.subscribed[0]["condition"] == {"broadcaster_user_id": "100", "user_id": "42"}
    feed(watcher, 600, 4)
    feed(watcher, 20, 30, "LUL")
    feed(watcher, 60, 4)
    row = conn.execute("SELECT * FROM clips").fetchone()
    assert row["source"] == "live_spike" and row["state"] == "found"
    assert row["id"] == f"live:v9:{row['vod_offset']}" and row["duration"] == 40
    assert 3600 + 600 - 30 <= row["vod_offset"] <= 3600 + 620 - 30
    assert row["url"] == "https://www.twitch.tv/videos/v9" and row["title"].endswith("- Live!")
    waits_until = datetime.fromisoformat(row["retry_at"].replace("Z", "+00:00")).timestamp()
    assert waits_until - T0 >= 600 + 180  # cut 3 minutes after the moment
    # The same moment again (e.g. found once more after a reconnect) is not saved twice.
    from clipvault.discover.live_spikes import LiveSpike

    channel = watcher.channels["100"]
    assert not watcher.save_moment(channel, LiveSpike(T0 + 3600 * 0 + 610, 99, 5))
    assert conn.execute("SELECT count(*) FROM clips").fetchone()[0] == 1


def test_the_stream_ending_stops_reading_its_chat(watcher) -> None:
    watcher.step(receive_timeout=0)
    assert "100" in watcher.channels
    watcher.helix.live = {}
    watcher.clock_state["now"] += 200
    watcher.step(receive_timeout=0)
    assert watcher.channels == {}


def test_no_recording_means_the_spike_is_only_logged(watcher, conn) -> None:
    watcher.helix.archives = []
    watcher.step(receive_timeout=0)
    feed(watcher, 600, 4)
    feed(watcher, 20, 30, "LUL")
    feed(watcher, 60, 4)
    assert watcher.channels["100"].spikes == 1
    assert conn.execute("SELECT count(*) FROM clips").fetchone()[0] == 0


def test_pause_stops_watching(watcher, conn) -> None:
    alerts.set_setting(conn, "paused", "1")
    watcher.step(receive_timeout=0)
    assert watcher.channels == {}


def test_without_a_login_the_watcher_waits_quietly(home, conn) -> None:
    w = LiveWatcher(cfg.Config(), db_path=home / "clipvault.db", app_client=FakeHelix(),
                    session=FakeSession())  # fmt: skip
    stop = threading.Event()
    thread = threading.Thread(target=w.run, args=(stop,))
    thread.start()
    stop.set()
    thread.join(5)
    assert not thread.is_alive() and alerts.open_alerts(conn) == []


def test_a_refused_login_raises_a_relogin_alert(watcher, conn) -> None:
    def refuse(request):
        return httpx.Response(401, json={"message": "invalid token"})

    watcher.http = httpx.Client(transport=httpx.MockTransport(refuse))
    watcher.tokens = user_auth.UserTokenProvider(watcher.http)
    stop = threading.Event()
    thread = threading.Thread(target=watcher.run, args=(stop,))
    thread.start()
    for _ in range(100):
        if alerts.open_alerts(conn):
            break
        threading.Event().wait(0.05)
    stop.set()
    thread.join(5)
    assert [a["kind"] for a in alerts.open_alerts(conn)] == ["relogin"]
    db.open_db(conn_path := watcher.db_path).close()
    assert conn_path.exists()


def test_the_connection_is_closed_when_nobody_is_live(watcher) -> None:
    watcher.step(receive_timeout=0)
    assert watcher.session.session_id == "s1"
    watcher.helix.live = {}
    watcher.clock_state["now"] += 200
    watcher.step(receive_timeout=0)
    assert watcher.session.session_id is None
    watcher.helix.live = {"100": "stream-2"}
    watcher.clock_state["now"] += 200
    watcher.step(receive_timeout=0)
    assert watcher.session.session_id == "s1" and len(watcher.subscribed) == 2


# --- which spikes become live moments, and what the panel sees ----------------------------


def test_a_modest_spike_is_shown_but_not_saved(watcher, conn) -> None:
    from clipvault import activity

    watcher.step(receive_timeout=0)
    feed(watcher, 600, 10)  # steady chat: 10 messages per 10 s
    feed(watcher, 20, 30)  # three times busier: a spike, but under the live bar
    feed(watcher, 60, 10)
    assert watcher.channels["100"].spikes == 1
    assert conn.execute("SELECT count(*) FROM clips").fetchone()[0] == 0
    events = activity.snapshot()["events"]
    assert [e["kind"] for e in events] == ["spike"] and events[0]["surge"] == 3.0


def test_a_saved_moment_keeps_its_surge_and_is_cut_on_time(watcher, conn) -> None:
    from clipvault import activity

    due: list[datetime] = []
    watcher.on_moment = due.append
    watcher.step(receive_timeout=0)
    feed(watcher, 600, 4)
    feed(watcher, 20, 30, "LUL")
    feed(watcher, 60, 4)
    row = conn.execute("SELECT * FROM clips").fetchone()
    assert row["surge"] > 4
    assert len(due) == 1 and to_rfc3339(due[0]) == row["retry_at"]
    moment = [e for e in activity.snapshot()["events"] if e["kind"] == "moment"][0]
    assert moment["clip_id"] == row["id"] and moment["cut_at"] == due[0].timestamp()
    channel = activity.snapshot()["live"]["channels"][0]
    assert channel["login"] == "alpha" and channel["recording"] and channel["moments"] == 1
    assert len(channel["windows"]) <= activity.METER_WINDOWS and channel["baseline"] > 0


def test_at_most_ten_live_moments_per_stream(watcher, conn) -> None:
    from clipvault.discover.live_spikes import LiveSpike
    from clipvault.live import LIVE_MAX_PER_STREAM

    watcher.step(receive_timeout=0)
    channel = watcher.channels["100"]
    saved = [watcher.save_moment(channel, LiveSpike(T0 + 120 * i, 99, 5)) for i in range(12)]
    assert saved == [True] * LIVE_MAX_PER_STREAM + [False, False]


def test_live_moments_can_be_switched_off(watcher, conn) -> None:
    from clipvault import activity
    from clipvault.live import LIVE_SETTING

    watcher.step(receive_timeout=0)
    assert "100" in watcher.channels
    alerts.set_setting(conn, LIVE_SETTING, "0")
    watcher.clock_state["now"] += 200
    watcher.step(receive_timeout=0)
    assert watcher.channels == {} and activity.snapshot()["live"]["status"] == "off"


def test_the_chat_connection_checks_twitch_with_the_shipped_certificates(monkeypatch) -> None:
    """The Mac app's Python has no system certificate list: without certifi's, live chat
    failed with "unable to get local issuer certificate" (0.3.4)."""
    import ssl

    import websockets.sync.client

    used: dict = {}

    def connect(url, **kwargs):
        used.update(kwargs)
        return object()

    monkeypatch.setattr(websockets.sync.client, "connect", connect)
    eventsub._default_connect(eventsub.URL)
    assert isinstance(used["ssl"], ssl.SSLContext)
    assert used["ssl"].verify_mode == ssl.CERT_REQUIRED and used["ssl"].check_hostname
    assert used["ssl"].cert_store_stats()["x509_ca"] > 50
