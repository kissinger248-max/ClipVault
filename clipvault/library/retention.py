"""Keeping the library tidy: disk guard, retention, and noticing files deleted by hand.

Safety rules (hard rule 8), enforced in :func:`apply_retention`:
- only files recorded in the database are ever deleted;
- a file is deleted only if it really sits inside the library folder;
- starred clips and anything in ``Favorites/`` are never deleted.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from clipvault import db
from clipvault.alerts import raise_alert, resolve_alerts
from clipvault.config import Config
from clipvault.library import storage
from clipvault.library.storage import Library
from clipvault.logging_setup import ctx
from clipvault.timeutil import to_rfc3339, utc_now

POSTED_KEEP = timedelta(days=30)
# If this many tracked files vanish at once, the library was probably moved, not cleaned up.
MASS_MISSING_MIN = 10
MASS_MISSING_SHARE = 0.2
_PATH_COLUMNS = ("file_path", "sidecar_path", "transcript_path", "portrait_path")
# Clips judged by their view count; moments found from chat have none.
_BY_VIEWS = "source IN ('viewer', 'auto')"

log = logging.getLogger(__name__)


# --- disk guard --------------------------------------------------------------


def free_space_pct(path: Path) -> float:
    """Percentage of free space on the drive holding ``path`` (or its nearest existing parent)."""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return 100 * usage.free / usage.total


def check_disk(conn: sqlite3.Connection, config: Config, library: Library) -> bool:
    """Return True when there is room to download; open or close the storage alert to match."""
    limit = config.retention.min_free_space_pct
    try:
        free = free_space_pct(library.write_base)
    except OSError as exc:
        raise_alert(conn, "storage", f"Can't read free space for {library.write_base}: {exc}")
        return False
    if free < limit:
        raise_alert(
            conn,
            "storage",
            f"Downloads are paused: less than {limit}% of the disk is free. "
            "Free up space or move the library to a bigger drive.",
            "/settings#storage",
        )
        return False
    resolve_alerts(conn, "storage")
    return True


# --- retention ---------------------------------------------------------------


@dataclass(frozen=True)
class Removal:
    """A clip that retention would delete, and why."""

    clip_id: str
    login: str
    relpath: str
    view_count: int
    median_views: float
    size: int


def plan_retention(
    conn: sqlite3.Connection, config: Config, library: Library, now: datetime | None = None
) -> list[Removal]:
    """List clips whose files retention would delete right now (nothing is changed).

    A viewer clip goes when it is older than ``keep_days_below_median`` days AND
    has fewer views than the median of every viewer clip ever saved for that
    streamer. The median covers clips already removed too, so the bar doesn't
    creep up and eat the whole archive run after run. Kept regardless: moments
    found from chat (live or finished streams have no view count, so this rule
    can't judge them; there are a few per stream), starred clips, files moved
    to ``Favorites/``, and sources of posts that are queued or were posted in the
    last 30 days.
    """
    if not library.available:
        return []
    now = now or utc_now()
    cutoff = to_rfc3339(now - timedelta(days=config.retention.keep_days_below_median))
    posted_after = to_rfc3339(now - POSTED_KEEP)

    medians: dict[int, float] = {}
    views_by_streamer: dict[int, list[int]] = {}
    for row in conn.execute(
        f"SELECT streamer_id, view_count FROM clips WHERE fetched_at IS NOT NULL AND {_BY_VIEWS}"
    ):
        views_by_streamer.setdefault(row["streamer_id"], []).append(row["view_count"] or 0)
    for streamer_id, views in views_by_streamer.items():
        medians[streamer_id] = statistics.median(views)

    rows = conn.execute(
        "SELECT c.id, c.streamer_id, c.file_path, c.view_count, c.file_size, s.login "
        "FROM clips c JOIN streamers s ON s.id = c.streamer_id "
        "WHERE c.file_path IS NOT NULL AND c.removed_at IS NULL AND c.starred = 0 "
        f"AND c.{_BY_VIEWS} AND c.created_at < ? "
        "AND NOT EXISTS (SELECT 1 FROM posts p JOIN renders r ON r.id = p.render_id "
        "  WHERE r.clip_id = c.id AND (p.status IN ('queued', 'approved') "
        "  OR (p.status = 'posted' AND p.posted_at > ?))) "
        "ORDER BY c.created_at",
        (cutoff, posted_after),
    ).fetchall()

    plan: list[Removal] = []
    for row in rows:
        median = medians.get(row["streamer_id"])
        views = row["view_count"] or 0
        if median is None or views >= median:
            continue
        path = storage.resolve(library, row["file_path"])
        if path is None or storage.is_favorite(library, path):
            continue  # already gone, or the user chose to keep it
        plan.append(
            Removal(row["id"], row["login"], row["file_path"], views, median, row["file_size"] or 0)
        )
    return plan


def clip_relpaths(conn: sqlite3.Connection, row: sqlite3.Row) -> list[str]:
    """Every library file the app made for a clip: video, sidecar, subtitles, word
    timings, Twitch's portrait version and the vertical renders."""
    paths = [row[column] for column in _PATH_COLUMNS if row[column]]
    if row["file_path"]:
        paths.append(row["file_path"].rsplit(".", 1)[0] + ".words.json")
    paths += [
        r["path"]
        for r in conn.execute(
            "SELECT path FROM renders WHERE clip_id = ? AND path IS NOT NULL", (row["id"],)
        )  # fmt: skip
    ]
    return list(dict.fromkeys(paths))


def remove_clip_files(
    conn: sqlite3.Connection, library: Library, clip_id: str, reason: str, job: str
) -> bool:
    """Delete a clip's files (only inside the library, never in Favorites) and mark it.

    Returns False when nothing was deleted (no files, or the clip is starred).
    Only files recorded in the database are touched (hard rule 8).
    """
    row = conn.execute(
        "SELECT c.*, s.login FROM clips c JOIN streamers s ON s.id = c.streamer_id WHERE c.id = ?",
        (clip_id,),
    ).fetchone()
    if row is None or row["removed_at"] or row["starred"]:
        return False
    log_ctx = ctx(clip=clip_id, streamer=row["login"], job=job)
    from clipvault.library import edited

    deleted_any = edited.withdraw(conn, library, clip_id) > 0  # its "Edited clips" files
    for relpath in clip_relpaths(conn, row):
        path = storage.resolve(library, relpath)
        if path is None:
            continue
        if not storage.is_inside_library(library, path) or storage.is_favorite(library, path):
            log.error("refusing to delete a file outside the library folder: %s", path,
                      extra=log_ctx)  # fmt: skip
            continue
        path.unlink()
        deleted_any = True
    if deleted_any:
        _mark_removed(conn, clip_id, reason)
        log.info("clip files removed (%s)", reason, extra=log_ctx)
    return deleted_any


def apply_retention(conn: sqlite3.Connection, library: Library, plan: list[Removal]) -> int:
    """Delete the files for each planned removal and mark the clips; return clips removed."""
    return sum(
        1 for item in plan if remove_clip_files(conn, library, item.clip_id, "retention", "cleanup")
    )


# --- files deleted by hand ---------------------------------------------------


def mark_missing_files(conn: sqlite3.Connection, library: Library) -> int:
    """Notice clips whose video file was deleted by hand; return how many were marked.

    Marked clips keep their row, so they are never downloaded again. Nothing is
    marked while the library is away, and if a large share of files vanishes at
    once an alert is raised instead: that pattern means the folder was moved.
    """
    if not library.available:
        return 0
    restore_returned_files(conn, library)
    rows = conn.execute(
        "SELECT id, file_path FROM clips WHERE file_path IS NOT NULL AND removed_at IS NULL "
        "AND state NOT IN ('found', 'rejected', 'dead')"
    ).fetchall()
    missing = [row["id"] for row in rows if storage.resolve(library, row["file_path"]) is None]
    if not missing:
        resolve_alerts(conn, "library")
        return 0
    if len(missing) >= MASS_MISSING_MIN and len(missing) >= MASS_MISSING_SHARE * len(rows):
        raise_alert(
            conn,
            "library",
            f"{len(missing)} of {len(rows)} saved clips can't be found in {library.root}. "
            "If you moved the library folder, set the new location in Settings; "
            "nothing was changed.",
            "/settings#storage",
        )
        return 0
    with db.transaction(conn):
        for clip_id in missing:
            _mark_removed(conn, clip_id, "missing")
    log.info("marked clips with missing files", extra=ctx(job="cleanup", count=len(missing)))
    return len(missing)


def restore_returned_files(conn: sqlite3.Connection, library: Library) -> int:
    """Un-mark clips marked 'missing' whose file is back; return how many.

    A file can be away for a while without having been deleted (restored from
    the recycle bin, a sync still running, moved into ``Favorites/`` later).
    """
    if not library.available:
        return 0
    rows = conn.execute(
        "SELECT id, file_path FROM clips WHERE removed_reason = 'missing'"
    ).fetchall()
    back = [row["id"] for row in rows if storage.resolve(library, row["file_path"]) is not None]
    for clip_id in back:
        conn.execute(
            "UPDATE clips SET removed_at = NULL, removed_reason = NULL WHERE id = ?", (clip_id,)
        )
    if back:
        log.info("clips whose files came back", extra=ctx(job="cleanup", count=len(back)))
    return len(back)


def _mark_removed(conn: sqlite3.Connection, clip_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE clips SET removed_at = ?, removed_reason = ? WHERE id = ?",
        (to_rfc3339(utc_now()), reason, clip_id),
    )
