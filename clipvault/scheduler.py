"""The scheduled steps that keep ClipVault running unattended (SPEC §4).

Each step is a plain function ``(conn, config, now) -> str`` that does one
round of work and returns a short note for the log (an empty note means there
was nothing to do, which is only logged at debug level). :func:`run_step` wraps a
step with the bookkeeping every step needs: its own database connection, the
pause switch, the ``runs`` row, and the "broken" alert after 3 failures in a row.

Catching up after sleep needs no special code in the steps: discovery works
from per-streamer cursors, the worker takes whatever is waiting, and the daily
steps are run whenever their last success is older than their latest due time
(:func:`daily_due`), checked every minute by the heartbeat.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from clipvault import activity, alerts, db, jobs, power, runs
from clipvault import config as cfg
from clipvault.logging_setup import ctx
from clipvault.notify import ntfy
from clipvault.timeutil import parse_rfc3339, to_rfc3339, utc_now

log = logging.getLogger(__name__)

FETCH_BATCH = 20  # clips per worker round, so pausing or quitting takes effect quickly
TRANSCRIBE_BATCH = 5
RENDER_BATCH = 3  # about 25 s each; the worker runs every 2 minutes
BROKEN_AFTER = 3
CLEANUP_AT = "04:00"
BACKUP_FOLDER = ".clipvault-backups"

Step = Callable[[sqlite3.Connection, cfg.Config, datetime], str]


class StepFailed(Exception):
    """A step finished but could not do its job (counts towards the "broken" alert)."""


@dataclass(frozen=True)
class StepInfo:
    name: str
    label: str  # plain words for alerts
    runs_while_paused: bool = False


STEPS = {
    "discover": StepInfo("discover", "find new clips"),
    "worker": StepInfo("worker", "download and transcribe"),
    "vods": StepInfo("vods", "find moments in stream chat"),
    "cleanup": StepInfo("cleanup", "daily cleanup"),
    "summary": StepInfo("summary", "daily summary", runs_while_paused=True),
    "notify": StepInfo("notify", "send notifications", runs_while_paused=True),
    "updates": StepInfo("updates", "check for updates", runs_while_paused=True),
}


def step_job(name: str) -> str:
    """The ``runs`` row a scheduled step keeps its bookkeeping in."""
    return f"step:{name}"


def broken_message(name: str) -> str:
    return (
        f"The '{STEPS[name].label}' step has failed {BROKEN_AFTER} times in a row. "
        "The log in the logs folder says why."
    )


def run_step(
    name: str,
    step: Step,
    config: cfg.Config,
    *,
    db_path: Path | None = None,
    now: datetime | None = None,
) -> str | None:
    """Run one step with its bookkeeping; returns its note, or None if skipped or failed."""
    from clipvault import lifecycle

    if lifecycle.stopping.is_set():
        return None  # the app is quitting: start nothing new
    info = STEPS[name]
    job = step_job(name)
    conn = db.open_db(db_path or cfg.db_path())
    try:
        if alerts.is_paused(conn) and not info.runs_while_paused:
            return None
        runs.mark_started(conn, job)
        activity.step_started(name)
        try:
            note = step(conn, config, now or utc_now())
        except Exception as exc:  # one failed round must never stop the scheduler
            error = f"{exc.__class__.__name__}: {exc}"
            streak = runs.record_step_failure(conn, job, error)
            log.error("step failed: %s", error, extra=ctx(job=name, streak=streak), exc_info=True)
            if streak >= BROKEN_AFTER:
                alerts.raise_alert(conn, "broken", broken_message(name))
            return None
        finally:
            activity.step_finished(name)
        runs.record_step_success(conn, job)
        alerts.resolve_alert(conn, "broken", broken_message(name))
        level = logging.INFO if note else logging.DEBUG
        log.log(level, "step finished: %s", note or "nothing to do", extra=ctx(job=name))
        return note
    finally:
        conn.close()


# --- when daily steps are due ----------------------------------------------------


def latest_due(now: datetime, at: str, tz: ZoneInfo) -> datetime:
    """The most recent moment (UTC) the local clock showed ``at``, at or before ``now``."""
    hour, minute = (int(part) for part in at.split(":"))
    local = now.astimezone(tz)
    due = datetime.combine(local.date(), time(hour, minute), tzinfo=tz)
    if due > local:
        due = datetime.combine(local.date() - timedelta(days=1), time(hour, minute), tzinfo=tz)
    return due.astimezone(now.tzinfo)


def daily_due(conn: sqlite3.Connection, name: str, at: str, tz: ZoneInfo, now: datetime) -> bool:
    """Is a daily step due: never ran, or last succeeded before its latest due time?"""
    row = runs.get_run(conn, step_job(name))
    if row is None or row["last_success"] is None:
        return True
    return parse_rfc3339(row["last_success"]) < latest_due(now, at, tz)


# --- the steps ---------------------------------------------------------------------


def step_discover(conn: sqlite3.Connection, config: cfg.Config, now: datetime) -> str:
    """Find new viewer clips for every streamer (catches up from each cursor)."""
    from clipvault import twitch
    from clipvault.discover.viewer_clips import discover_all

    if not conn.execute("SELECT 1 FROM streamers").fetchone():
        return "no streamers yet"
    try:
        client = twitch.build_client()
    except twitch.MissingCredentials as exc:
        raise StepFailed(str(exc)) from exc
    try:
        results = discover_all(conn, client, config, now=now)
    finally:
        client.close()
    # A login Twitch doesn't know is the user's to fix, not a failing step.
    failed = [r for r in results if r.error and "no user with this login" not in r.error]
    new = sum(len(r.new_clips) for r in results)
    for result in results:
        if result.new_clips:
            count = len(result.new_clips)
            activity.event("found", f"{count} new clip{'s' if count != 1 else ''} found",
                           login=result.login)  # fmt: skip
    if failed and len(failed) == len(results):
        raise StepFailed(f"discovery failed for every streamer: {failed[0].error}")
    return f"{new} new clip(s) from {len(results)} streamer(s), {len(failed)} failed"


def step_worker(conn: sqlite3.Connection, config: cfg.Config, now: datetime) -> str:
    """Download and cut waiting clips, then subtitle and edit a few (those on mains power).

    Cutting a moment from a stream recording is a short download, so it runs on battery
    too: live moments are saved while the stream is still on.
    """
    from clipvault.fetch import ytdlp

    ytdlp.update_if_due(conn, now=now)
    heavy = power.heavy_allowed(config)
    fetched = jobs.run_fetch(conn, config, limit=FETCH_BATCH, now=now, allow_vod_cuts=True)
    if fetched.stopped_on_failures:
        raise StepFailed("downloads failed several times in a row")
    notes = []
    saved = fetched.downloaded + fetched.already_there
    if saved or fetched.failed or fetched.remaining:
        notes.append(f"saved {saved}, {fetched.failed} failed, {fetched.remaining} waiting")
    if config.transcription.enabled:
        if not heavy:
            notes.append("subtitles and editing wait for mains power")
        else:
            done = jobs.run_transcribe(conn, config, limit=TRANSCRIBE_BATCH, now=now)
            if done.stopped_on_failures:
                raise StepFailed("transcription failed several times in a row")
            finished = done.done + done.no_speech
            if finished or done.failed or done.remaining:
                notes.append(
                    f"transcribed {finished}, {done.failed} failed, {done.remaining} waiting"
                )
    if heavy:
        # Every new clip gets its phone version (the dashboard shows those), newest first.
        made = jobs.run_render(conn, config, limit=RENDER_BATCH)
        if made.rendered or made.failed:
            notes.append(f"edited {made.rendered}, {made.failed} failed, {made.remaining} waiting")
        if made.refreshed:
            notes.append(f"{made.refreshed} older phone version(s) given a new headline")
    return "; ".join(notes)


VODS_PER_ROUND = 3


def step_vods(conn: sqlite3.Connection, config: cfg.Config, now: datetime) -> str:
    """Look for finished streams and search their chat for moments nobody clipped."""
    from clipvault import twitch
    from clipvault.discover import vods
    from clipvault.twitch import chat_replay

    tool = chat_replay.find_tool(cfg.home_dir())
    if tool is None:
        return ""  # optional tool not installed; `doctor` says how to add it
    if not conn.execute("SELECT 1 FROM streamers WHERE vod_moments > 0").fetchone():
        return ""
    try:
        client = twitch.build_client()
    except twitch.MissingCredentials as exc:
        raise StepFailed(str(exc)) from exc
    try:
        found = vods.find_vods(conn, client, now=now)
        done = vods.process_vods(conn, config, tool, client=client, limit=VODS_PER_ROUND, now=now)
    finally:
        client.close()
    if done.moments:
        activity.event("found", f"{done.moments} chat moment{'s' if done.moments != 1 else ''} "
                       "found in finished streams")  # fmt: skip
    if not (found or done.processed or done.skipped or done.failed):
        return ""
    return (
        f"{found} new VOD(s); searched {done.processed}, {done.moments} moment(s) found, "
        f"{done.skipped} skipped, {done.failed} failed"
    )


def step_cleanup(conn: sqlite3.Connection, config: cfg.Config, now: datetime) -> str:
    """Retention, hand-deleted files, the disk guard and a database backup (daily 04:00)."""
    from clipvault.library import retention, storage

    library = storage.open_library(conn, config)
    if not library.available:
        return "library folder away; nothing done"
    moved = storage.relocate_cache(library)
    missing = retention.mark_missing_files(conn, library)
    removed = retention.apply_retention(
        conn, library, retention.plan_retention(conn, config, library)
    )
    retention.check_disk(conn, config, library)
    backup = backup_database(conn, library.root, now, ZoneInfo(config.timezone))
    return f"moved {moved}, marked {missing} missing, removed {removed}, backup {backup.name}"


def backup_database(
    conn: sqlite3.Connection, library_root: Path, now: datetime, tz: ZoneInfo
) -> Path:
    """Copy the database into the library folder, one file per weekday.

    The seven files are overwritten in turn, so backups never need deleting
    (hard rule 8), and they live on the library drive, away from the original.
    """
    folder = library_root / BACKUP_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"clipvault-{now.astimezone(tz).strftime('%a').lower()}.db"
    handle, temp_name = tempfile.mkstemp(prefix=".backup-", suffix=".db", dir=folder)
    os.close(handle)
    try:
        target = sqlite3.connect(temp_name)
        try:
            conn.backup(target)
        finally:
            target.close()
        os.replace(temp_name, dest)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return dest


def summary_text(conn: sqlite3.Connection, now: datetime) -> str:
    """The daily summary: what happened in the last 24 hours and what needs the user."""
    since = to_rfc3339(now - timedelta(hours=24))
    saved, size = conn.execute(
        "SELECT count(*), coalesce(sum(file_size), 0) FROM clips WHERE fetched_at >= ?", (since,)
    ).fetchone()
    dead = conn.execute(
        "SELECT count(*) FROM clips WHERE state = 'dead' AND updated_at >= ?", (since,)
    ).fetchone()[0]
    waiting = conn.execute(
        "SELECT count(*) FROM clips WHERE state = 'found' AND removed_at IS NULL"
    ).fetchone()[0]
    total, total_size = conn.execute(
        "SELECT count(*), coalesce(sum(file_size), 0) FROM clips "
        "WHERE file_path IS NOT NULL AND removed_at IS NULL AND state NOT IN ('found', 'dead')"
    ).fetchone()
    lines = [
        f"Last 24 h: {saved} clip(s) saved ({size / 1e6:,.0f} MB).",
        f"Library: {total} clip(s), {total_size / 1e9:,.1f} GB.",
    ]
    if waiting:
        lines.append(f"{waiting} clip(s) waiting to download.")
    if dead:
        lines.append(f"{dead} clip(s) could not be saved after every retry.")
    open_ = alerts.open_alerts(conn)
    if open_:
        lines.append(f"{len(open_)} problem(s) need you:")
        lines += [f"- {row['message']}" for row in open_[:5]]
    if alerts.is_paused(conn):
        lines.append("ClipVault is paused.")
    return "\n".join(lines)


def step_summary(conn: sqlite3.Connection, config: cfg.Config, now: datetime) -> str:
    """Queue the daily summary notification."""
    text = summary_text(conn, now)
    queued = ntfy.queue(conn, config, "summary", "ClipVault daily summary", text, priority=2,
                        tags=("clipboard",), now=now)  # fmt: skip
    return "summary queued" if queued else "notifications not set up"


def step_notify(conn: sqlite3.Connection, config: cfg.Config, now: datetime) -> str:
    """Turn new alerts into notifications and send whatever is due."""
    ntfy.queue_new_alerts(conn, config, now=now)
    result = ntfy.flush(conn, config, now=now)
    # Being offline is not "broken": the messages simply wait in the outbox.
    if not (result.sent or result.failed or result.dropped):
        return ""
    return f"sent {result.sent}, {result.failed} waiting for a connection, {result.dropped} dropped"


def step_updates(conn: sqlite3.Connection, config: cfg.Config, now: datetime) -> str:
    """Look for a newer ClipVault, announce it, and install it at a quiet moment."""
    from clipvault import updates

    return updates.step_updates(conn, config, now)


STEP_FUNCTIONS: dict[str, Step] = {
    "discover": step_discover,
    "worker": step_worker,
    "vods": step_vods,
    "cleanup": step_cleanup,
    "summary": step_summary,
    "notify": step_notify,
    "updates": step_updates,
}
