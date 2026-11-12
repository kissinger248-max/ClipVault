"""Discovery of viewer clips through Helix Get Clips (SPEC §5.1).

How a run works, per streamer:

1. Work out which local days to look at: from the day of the streamer's cursor
   (minus a one-day lookback, so late view-count changes are noticed) up to today.
   A streamer with no cursor starts 7 days back.
2. Ask Twitch for each day's clips. Twitch sorts them by view count, so one page
   holds the day's top 100.
3. Pick clips: each day's top ``top_n`` by views, or, when the streamer has
   ``min_views`` set, every clip at or above it.
4. In one transaction: insert picked clips that aren't known yet as ``found``,
   refresh view counts of known ones, and move the cursor.

Whole days are always re-read, so "top N of the day" is judged on the full day
and running the job twice in a row adds nothing new.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from clipvault import db, runs
from clipvault.config import Config
from clipvault.logging_setup import ctx
from clipvault.streamers import refresh_profiles
from clipvault.timeutil import parse_rfc3339, to_rfc3339, utc_now
from clipvault.twitch.api import TwitchAPIError, TwitchClient

JOB = "discover"
FIRST_RUN_LOOKBACK = timedelta(days=7)
CURSOR_LOOKBACK = timedelta(days=1)
MAX_SINCE = timedelta(days=90)
THRESHOLD_MAX_PAGES = 10

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Clip:
    """A clip as Twitch describes it."""

    id: str
    url: str
    title: str
    created_at: datetime
    view_count: int
    duration: float
    video_id: str | None
    vod_offset: int | None
    game_id: str | None
    creator_name: str | None


@dataclass
class DiscoverResult:
    """Outcome of discovery for one streamer."""

    login: str
    days: int = 0
    seen: int = 0
    new_clips: list[Clip] = field(default_factory=list)
    error: str | None = None


def parse_clip(raw: dict[str, Any]) -> Clip:
    """Convert one item of a Get Clips response."""
    return Clip(
        id=raw["id"],
        url=raw["url"],
        title=raw.get("title") or "",
        created_at=parse_rfc3339(raw["created_at"]),
        view_count=int(raw.get("view_count") or 0),
        duration=float(raw.get("duration") or 0.0),
        video_id=raw.get("video_id") or None,  # Twitch sends "" when the VOD is gone
        vod_offset=raw.get("vod_offset"),
        game_id=raw.get("game_id") or None,
        creator_name=raw.get("creator_name") or None,
    )


def select_clips(clips: list[Clip], top_n: int, min_views: int | None, tz: tzinfo) -> list[Clip]:
    """Pick the clips worth keeping, oldest first.

    With ``min_views`` set: every clip at or above it. Otherwise: the ``top_n``
    most-viewed clips of each local day (ties go to the older clip).
    """
    if min_views is not None:
        picked = [c for c in clips if c.view_count >= min_views]
    else:
        by_day: dict[Any, list[Clip]] = defaultdict(list)
        for clip in clips:
            by_day[clip.created_at.astimezone(tz).date()].append(clip)
        picked = []
        for day_clips in by_day.values():
            day_clips.sort(key=lambda c: (-c.view_count, c.created_at, c.id))
            picked += day_clips[:top_n]
    return sorted(picked, key=lambda c: (c.created_at, c.id))


def day_windows(start: datetime, end: datetime, tz: tzinfo) -> list[tuple[datetime, datetime]]:
    """Split ``start..end`` into whole local days, as UTC ``(from, to)`` pairs.

    The first window begins at local midnight of ``start``'s day; the last one
    stops at ``end``. Windows shorter than a minute are dropped (Twitch
    truncates times to the minute).
    """
    windows = []
    day = start.astimezone(tz).date()
    last_day = end.astimezone(tz).date()
    while day <= last_day:
        day_start = datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)
        day += timedelta(days=1)
        day_end = min(datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC), end)
        if day_end - day_start >= timedelta(minutes=1):
            windows.append((day_start, day_end))
    return windows


def discover_streamer(
    conn: sqlite3.Connection,
    client: TwitchClient,
    streamer: sqlite3.Row,
    tz: tzinfo,
    *,
    now: datetime | None = None,
    since: timedelta | None = None,
) -> DiscoverResult:
    """Find new clips for one streamer and store them as ``found``.

    Raises ``TwitchAPIError`` when Twitch can't be read; nothing is stored and
    the cursor stays put, so the next run covers the same period again.
    """
    now = now or utc_now()
    job = f"{JOB}:{streamer['id']}"
    result = DiscoverResult(login=streamer["login"])

    cursor = runs.get_cursor(conn, job)
    if since is not None:
        start = now - min(since, MAX_SINCE)
    elif cursor:
        start = max(parse_rfc3339(cursor) - CURSOR_LOOKBACK, now - MAX_SINCE)
    else:
        start = now - FIRST_RUN_LOOKBACK

    # 1. Read from Twitch (no database lock held while waiting on the network).
    top_n, min_views = streamer["top_n"], streamer["min_views"]
    fetched: dict[str, Clip] = {}
    windows = day_windows(start, now, tz)
    for window_start, window_end in windows:
        pages = THRESHOLD_MAX_PAGES if min_views is not None else 1
        for raw in client.iter_clips(
            streamer["twitch_user_id"], window_start, window_end, max_pages=pages
        ):
            clip = parse_clip(raw)
            if min_views is not None and clip.view_count < min_views:
                break  # sorted by views: nothing further down can qualify
            fetched[clip.id] = clip
    result.days, result.seen = len(windows), len(fetched)

    picked = select_clips(list(fetched.values()), top_n, min_views, tz)
    known = {
        row["id"]: row
        for row in conn.execute(
            "SELECT id, view_count, vod_id, vod_offset FROM clips WHERE streamer_id = ?",
            (streamer["id"],),
        )
    }
    new_clips = [c for c in picked if c.id not in known]
    game_names = _game_names(conn, client, {c.game_id for c in new_clips if c.game_id})

    # 2. Write everything for this streamer in one short transaction.
    with db.transaction(conn):
        for clip in new_clips:
            conn.execute(
                "INSERT INTO clips (id, streamer_id, source, title, created_at, view_count, "
                "duration, vod_id, vod_offset, game_id, game_name, creator_name, url, state) "
                "VALUES (?, ?, 'viewer', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'found') "
                "ON CONFLICT(id) DO NOTHING",
                (
                    clip.id,
                    streamer["id"],
                    clip.title,
                    to_rfc3339(clip.created_at),
                    clip.view_count,
                    clip.duration,
                    clip.video_id,
                    clip.vod_offset,
                    clip.game_id,
                    game_names.get(clip.game_id or ""),
                    clip.creator_name,
                    clip.url,
                ),
            )
        for clip in fetched.values():
            row = known.get(clip.id)
            if row is None:
                continue
            # Views keep growing, and Twitch fills in the VOD position minutes after creation.
            vod_id = row["vod_id"] or clip.video_id
            vod_offset = row["vod_offset"] if row["vod_offset"] is not None else clip.vod_offset
            if (clip.view_count, vod_id, vod_offset) != (
                row["view_count"],
                row["vod_id"],
                row["vod_offset"],
            ):
                conn.execute(
                    "UPDATE clips SET view_count = ?, vod_id = ?, vod_offset = ? WHERE id = ?",
                    (clip.view_count, vod_id, vod_offset, clip.id),
                )
        runs.mark_success(conn, job, cursor=to_rfc3339(now))

    result.new_clips = new_clips
    return result


def discover_all(
    conn: sqlite3.Connection,
    client: TwitchClient,
    config: Config,
    *,
    login: str | None = None,
    since: timedelta | None = None,
    now: datetime | None = None,
) -> list[DiscoverResult]:
    """Run discovery for every streamer with saving on (or just ``login``).

    One streamer failing doesn't stop the others; its result carries the error.
    ``TwitchAuthError`` (bad keys) stops the whole run, since nothing can work.
    """
    now = now or utc_now()
    tz = ZoneInfo(config.timezone)
    runs.mark_started(conn, JOB)
    try:
        missing = set(refresh_profiles(conn, client, now))
    except TwitchAPIError as exc:
        runs.mark_error(conn, JOB, f"profile lookup: {exc}")
        raise

    query = "SELECT * FROM streamers WHERE (save_enabled = 1 OR post_enabled = 1)"
    params: list[Any] = []
    if login:
        query += " AND login = ?"
        params.append(login)
    results: list[DiscoverResult] = []
    for streamer in conn.execute(query + " ORDER BY login", params).fetchall():
        if not streamer["twitch_user_id"]:
            reason = "Twitch has no user with this login" if streamer["login"] in missing else ""
            results.append(DiscoverResult(streamer["login"], error=reason or "no Twitch id yet"))
            continue
        try:
            results.append(discover_streamer(conn, client, streamer, tz, now=now, since=since))
        except TwitchAPIError as exc:
            log.error("discovery failed: %s", exc, extra=ctx(streamer=streamer["login"], job=JOB))
            runs.mark_error(conn, f"{JOB}:{streamer['id']}", str(exc))
            results.append(DiscoverResult(streamer["login"], error=str(exc)))

    failed = [r.login for r in results if r.error]
    if failed:
        runs.mark_error(conn, JOB, f"failed for: {', '.join(failed)}")
    else:
        runs.mark_success(conn, JOB, cursor=to_rfc3339(now))
    log.info(
        "discovery finished",
        extra=ctx(
            job=JOB,
            streamers=len(results),
            new_clips=sum(len(r.new_clips) for r in results),
            failed=failed,
        ),
    )
    return results


def _game_names(conn: sqlite3.Connection, client: TwitchClient, game_ids: set[str]) -> dict:
    """Return ``{game_id: name}``, asking Twitch only for ids not cached yet.

    A failed lookup is not fatal: the clip is stored with its game id and no name.
    """
    if not game_ids:
        return {}
    marks = ", ".join("?" * len(game_ids))
    names = {
        row["id"]: row["name"]
        for row in conn.execute(f"SELECT id, name FROM games WHERE id IN ({marks})", list(game_ids))
    }
    unknown = game_ids - names.keys()
    if unknown:
        try:
            fresh = client.get_games(unknown)
        except TwitchAPIError as exc:
            log.warning("game name lookup failed: %s", exc, extra=ctx(job=JOB))
            return names
        for game_id, name in fresh.items():
            conn.execute("INSERT OR REPLACE INTO games (id, name) VALUES (?, ?)", (game_id, name))
        names.update(fresh)
    return names
