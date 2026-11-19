"""Job definitions and clip state transitions (SPEC §3).

Each transition is one step. Steps are idempotent: they look at the database
and the file system first, so a crash at any point can simply be run again.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from clipvault import activity, db, lifecycle
from clipvault.alerts import get_setting, raise_alert, resolve_alerts, set_setting
from clipvault.config import Config
from clipvault.fetch import ytdlp
from clipvault.fetch.ytdlp import DownloadError
from clipvault.library import retention, storage
from clipvault.logging_setup import ctx
from clipvault.process import transcribe, write
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
SILENT_LUFS = -50.0  # quieter than this is treated as muted
# Moments cut from a stream recording rather than downloaded as a Twitch clip.
CUT_SOURCES = ("vod_spike", "live_spike")
# Queue order for downloads and subtitles: moments caught live first (someone may be
# watching them arrive), then the most viewed.
LIVE_FIRST = " ORDER BY (c.source = 'live_spike') DESC, c.view_count DESC, c.id"
DOWNLOADER_ALERT = "Clip downloads keep failing; the downloader (yt-dlp) may need an update."

FETCH_JOB = "fetch"
Downloader = Callable[[str, Path, str], Path]
SectionDownloader = Callable[[str, Path, str, float, float], Path]
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
    cut: SectionDownloader = ytdlp.download_section,
    is_silent: Callable[[Path], bool] | None = None,
    allow_vod_cuts: bool = True,
    on_progress: Callable[[str], None] | None = None,
) -> FetchSummary:
    """Download every clip in state ``found`` (most-viewed first) into the library.

    Safe to run again at any time: a clip whose file is already complete is
    simply marked ``fetched``, and nothing is downloaded twice. Moments found
    from chat (``vod_spike``) are cut from their VOD; they count as heavy work,
    so ``allow_vod_cuts=False`` (laptop on battery) leaves them waiting. A cut
    with no sound (Twitch mutes copyrighted music in VODs) is flagged ``muted``.
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
    pending = conn.execute(query + LIVE_FIRST, params).fetchall()
    due = [c for c in pending if not c["retry_at"] or parse_rfc3339(c["retry_at"]) <= now]
    if not allow_vod_cuts:
        due = [c for c in due if c["source"] not in CUT_SOURCES]
    summary.waiting = len(pending) - len(due)
    todo = due[:limit] if limit else due

    streak = int(get_setting(conn, _FAILURE_STREAK_SETTING, "0") or 0)
    run_streak = 0
    done = 0
    for index, clip in enumerate(todo, start=1):
        if lifecycle.stopping.is_set():
            break  # quitting: the rest waits for the next run
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
                clip["view_count"],
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
                is_cut = clip["source"] in CUT_SOURCES
                with activity.working(clip["id"], "cutting" if is_cut else "downloading",
                                      clip["login"]):  # fmt: skip
                    if is_cut:
                        start = clip["vod_offset"] or 0
                        end = start + (clip["duration"] or 0)
                        path = cut(clip["url"], library.write_base / relpath, config.quality,
                                   start, end)  # fmt: skip
                    else:
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
        if clip["source"] in CUT_SOURCES and (is_silent or _is_silent)(path):
            conn.execute("UPDATE clips SET reject_reason = 'muted' WHERE id = ?", (clip["id"],))
            log.info("VOD cut has no sound (muted by Twitch?)", extra=log_ctx)
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


def _is_silent(path: Path) -> bool:
    """True for a file with no audio or only near-silence (muted VOD stretch)."""
    from clipvault.render import ffmpeg

    values = ffmpeg.measure_loudness(path)
    try:
        return values is None or float(values["input_i"]) < SILENT_LUFS
    except (KeyError, ValueError):
        return True


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


# --- transcribe: fetched -> transcribed ------------------------------------------


@dataclass
class TranscribeSummary:
    """What one transcription run did."""

    done: int = 0
    already_there: int = 0
    no_speech: int = 0
    failed: int = 0
    words: int = 0
    waiting: int = 0
    remaining: int = 0
    stopped_on_failures: bool = False
    providers_used: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def run_transcribe(
    conn: sqlite3.Connection,
    config: Config,
    *,
    login: str | None = None,
    limit: int | None = None,
    now: datetime | None = None,
    providers: list[transcribe.Transcriber] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> TranscribeSummary:
    """Make a transcript for every saved clip that has none yet (most-viewed first).

    Writes ``<clip>.srt`` (subtitles, with masked words) and ``<clip>.words.json``
    (word timings, as spoken) next to the video and indexes the text for search.
    Safe to run again: a clip whose word file already exists is only recorded.
    """
    now = now or utc_now()
    say = on_progress or (lambda _line: None)
    summary = TranscribeSummary()
    library = storage.open_library(conn, config)
    if providers is None:
        providers = transcribe.build_providers(config)
    mask = transcribe.mask_pattern(transcribe.load_mask_words())

    query = (
        "SELECT c.*, s.login FROM clips c JOIN streamers s ON s.id = c.streamer_id "
        "WHERE c.state IN ('fetched', 'filtered') AND c.removed_at IS NULL "
        "AND c.file_path IS NOT NULL"
    )
    params: list[str] = []
    if login:
        query += " AND s.login = ?"
        params.append(login)
    pending = conn.execute(query + LIVE_FIRST, params).fetchall()
    due = [c for c in pending if not c["retry_at"] or parse_rfc3339(c["retry_at"]) <= now]
    summary.waiting = len(pending) - len(due)
    todo = due[:limit] if limit else due

    run_streak = 0
    done = 0
    for index, clip in enumerate(todo, start=1):
        if lifecycle.stopping.is_set():
            break  # quitting: the rest waits for the next run
        done = index
        log_ctx = ctx(clip=clip["id"], streamer=clip["login"], job="transcribe")
        video = storage.resolve(library, clip["file_path"])
        if video is None:
            continue  # the file is away or was deleted; cleanup deals with that
        words_file = video.with_suffix(".words.json")
        try:
            result = _load_transcript(words_file)
            if result is not None:
                summary.already_there += 1
            else:
                with activity.working(clip["id"], "subtitles", clip["login"]):
                    result = _transcribe_video(video, providers, config.transcription.language)
                storage.write_text_atomic(words_file, result.to_json())
                summary.done += 1
        except transcribe.TranscribeError as exc:
            state = record_failure(conn, clip["id"], str(exc), now)
            summary.failed += 1
            summary.errors.append(f"{clip['id']}: {exc}")
            log.error("transcription failed (%s): %s", state, exc, extra=log_ctx)
            say(f"[{index}/{len(todo)}] {clip['login']}: FAILED - {exc}")
            run_streak += 1
            if run_streak >= DOWNLOADER_ALERT_AFTER:
                summary.stopped_on_failures = True
                break
            continue

        run_streak = 0
        srt = transcribe.to_srt(transcribe.cues_from_words(result.words), mask)
        storage.write_text_atomic(video.with_suffix(".srt"), srt)
        seconds = clip["duration"] or (result.words[-1].end if result.words else 0.0)
        wps = round(len(result.words) / seconds, 3) if seconds else 0.0
        with db.transaction(conn):
            conn.execute(
                "UPDATE clips SET state = 'transcribed', transcript_path = ?, speech_wps = ?, "
                "language = ?, transcript_provider = ?, fail_count = 0, last_error = NULL, "
                "retry_at = NULL WHERE id = ?",
                (
                    clip["file_path"].rsplit(".", 1)[0] + ".srt",
                    wps,
                    result.language,
                    result.provider,
                    clip["id"],
                ),
            )
            conn.execute("DELETE FROM transcripts WHERE clip_id = ?", (clip["id"],))
            conn.execute(
                "INSERT INTO transcripts (clip_id, text) VALUES (?, ?)", (clip["id"], result.text)
            )
        summary.words += len(result.words)
        summary.no_speech += not result.words
        used = summary.providers_used
        used[result.provider] = used.get(result.provider, 0) + 1
        log.info(
            "clip transcribed",
            extra={**log_ctx, "words": len(result.words), "provider": result.provider},
        )
        say(
            f"[{index}/{len(todo)}] {clip['login']}: {len(result.words):3} words "
            f"({result.provider})  {result.text[:70]}"
        )

    summary.remaining = len(due) - done
    return summary


def _load_transcript(words_file: Path) -> transcribe.Transcript | None:
    """Read a transcript written by an earlier run; None if absent or unreadable."""
    if not words_file.is_file():
        return None
    try:
        return transcribe.Transcript.from_json(words_file.read_text(encoding="utf-8"))
    except (ValueError, KeyError, TypeError):
        return None


def _transcribe_video(
    video: Path, providers: list[transcribe.Transcriber], language: str | None
) -> transcribe.Transcript:
    with tempfile.TemporaryDirectory(prefix="clipvault-audio-") as folder:
        audio = Path(folder) / "audio.flac"
        try:
            transcribe.extract_audio(video, audio)
        except transcribe.NoAudio:
            return transcribe.Transcript("", (), None, "none")
        return transcribe.transcribe_audio(audio, providers, language)


# --- write: transcribed -> written (SPEC §9) ---------------------------------------

WRITE_JOB = "write"


@dataclass
class WriteSummary:
    """What one headline-writing run did."""

    written: int = 0
    rejected: int = 0
    unusable: int = 0  # no valid answer, clip kept (archive use)
    errors: list[str] = field(default_factory=list)
    examples: list[tuple[str, str, str]] = field(default_factory=list)  # (clip, login, headline)


def clip_facts(
    conn: sqlite3.Connection, library: storage.Library, clip: sqlite3.Row
) -> write.ClipFacts:
    """Everything the headline writer may use for one clip."""
    from clipvault.process import write

    words: tuple[transcribe.Word, ...] = ()
    video = storage.resolve(library, clip["file_path"])
    if video is not None:
        transcript = _load_transcript(video.with_suffix(".words.json"))
        if transcript is not None:
            words = transcript.words
    mask = transcribe.mask_pattern(transcribe.load_mask_words())
    lines = [
        (cue.start, transcribe.mask_text(cue.text, mask))
        for cue in transcribe.cues_from_words(words)
    ]
    streamer = conn.execute(
        "SELECT * FROM streamers WHERE id = ?", (clip["streamer_id"],)
    ).fetchone()
    disclosure = None
    if streamer["permission_type"] == "campaign":
        disclosure = f"#ad Paid partnership with @{streamer['login']}"
    return write.ClipFacts(
        login=streamer["login"],
        display_name=streamer["display_name"] or streamer["login"],
        title=clip["title"] or "",
        game=clip["game_name"] or "",
        transcript=lines,
        disclosure=disclosure,
    )


def write_for_clip(
    conn: sqlite3.Connection,
    config: Config,
    clip_id: str,
    provider: write.Provider,
    *,
    reject_on_failure: bool = False,
) -> write.Copy | None:
    """Write and store a clip's headline, caption and hashtags; None when no valid answer came.

    ``reject_on_failure`` (the posting pipeline) marks such a clip ``rejected``
    with reason ``headline``; for the private archive the clip simply keeps its
    stand-in headline. Raises ``write.LLMError`` when the model can't be reached.
    """
    from clipvault.process import write
    from clipvault.render.ass import layout_headline
    from clipvault.render.template import load_look

    clip = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if clip is None:
        raise KeyError(clip_id)
    library = storage.open_library(conn, config)
    facts = clip_facts(conn, library, clip)
    look = load_look(config)
    blocked = transcribe.mask_pattern(transcribe.load_mask_words())
    # Leave room for the emoji picture that is drawn next to the hook, so a hook that fits still fits with it.
    room = ("😂",) if look.hook_emoji.count > 0 else ()
    copy, problems = write.write_copy(
        facts, provider, blocked=blocked,
        fits=lambda text: layout_headline(text, look, room, look.hook_emoji.position).fits,
    )
    log_ctx = ctx(clip=clip_id, streamer=facts.login, job=WRITE_JOB)
    if copy is None:
        log.warning("no usable headline: %s", "; ".join(problems), extra=log_ctx)
        if reject_on_failure:
            conn.execute(
                "UPDATE clips SET state = 'rejected', reject_reason = 'headline', last_error = ? "
                "WHERE id = ?",
                ("; ".join(problems)[:1000], clip_id),
            )
        return None
    conn.execute(
        "UPDATE clips SET headline = ?, post_caption = ?, hashtags_json = ?, copy_provider = ?, "
        "state = CASE WHEN state = 'transcribed' THEN 'written' ELSE state END WHERE id = ?",
        (copy.headline, copy.post_caption, copy.hashtags_json(), copy.provider, clip_id),
    )
    log.info("headline written: %s", copy.headline, extra=log_ctx)
    return copy


def run_write(
    conn: sqlite3.Connection,
    config: Config,
    provider: write.Provider,
    *,
    login: str | None = None,
    limit: int = 20,
    reject_on_failure: bool = False,
) -> WriteSummary:
    """Write headlines for the best-scoring transcribed clips that have none yet."""
    from clipvault.process import candidates, write

    summary = WriteSummary()
    ranked = candidates.rank_candidates(conn, login=login, unrendered_only=False)
    have = {r[0] for r in conn.execute("SELECT id FROM clips WHERE headline IS NOT NULL")}
    states = {r[0]: r[1] for r in conn.execute("SELECT id, state FROM clips")}
    todo = [c for c in ranked if c.clip_id not in have and states.get(c.clip_id) == "transcribed"]
    for candidate in todo[:limit]:
        try:
            copy = write_for_clip(conn, config, candidate.clip_id, provider,
                                  reject_on_failure=reject_on_failure)  # fmt: skip
        except write.LLMError as exc:
            summary.errors.append(str(exc))
            break  # the service is down or refusing: no point trying the next clip
        if copy is None:
            if reject_on_failure:
                summary.rejected += 1
            else:
                summary.unusable += 1
            continue
        summary.written += 1
        summary.examples.append((candidate.clip_id, candidate.login, copy.headline))
    return summary


# --- render: phone versions of every new clip ----------------------------------------

RENDER_JOB = "render"


@dataclass
class RenderSummary:
    """What one round of automatic rendering did."""

    rendered: int = 0
    failed: int = 0
    remaining: int = 0
    errors: list[str] = field(default_factory=list)


def clips_to_render(conn: sqlite3.Connection, config: Config) -> list[str]:
    """Saved clips without a phone version yet, newest first.

    Clips wait for their transcript (the captions come from it) unless
    transcripts are switched off. A clip that failed to render is skipped.
    """
    states = ("'transcribed'", "'written'") if config.transcription.enabled else (
        "'fetched'", "'transcribed'", "'written'")  # fmt: skip
    rows = conn.execute(
        "SELECT c.id FROM clips c JOIN streamers s ON s.id = c.streamer_id "
        f"WHERE c.state IN ({', '.join(states)}) AND c.removed_at IS NULL "
        "AND c.file_path IS NOT NULL AND c.render_error IS NULL AND s.save_enabled = 1 "
        "AND NOT EXISTS (SELECT 1 FROM renders r WHERE r.clip_id = c.id AND r.path IS NOT NULL) "
        "ORDER BY c.created_at DESC"
    ).fetchall()
    return [row["id"] for row in rows]


def run_render(
    conn: sqlite3.Connection,
    config: Config,
    *,
    limit: int | None = None,
    render: Callable[..., object] | None = None,
) -> RenderSummary:
    """Make the phone version (best layout, headline, captions) of the newest clips."""
    from clipvault.render import engine
    from clipvault.render.ffmpeg import RenderError

    render = render or engine.render_clip
    summary = RenderSummary()
    todo = clips_to_render(conn, config)
    picked = todo[:limit] if limit else todo
    for clip_id in picked:
        if lifecycle.stopping.is_set():
            break  # quitting: the rest waits for the next run
        try:
            with activity.working(clip_id, "editing"):
                render(conn, config, clip_id)
            summary.rendered += 1
            activity.event("edited", "Phone version ready", clip_id=clip_id)
        except (RenderError, OSError) as exc:
            summary.failed += 1
            summary.errors.append(f"{clip_id}: {exc}")
            conn.execute(
                "UPDATE clips SET render_error = ? WHERE id = ?", (str(exc)[:500], clip_id)
            )
            log.error("phone version failed: %s", exc, extra=ctx(clip=clip_id, job=RENDER_JOB))
    summary.remaining = len(todo) - len(picked)
    return summary
