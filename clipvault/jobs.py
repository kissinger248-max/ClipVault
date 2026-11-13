"""Job definitions and clip state transitions (SPEC §3).

Each transition is one step. Steps are idempotent: they look at the database
and the file system first, so a crash at any point can simply be run again.
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
from clipvault.alerts import get_setting, raise_alert, resolve_alerts, set_setting
from clipvault.config import Config
from clipvault.fetch import ytdlp
from clipvault.fetch.ytdlp import DownloadError
from clipvault.library import retention, storage
from clipvault.logging_setup import ctx
from clipvault.process.filter import Span, find_duplicates
from clipvault.timeutil import parse_rfc3339, to_rfc3339, utc_now

# Clip states in pipeline order.
PIPELINE_STATES = (
    "found",
    "fetched",
    "filtered",
    "transcribed",
    "written",
    "rendered",
    "qa_passed",
    "scheduled",
    "posted",
    "submitted",
)
# States a clip can end in off the main line.
SIDE_STATES = ("rejected", "qa_failed", "dead")
ALL_STATES = PIPELINE_STATES + SIDE_STATES

# A failed step is retried after 1, 5 and 30 minutes; when the last retry fails too,
# the clip is dead and no longer holds anything up.
RETRY_DELAYS_MIN = (1, 5, 30)
MAX_FAILURES = len(RETRY_DELAYS_MIN) + 1

# After this many download failures in a row (across clips) the run stops and an
# alert says the downloader may need an update (SPEC §6).
DOWNLOADER_ALERT_AFTER = 3
_FAILURE_STREAK_SETTING = "fetch.consecutive_failures"
DOWNLOADER_ALERT = "Clip downloads keep failing; the downloader (yt-dlp) may need an update."

FETCH_JOB = "fetch"
Downloader = Callable[[str, Path, str], Path]
log = logging.getLogger(__name__)


def record_failure(
    conn: sqlite3.Connection, clip_id: str, error: str, now: datetime | None = None
) -> str:
    """Count a failed step for a clip and schedule the retry; return the clip's state.

    The clip stays in its current state with ``retry_at`` set, or becomes
    ``dead`` once every retry has failed.
    """
    now = now or utc_now()
    row = conn.execute("SELECT state, fail_count FROM clips WHERE id = ?", (clip_id,)).fetchone()
    failures = row["fail_count"] + 1
    if failures >= MAX_FAILURES:
        conn.execute(
            "UPDATE clips SET state = 'dead', fail_count = ?, last_error = ?, retry_at = NULL "
            "WHERE id = ?",
            (failures, error[:1000], clip_id),
        )
        return "dead"
    retry_at = now + timedelta(minutes=RETRY_DELAYS_MIN[failures - 1])
    conn.execute(
        "UPDATE clips SET fail_count = ?, last_error = ?, retry_at = ? WHERE id = ?",
        (failures, error[:1000], to_rfc3339(retry_at), clip_id),
    )
    return row["state"]


# --- fetch: found -> fetched ---------------------------------------------------


@dataclass
class FetchSummary:
    """What one fetch run did."""

    downloaded: int = 0
    already_there: int = 0
    duplicates: int = 0
    unavailable: int = 0
    failed: int = 0
    bytes: int = 0
    waiting: int = 0  # clips whose retry time hasn't come yet
    remaining: int = 0  # clips still to download after this run
    paused_for_disk: bool = False
    stopped_on_failures: bool = False
    library_away: bool = False
    errors: list[str] = field(default_factory=list)


def reject_duplicates(conn: sqlite3.Connection, login: str | None = None) -> int:
    """Reject not-yet-downloaded clips that repeat another clip of the same VOD."""
    query = (
        "SELECT c.id, c.vod_id, c.vod_offset, c.duration, c.view_count, c.created_at, c.state "
        "FROM clips c JOIN streamers s ON s.id = c.streamer_id "
        "WHERE c.state NOT IN ('rejected', 'dead') AND c.removed_at IS NULL"
    )
    params: list[str] = []
    if login:
        query += " AND s.login = ?"
        params.append(login)
    rows = conn.execute(query, params).fetchall()
    spans = [
        Span(
            id=r["id"],
            vod_id=r["vod_id"],
            start=r["vod_offset"],
            duration=r["duration"] or 0.0,
            view_count=r["view_count"] or 0,
            created_at=r["created_at"] or "",
            settled=r["state"] != "found",
        )
        for r in rows
    ]
    losers = find_duplicates(spans)
    if losers:
        with db.transaction(conn):
            for loser, winner in losers.items():
                conn.execute(
                    "UPDATE clips SET state = 'rejected', reject_reason = 'duplicate', "
                    "last_error = ?, retry_at = NULL WHERE id = ? AND state = 'found'",
                    (f"same moment as clip {winner}", loser),
                )
    return len(losers)


def run_fetch(
    conn: sqlite3.Connection,
    config: Config,
    *,
    login: str | None = None,
    limit: int | None = None,
    now: datetime | None = None,
    download: Downloader = ytdlp.download_clip,
    on_progress: Callable[[str], None] | None = None,
) -> FetchSummary:
    """Download every clip in state ``found`` (most-viewed first) into the library.

    Safe to run again at any time: a clip whose file is already complete is
    simply marked ``fetched``, and nothing is downloaded twice.
    """
    now = now or utc_now()
    tz = ZoneInfo(config.timezone)
    say = on_progress or (lambda _line: None)
    summary = FetchSummary()

    library = storage.open_library(conn, config)
    summary.library_away = not library.available
    if library.available:
        resolve_alerts(conn, "library")
        storage.relocate_cache(library)
    else:
        raise_alert(
            conn,
            "library",
            f"The library folder {library.root} can't be found (is the drive connected?). "
            "New clips are kept in a temporary folder and moved over when it is back.",
            "/settings#storage",
        )

    summary.duplicates = reject_duplicates(conn, login)

    query = (
        "SELECT c.*, s.login FROM clips c JOIN streamers s ON s.id = c.streamer_id "
        "WHERE c.state = 'found' AND c.removed_at IS NULL AND s.save_enabled = 1"
    )
    params: list[str] = []
    if login:
        query += " AND s.login = ?"
        params.append(login)
    pending = conn.execute(query + " ORDER BY c.view_count DESC, c.id", params).fetchall()
    due = [c for c in pending if not c["retry_at"] or parse_rfc3339(c["retry_at"]) <= now]
    summary.waiting = len(pending) - len(due)
    todo = due[:limit] if limit else due

    streak = int(get_setting(conn, _FAILURE_STREAK_SETTING, "0") or 0)
    run_streak = 0
    done = 0
    for index, clip in enumerate(todo, start=1):
        if not retention.check_disk(conn, config, library):
            summary.paused_for_disk = True
            break
        log_ctx = ctx(clip=clip["id"], streamer=clip["login"], job=FETCH_JOB)
        relpath = clip["file_path"]
        if not relpath:
            relpath = storage.clip_relpath(
                clip["login"],
                clip["id"],
                clip["title"] or "",
                clip["view_count"] or 0,
                parse_rfc3339(clip["created_at"]),
                tz,
                root_chars=len(str(library.root)),
            )
            # Record the name before downloading: a crash mid-download then resumes
            # into the same file instead of starting a second one under a new name.
            conn.execute("UPDATE clips SET file_path = ? WHERE id = ?", (relpath, clip["id"]))

        path = storage.resolve(library, relpath)
        try:
            if path is None:
                path = download(clip["url"], library.write_base / relpath, config.quality)
                summary.downloaded += 1
            else:
                summary.already_there += 1
        except DownloadError as exc:
            done = index
            if exc.permanent:
                conn.execute(
                    "UPDATE clips SET state = 'rejected', reject_reason = 'unavailable', "
                    "last_error = ?, file_path = NULL, retry_at = NULL WHERE id = ?",
                    (str(exc)[:1000], clip["id"]),
                )
                summary.unavailable += 1
                log.info("clip is no longer available: %s", exc, extra=log_ctx)
                say(f"[{index}/{len(todo)}] {clip['login']}: gone from Twitch, skipped")
                continue
            state = record_failure(conn, clip["id"], str(exc), now)
            summary.failed += 1
            summary.errors.append(f"{clip['id']}: {exc}")
            log.error("download failed (%s): %s", state, exc, extra=log_ctx)
            say(f"[{index}/{len(todo)}] {clip['login']}: FAILED - {exc}")
            streak += 1
            run_streak += 1
            set_setting(conn, _FAILURE_STREAK_SETTING, str(streak))
            if streak >= DOWNLOADER_ALERT_AFTER:
                raise_alert(conn, "downloader", DOWNLOADER_ALERT, "/alerts")
            if run_streak >= DOWNLOADER_ALERT_AFTER:
                summary.stopped_on_failures = True
                break
            continue

        done = index
        size = path.stat().st_size
        fetched_at = to_rfc3339(utc_now())
        sidecar_rel = storage.sidecar_relpath(relpath)
        sidecar = _sidecar(clip, relpath, sidecar_rel, size, fetched_at)
        storage.write_sidecar(path.with_suffix(".json"), sidecar)
        with db.transaction(conn):
            conn.execute(
                "UPDATE clips SET state = 'fetched', downloaded_via = 'unofficial', file_path = ?, "
                "sidecar_path = ?, file_size = ?, fetched_at = ?, fail_count = 0, "
                "last_error = NULL, retry_at = NULL WHERE id = ?",
                (relpath, sidecar_rel, size, fetched_at, clip["id"]),
            )
            if streak:
                streak = 0
                set_setting(conn, _FAILURE_STREAK_SETTING, "0")
        run_streak = 0
        resolve_alerts(conn, "downloader")
        summary.bytes += size
        log.info("clip saved", extra={**log_ctx, "bytes": size, "path": relpath})
        say(
            f"[{index}/{len(todo)}] {clip['login']}: {size / 1_048_576:5.1f} MB  "
            f"{(clip['title'] or '')[:60]}"
        )

    summary.remaining = len(due) - done
    return summary


def _sidecar(clip: sqlite3.Row, relpath: str, sidecar_rel: str, size: int, fetched_at: str) -> dict:
    """Everything known about a clip, for the JSON file saved next to it."""
    data = {key: clip[key] for key in clip.keys() if key not in ("retry_at", "last_error")}
    data.update(
        streamer_login=clip["login"],
        source_url=clip["url"],
        state="fetched",
        downloaded_via="unofficial",
        file_path=relpath,
        sidecar_path=sidecar_rel,
        file_size=size,
        fetched_at=fetched_at,
        fail_count=0,
    )
    data.pop("login", None)
    return data
