"""Moments from chat while streamers are live (M14, SPEC §5.3).

The watcher runs on its own thread inside ``run``:

1. Every 2 minutes it asks Helix which saved streamers are live, and finds each
   live stream's in-progress recording (Get Videos lists it as an archive while
   the stream runs; checked live 2026-11-13).
2. It keeps one EventSub chat connection and subscribes to each live channel.
3. Every chat message goes into that channel's rolling detector (the M4 rules).
4. A spike becomes a clip ``live:<vod_id>:<offset>`` (source ``live_spike``,
   state ``found``) that the download step cuts from the recording, waiting
   3 minutes first so the recording has caught up past the moment.

Archive only (hard rule 3): cuts use yt-dlp. Streamers who switch recordings off
yield no footage; their spikes are only logged. Clipping through Twitch's own
Create Clip From VOD is for permissioned streamers and isn't used.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from clipvault import alerts, db
from clipvault import config as cfg
from clipvault.discover import live_spikes, vod_spikes
from clipvault.logging_setup import ctx
from clipvault.timeutil import parse_rfc3339, to_rfc3339
from clipvault.twitch import eventsub, user_auth

JOB = "live"
CHECK_EVERY_S = 120
CUT_DELAY = timedelta(minutes=3)  # let the recording catch up past the moment
RETRY_LOGIN_S = 300
LOGIN_POLL_S = 60
RECONNECT_BACKOFF_S = (5, 15, 60, 300)
RELOGIN_ALERT = "Log in to Twitch again so ClipVault can read live chat (Settings, Twitch login)."

log = logging.getLogger(__name__)


@dataclass
class Channel:
    """A streamer who is live right now."""

    streamer_id: int
    login: str
    user_id: str
    stream_id: str
    vod_id: str | None = None
    vod_url: str | None = None
    vod_title: str = ""
    recording_started: float | None = None
    subscription_id: str | None = None
    detector: live_spikes.RollingDetector = field(default_factory=live_spikes.RollingDetector)
    spikes: int = 0
    moments: int = 0


class LiveWatcher:
    """Watches live chat of saved streamers and records the moments it finds."""

    def __init__(
        self,
        config: cfg.Config,
        *,
        db_path: Path | None = None,
        app_client: Callable[[], Any] | None = None,
        http: httpx.Client | None = None,
        session: eventsub.Session | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.db_path = db_path or cfg.db_path()
        self._app_client = app_client
        self.http = http or httpx.Client(timeout=15.0)
        self.session = session or eventsub.Session()
        self.clock = clock
        self.channels: dict[str, Channel] = {}
        self.tokens = user_auth.UserTokenProvider(self.http)
        self._last_check = 0.0
        self._failures = 0

    # --- the loop -----------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Watch until ``stop`` is set. Never raises: problems are logged and retried."""
        log.info("live chat watcher started", extra=ctx(job=JOB))
        while not stop.is_set():
            if not self.tokens.logged_in:
                # Not logged in (yet): nothing to do and nothing to complain about.
                self.tokens = user_auth.UserTokenProvider(self.http)
                if not self.tokens.logged_in:
                    stop.wait(LOGIN_POLL_S)
                    continue
            try:
                self.step()
                self._failures = 0
            except user_auth.LoginNeeded as exc:
                self._login_problem(str(exc))
                stop.wait(RETRY_LOGIN_S)
                self.tokens = user_auth.UserTokenProvider(self.http)
            except (eventsub.EventSubError, httpx.HTTPError, OSError) as exc:
                wait = RECONNECT_BACKOFF_S[min(self._failures, len(RECONNECT_BACKOFF_S) - 1)]
                self._failures += 1
                log.warning("live chat problem, retrying in %d s: %s", wait, exc,
                            extra=ctx(job=JOB))  # fmt: skip
                self._drop_connection()
                stop.wait(wait)
            except Exception as exc:  # never take the app down
                log.exception("live chat watcher error: %s", exc, extra=ctx(job=JOB))
                self._drop_connection()
                stop.wait(60)
        for channel in list(self.channels.values()):
            self._ended(channel)
        self._drop_connection()
        log.info("live chat watcher stopped", extra=ctx(job=JOB))

    def step(self, receive_timeout: float = 1.0) -> None:
        """One round: refresh who is live (every 2 min), read chat, close quiet windows."""
        now = self.clock()
        if now - self._last_check >= CHECK_EVERY_S:
            self._last_check = now
            self.refresh_live()
        if not self.channels:
            if receive_timeout:
                time.sleep(min(receive_timeout, 1.0))
            return
        for message in self.session.receive(timeout=receive_timeout):
            channel = self.channels.get(message.broadcaster_id)
            if channel is not None:
                self._handle(channel, channel.detector.add(message.when, message.text))
        now = self.clock()
        for channel in self.channels.values():
            self._handle(channel, channel.detector.advance(now))

    # --- who is live ---------------------------------------------------------------------

    def refresh_live(self) -> None:
        conn = db.open_db(self.db_path)
        try:
            if alerts.is_paused(conn):
                for channel in list(self.channels.values()):
                    self._ended(channel)
                return
            streamers = conn.execute(
                "SELECT id, login, twitch_user_id FROM streamers WHERE save_enabled = 1 "
                "AND twitch_user_id IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
        client = self._client()
        try:
            live = (
                client.get_live_stream_ids(s["twitch_user_id"] for s in streamers)
                if streamers
                else {}
            )
            for streamer in streamers:
                user_id = streamer["twitch_user_id"]
                current = self.channels.get(user_id)
                if user_id not in live:
                    if current is not None:
                        self._ended(current)
                    continue
                if current is not None and current.stream_id != live[user_id]:
                    self._ended(current)  # a new stream started: start fresh
                    current = None
                if current is None:
                    current = Channel(streamer["id"], streamer["login"], user_id, live[user_id])
                    self.channels[user_id] = current
                    log.info(
                        "streamer is live; reading chat", extra=ctx(streamer=current.login, job=JOB)
                    )
                if current.vod_id is None:
                    self._find_recording(client, current)
        finally:
            client.close()
        self._subscribe_new()

    def _client(self) -> Any:
        if self._app_client is not None:
            return self._app_client()
        from clipvault import twitch

        return twitch.build_client()

    def _find_recording(self, client: Any, channel: Channel) -> None:
        for video in client.get_archives(channel.user_id, 5):
            if video.get("stream_id") == channel.stream_id:
                channel.vod_id = video["id"]
                channel.vod_url = video.get("url") or f"https://www.twitch.tv/videos/{video['id']}"
                channel.vod_title = video.get("title") or ""
                channel.recording_started = parse_rfc3339(video["created_at"]).timestamp()
                return

    def _subscribe_new(self) -> None:
        pending = [c for c in self.channels.values() if c.subscription_id is None]
        if not pending:
            return
        if self.session.session_id is None:
            self.session.open()
            for channel in self.channels.values():
                channel.subscription_id = None
            pending = list(self.channels.values())
        token = self.tokens.get()
        client_id = cfg.twitch_client_id() or ""
        for channel in pending:
            try:
                channel.subscription_id = eventsub.subscribe_chat(
                    self.http, client_id, token, self.session.session_id or "",
                    channel.user_id, self.tokens.user_id,
                )  # fmt: skip
            except eventsub.SubscriptionRefused as exc:
                if exc.login_problem:
                    self.tokens.invalidate()
                    raise user_auth.LoginNeeded(str(exc)) from exc
                log.warning("can't read chat: %s", exc, extra=ctx(streamer=channel.login, job=JOB))
                channel.subscription_id = "refused"
        self._login_ok()

    def _ended(self, channel: Channel) -> None:
        self._handle(channel, channel.detector.flush())
        if channel.subscription_id not in (None, "refused") and self.session.session_id:
            try:
                eventsub.unsubscribe(self.http, cfg.twitch_client_id() or "",
                                     self.tokens.get(), channel.subscription_id)  # fmt: skip
            except user_auth.LoginNeeded:
                pass
        self.channels.pop(channel.user_id, None)
        if not self.channels:
            # Twitch closes a connection without subscriptions ("connection unused");
            # close it ourselves and open a fresh one when someone goes live again.
            self._drop_connection()
        log.info("stream ended; %d spike(s), %d moment(s) saved", channel.spikes, channel.moments,
                 extra=ctx(streamer=channel.login, job=JOB))  # fmt: skip

    def _drop_connection(self) -> None:
        try:
            self.session.close()
        finally:
            for channel in self.channels.values():
                channel.subscription_id = None

    # --- moments ---------------------------------------------------------------------------

    def _handle(self, channel: Channel, spikes: list[live_spikes.LiveSpike]) -> None:
        for spike in spikes:
            channel.spikes += 1
            if channel.vod_id is None or channel.recording_started is None:
                log.info("chat spike, but the stream has no recording to cut from",
                         extra=ctx(streamer=channel.login, job=JOB, score=spike.score))  # fmt: skip
                continue
            if self.save_moment(channel, spike):
                channel.moments += 1

    def save_moment(self, channel: Channel, spike: live_spikes.LiveSpike) -> bool:
        """Record a spike as a clip to cut later; False when an existing clip covers it."""
        assert channel.vod_id is not None and channel.recording_started is not None
        cut = live_spikes.cut_for(spike, channel.recording_started)
        duration = cut.end - cut.start
        conn = db.open_db(self.db_path)
        try:
            covered = conn.execute(
                "SELECT 1 FROM clips WHERE vod_id = ? AND vod_offset IS NOT NULL "
                "AND vod_offset < ? AND vod_offset + duration > ? LIMIT 1",
                (channel.vod_id, cut.end, cut.start),
            ).fetchone()
            if covered:
                return False
            title = f"Live chat moment at {vod_spikes.clock(cut.start + vod_spikes.BEFORE_PEAK_S)}"
            if channel.vod_title:
                title += f" - {channel.vod_title}"
            peak = datetime.fromtimestamp(spike.peak, UTC)
            cursor = conn.execute(
                "INSERT INTO clips (id, streamer_id, source, title, created_at, view_count, "
                "duration, vod_id, vod_offset, url, state, score, retry_at) VALUES "
                "(?, ?, 'live_spike', ?, ?, NULL, ?, ?, ?, ?, 'found', ?, ?) "
                "ON CONFLICT(id) DO NOTHING",
                (
                    f"live:{channel.vod_id}:{cut.start}",
                    channel.streamer_id,
                    title[:300],
                    to_rfc3339(peak),
                    float(duration),
                    channel.vod_id,
                    cut.start,
                    channel.vod_url,
                    spike.score,
                    to_rfc3339(peak + CUT_DELAY),
                ),
            )
        finally:
            conn.close()
        if cursor.rowcount:
            log.info("live chat moment saved for cutting", extra=ctx(
                streamer=channel.login, job=JOB, vod=channel.vod_id, offset=cut.start,
                score=spike.score, surge=round(spike.surge, 1)))  # fmt: skip
        return bool(cursor.rowcount)

    # --- alerts ------------------------------------------------------------------------------

    def _login_problem(self, message: str) -> None:
        log.warning("live chat needs a Twitch login: %s", message, extra=ctx(job=JOB))
        conn = db.open_db(self.db_path)
        try:
            alerts.raise_alert(conn, "relogin", RELOGIN_ALERT, "/settings#twitch-login")
        finally:
            conn.close()
        self._drop_connection()

    def _login_ok(self) -> None:
        conn = db.open_db(self.db_path)
        try:
            alerts.resolve_alert(conn, "relogin", RELOGIN_ALERT)
        finally:
            conn.close()
