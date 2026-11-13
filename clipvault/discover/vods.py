"""Moments nobody clipped, from the chat of finished streams (SPEC §5.2).

Two steps, both safe to repeat:

1. :func:`find_vods` lists each streamer's recent past broadcasts with the
   Helix API and records new ones in the ``vods`` table. A broadcast that is
   still live is left for later.
2. :func:`process_vods` downloads each new VOD's chat replay, runs the spike
   detector, and adds the best moments as clips in state ``found``
   (id ``vod:<vod_id>:<offset>``, source ``vod_spike``). The download step
   then cuts just those 40 seconds from the VOD.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from clipvault import db
from clipvault.config import Config
from clipvault.discover import vod_spikes
from clipvault.library import storage
from clipvault.logging_setup import ctx
from clipvault.timeutil import parse_rfc3339, to_rfc3339, utc_now
from clipvault.twitch import chat_replay
from clipvault.twitch.api import TwitchAPIError, TwitchClient

JOB = "vods"
LOOKBACK = timedelta(days=7)  # VODs older than this are not looked at
VODS_PER_STREAMER = 20
# Chat download failures are retried after these delays, then the VOD is skipped.
RETRY_DELAYS = (timedelta(minutes=30), timedelta(hours=2), timedelta(hours=8))

CHUNK_S = 4 * 3600  # chat of long streams is read in pieces of this length

ChatDownloader = Callable[..., Path]
log = logging.getLogger(__name__)


@dataclass
class VodSummary:
    """What one round of VOD processing did."""

    found: int = 0
    processed: int = 0
    moments: int = 0
    skipped: int = 0
    failed: int = 0
    waiting: int = 0
    errors: list[str] = field(default_factory=list)


def find_vods(
    conn: sqlite3.Connection,
    client: TwitchClient,
    *,
    login: str | None = None,
    now: datetime | None = None,
) -> int:
    """Record new finished broadcasts from the last 7 days; returns how many were new."""
    now = now or utc_now()
    query = (
        "SELECT * FROM streamers WHERE save_enabled = 1 AND vod_moments > 0 "
        "AND twitch_user_id IS NOT NULL"
    )
    params: list[str] = []
    if login:
        query += " AND login = ?"
        params.append(login)
    streamers = conn.execute(query, params).fetchall()
    if not streamers:
        return 0
    live = client.get_live_stream_ids(s["twitch_user_id"] for s in streamers)
    added = 0
    for streamer in streamers:
        user_id = streamer["twitch_user_id"]
        for video in client.get_archives(user_id, VODS_PER_STREAMER):
            created = parse_rfc3339(video["created_at"])
            if now - created > LOOKBACK:
                continue
            if video.get("stream_id") and live.get(user_id) == video["stream_id"]:
                continue  # still being streamed: its chat isn't complete yet
            try:
                length = vod_spikes.parse_duration(video.get("duration") or "")
            except ValueError:
                length = None
            cursor = conn.execute(
                "INSERT INTO vods (id, streamer_id, stream_id, title, url, created_at, duration_s) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (
                    video["id"],
                    streamer["id"],
                    video.get("stream_id"),
                    video.get("title"),
                    video.get("url") or f"https://www.twitch.tv/videos/{video['id']}",
                    to_rfc3339(created),
                    length,
                ),
            )
            added += cursor.rowcount
    return added


def chat_relpath(
    login: str, vod_id: str, created_at: datetime, tz: ZoneInfo, part: int | None = None
) -> str:
    """Library-relative path of a VOD's saved chat (or of one piece of it)."""
    day = created_at.astimezone(tz).strftime("%Y-%m-%d")
    piece = f"_part{part:02d}" if part is not None else ""
    name = f"{day}_{storage.safe_id(vod_id)}{piece}.json.gz"
    return f"{storage.streamer_folder(login)}/chat/{name}"


def chat_pieces(length_s: int | None) -> list[tuple[int | None, int | None]]:
    """(begin, end) seconds of each piece of chat to download; one piece for short VODs."""
    if not length_s or length_s <= CHUNK_S:
        return [(None, None)]
    return [(b, min(b + CHUNK_S, length_s)) for b in range(0, length_s, CHUNK_S)]


def clipped_spans(
    conn: sqlite3.Connection, client: TwitchClient | None, vod: sqlite3.Row, user_id: str
) -> list[tuple[float, float]]:
    """(offset, duration) of every clip already made from this VOD.

    Clips in the library, plus every clip Twitch lists for the broadcast's time
    span (a moment someone clipped with only a few views was still clipped).
    """
    spans = [
        (row["vod_offset"], row["duration"])
        for row in conn.execute(
            "SELECT vod_offset, duration FROM clips WHERE vod_id = ? AND vod_offset IS NOT NULL",
            (vod["id"],),
        )
    ]
    if client is not None and vod["duration_s"]:
        start = parse_rfc3339(vod["created_at"])
        end = start + timedelta(seconds=vod["duration_s"])
        for clip in client.iter_clips(user_id, start, end):
            if clip.get("video_id") == vod["id"] and clip.get("vod_offset") is not None:
                spans.append((float(clip["vod_offset"]), float(clip.get("duration") or 30)))
    return spans


def process_vods(
    conn: sqlite3.Connection,
    config: Config,
    tool: Path,
    *,
    client: TwitchClient | None = None,
    login: str | None = None,
    limit: int | None = None,
    now: datetime | None = None,
    download_chat: ChatDownloader = chat_replay.download_chat,
) -> VodSummary:
    """Find the moments in every new VOD; safe to run again at any time."""
    now = now or utc_now()
    tz = ZoneInfo(config.timezone)
    summary = VodSummary()
    library = storage.open_library(conn, config)
    if not library.available:
        return summary  # chat files live in the library; try again when it is back
    query = (
        "SELECT v.*, s.login, s.twitch_user_id, s.vod_moments FROM vods v "
        "JOIN streamers s ON s.id = v.streamer_id WHERE v.state = 'found' AND s.save_enabled = 1"
    )
    params: list[str] = []
    if login:
        query += " AND s.login = ?"
        params.append(login)
    pending = conn.execute(query + " ORDER BY v.created_at DESC", params).fetchall()
    due = [v for v in pending if not v["retry_at"] or parse_rfc3339(v["retry_at"]) <= now]
    summary.waiting = len(pending) - len(due)
    for vod in due[:limit] if limit else due:
        log_ctx = ctx(streamer=vod["login"], job=JOB, vod=vod["id"])
        created = parse_rfc3339(vod["created_at"])
        pieces = chat_pieces(vod["duration_s"])
        scores: list[float] = []
        count = 0
        try:
            for part, (begin, end) in enumerate(pieces):
                number = part if len(pieces) > 1 else None
                relpath = chat_relpath(vod["login"], vod["id"], created, tz, number)
                path = storage.resolve(library, relpath)
                if path is None:  # pieces already saved are reused after an interruption
                    path = download_chat(tool, vod["id"], library.root / relpath, begin, end)
                if part == 0:
                    conn.execute("UPDATE vods SET chat_path = ? WHERE id = ?", (relpath, vod["id"]))
                messages = chat_replay.load_chat(path, float(begin or 0))
                count += len(messages)
                scores = vod_spikes.add_scores(scores, vod_spikes.window_scores(messages))
                del messages  # only the scores are kept: a 48-hour chat is millions of lines
            covered = clipped_spans(conn, client, vod, vod["twitch_user_id"])
        except chat_replay.ChatUnavailable as exc:
            if exc.permanent:
                _skip(conn, vod["id"], f"chat not available: {exc}", now)
                summary.skipped += 1
                log.info("VOD skipped: %s", exc, extra=log_ctx)
            else:
                _record_failure(conn, vod, str(exc), now, summary)
                log.warning("chat download failed: %s", exc, extra=log_ctx)
            continue
        except (OSError, ValueError, TwitchAPIError) as exc:
            _record_failure(conn, vod, f"{exc.__class__.__name__}: {exc}", now, summary)
            log.error("VOD processing failed: %s", exc, extra=log_ctx)
            continue

        if not count:
            _skip(conn, vod["id"], "no chat messages", now)
            summary.skipped += 1
            continue
        if vod["duration_s"]:  # quiet windows at the end still count as history
            scores = vod_spikes.add_scores(scores, [0.0] * -(-vod["duration_s"] // 10))
        spikes = vod_spikes.find_spikes(scores)
        moments = vod_spikes.pick_from_scores(
            scores, length_s=vod["duration_s"], covered=covered, keep=vod["vod_moments"]
        )
        with db.transaction(conn):
            for moment in moments:
                _add_moment(conn, vod, created, moment)
            conn.execute(
                "UPDATE vods SET state = 'done', messages = ?, spikes = ?, moments = ?, "
                "processed_at = ?, last_error = NULL, retry_at = NULL WHERE id = ?",
                (count, len(spikes), len(moments), to_rfc3339(now), vod["id"]),
            )
        summary.processed += 1
        summary.moments += len(moments)
        log.info(
            "VOD chat searched",
            extra={
                **log_ctx,
                "messages": count,
                "spikes": len(spikes),
                "moments": len(moments),
            },
        )
    return summary


def _add_moment(
    conn: sqlite3.Connection, vod: sqlite3.Row, created: datetime, moment: vod_spikes.Moment
) -> None:
    title = f"Chat moment at {vod_spikes.clock(moment.peak)}"
    if vod["title"]:
        title += f" - {vod['title']}"
    conn.execute(
        "INSERT INTO clips (id, streamer_id, source, title, created_at, view_count, duration, "
        "vod_id, vod_offset, url, state, score) VALUES (?, ?, 'vod_spike', ?, ?, NULL, ?, ?, ?, "
        "?, 'found', ?) ON CONFLICT(id) DO NOTHING",
        (
            f"vod:{vod['id']}:{moment.start}",
            vod["streamer_id"],
            title[:300],
            to_rfc3339(created + timedelta(seconds=moment.start)),
            float(moment.duration),
            vod["id"],
            moment.start,
            vod["url"],
            moment.score,
        ),
    )


def _skip(conn: sqlite3.Connection, vod_id: str, reason: str, now: datetime) -> None:
    conn.execute(
        "UPDATE vods SET state = 'skipped', skip_reason = ?, processed_at = ?, retry_at = NULL "
        "WHERE id = ?",
        (reason[:500], to_rfc3339(now), vod_id),
    )


def _record_failure(
    conn: sqlite3.Connection, vod: sqlite3.Row, error: str, now: datetime, summary: VodSummary
) -> None:
    failures = vod["fail_count"] + 1
    summary.failed += 1
    summary.errors.append(f"{vod['id']}: {error}")
    if failures > len(RETRY_DELAYS):
        _skip(conn, vod["id"], f"gave up after {failures} tries: {error}", now)
        conn.execute("UPDATE vods SET fail_count = ? WHERE id = ?", (failures, vod["id"]))
        return
    retry = now + RETRY_DELAYS[failures - 1]
    conn.execute(
        "UPDATE vods SET fail_count = ?, last_error = ?, retry_at = ? WHERE id = ?",
        (failures, error[:1000], to_rfc3339(retry), vod["id"]),
    )
