"""Renders one clip into a phone-ready vertical video and checks it (SPEC §10, §11)."""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from clipvault import config as cfg
from clipvault import db
from clipvault.alerts import get_setting, set_setting
from clipvault.library import storage
from clipvault.logging_setup import ctx
from clipvault.process import transcribe
from clipvault.process.transcribe import Transcript, Word
from clipvault.render import facecam, ffmpeg, layouts, qa
from clipvault.render.ass import build_overlay, fit_headline
from clipvault.render.ffmpeg import RenderError
from clipvault.render.layouts import Rect
from clipvault.render.template import FONTS_DIR, Look, load_look
from clipvault.timeutil import to_rfc3339, utc_now

CAM_SAMPLE_CLIPS = 4
MISSES_BEFORE_RECHECK = 3
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RenderResult:
    """What a render produced."""

    render_id: int
    clip_id: str
    layout: str
    relpath: str
    path: Path
    headline: str | None
    report: qa.Report
    seconds: float


def default_headline(title: str | None, max_words: int = 7) -> str | None:
    """A stand-in headline from the clip title, until the headline writer (M8) supplies one.

    Emoji and symbols are dropped; a title with no real words left gives no headline.
    """
    plain = re.sub(r"[^\w\s'!?&-]", " ", title or "", flags=re.UNICODE)
    words = [w for w in plain.split() if re.search(r"[A-Za-z0-9]", w)]
    return " ".join(words[:max_words]) or None


def vertical_relpath(file_relpath: str) -> str:
    """Library path of a clip's vertical version (SPEC §13)."""
    folder, _, name = file_relpath.rpartition("/")
    return f"{folder}/vertical/{name.rsplit('.', 1)[0]}_9x16.mp4"


def ensure_cam(
    conn: sqlite3.Connection, library: storage.Library, streamer: sqlite3.Row, force: bool = False
) -> Rect | None:
    """Return the streamer's stored facecam rectangle, detecting it on first use."""
    if streamer["layout_checked_at"] and not force:
        return Rect.from_json(streamer["cam_rect"])
    rows = conn.execute(
        "SELECT file_path FROM clips WHERE streamer_id = ? AND file_path IS NOT NULL "
        "AND removed_at IS NULL AND state NOT IN ('found', 'rejected', 'dead') "
        "ORDER BY created_at DESC LIMIT ?",
        (streamer["id"], CAM_SAMPLE_CLIPS),
    ).fetchall()
    videos = [p for p in (storage.resolve(library, r["file_path"]) for r in rows) if p]
    if len(videos) < 2:
        return None  # not enough clips yet to tell a steady cam from a coincidence
    try:
        cam = facecam.detect_streamer_cam(videos, streamer["login"])
    except (facecam.FacecamUnavailable, RenderError) as exc:
        log.warning("facecam detection skipped: %s", exc, extra=ctx(streamer=streamer["login"]))
        return None
    conn.execute(
        "UPDATE streamers SET cam_rect = ?, content_rect = NULL, layout_checked_at = ? "
        "WHERE id = ?",
        (cam.to_json() if cam else None, to_rfc3339(utc_now()), streamer["id"]),
    )
    set_setting(conn, f"facecam.misses:{streamer['id']}", "0")
    return cam


def _note_face_result(conn: sqlite3.Connection, streamer_id: int, found: bool) -> None:
    """Count consecutive clips where the face wasn't in the cam; after three, detect again."""
    key = f"facecam.misses:{streamer_id}"
    misses = 0 if found else int(get_setting(conn, key, "0") or 0) + 1
    if misses >= MISSES_BEFORE_RECHECK:
        conn.execute("UPDATE streamers SET layout_checked_at = NULL WHERE id = ?", (streamer_id,))
        misses = 0
    set_setting(conn, key, str(misses))


def render_clip(
    conn: sqlite3.Connection,
    config: cfg.Config,
    clip_id: str,
    *,
    layout: str | None = None,
    headline: str | None = None,
    key_word: str | None = None,
    look: Look | None = None,
) -> RenderResult:
    """Render ``clip_id`` to ``.../vertical/..._9x16.mp4``, run QA and record the result.

    Rendering the same clip and layout again replaces the file and updates the
    same ``renders`` row, so nothing is duplicated. Raises ``RenderError`` when
    the clip can't be rendered at all.
    """
    look = look or load_look(config)
    library = storage.open_library(conn, config)
    clip = conn.execute(
        "SELECT c.*, s.login FROM clips c JOIN streamers s ON s.id = c.streamer_id WHERE c.id = ?",
        (clip_id,),
    ).fetchone()
    if clip is None:
        raise RenderError(f"no clip with id {clip_id}")
    streamer = conn.execute(
        "SELECT * FROM streamers WHERE id = ?", (clip["streamer_id"],)
    ).fetchone()
    video = storage.resolve(library, clip["file_path"]) if not clip["removed_at"] else None
    if video is None:
        raise RenderError("the clip's video file is not in the library")

    portrait = storage.resolve(library, clip["portrait_path"])
    info = ffmpeg.probe(video)
    cam, face = None, None
    face_ok = False
    wanted = layout or streamer["layout_override"]
    if portrait is None and wanted in (None, layouts.SPLIT):
        cam = ensure_cam(conn, library, streamer)
        if cam is not None:
            try:
                face_ok, face = facecam.check_clip(video, cam)
            except (facecam.FacecamUnavailable, RenderError):
                face_ok = False
            _note_face_result(conn, streamer["id"], face_ok)
    chosen = layouts.choose_layout(
        override=wanted, has_portrait=portrait is not None, cam=cam, face_in_cam=face_ok, look=look
    )
    if chosen == layouts.NATIVE:
        source, info = portrait, ffmpeg.probe(portrait)
        plan = layouts.native(look)
    elif chosen == layouts.SPLIT:
        source = video
        plan = layouts.split(info, cam, Rect.from_json(streamer["content_rect"]), face, look)
    else:
        source = video
        plan = layouts.letterbox(info, look)

    # Shorts may be at most 59 s: keep the end of a longer clip (SPEC §7).
    start = max(info.duration - look.export.max_seconds, 0.0)
    duration = info.duration - start
    transcript = _read_transcript(video)
    words = tuple(
        Word(w.text, w.start - start, w.end - start) for w in transcript.words if w.end > start
    )
    mask = transcribe.mask_pattern(transcribe.load_mask_words())
    if headline is None:
        headline = clip["headline"] or fit_headline(default_headline(clip["title"]), look)
    disclosure = None
    if streamer["permission_type"] == "campaign":
        disclosure = f"#ad Paid partnership with @{clip['login']}"
    overlay = build_overlay(
        look=look,
        layout=chosen,
        duration=duration,
        headline=headline,
        login=clip["login"],
        words=words,
        key_word=key_word,
        disclosure=disclosure,
        mask=mask,
    )

    relpath = vertical_relpath(clip["file_path"])
    dest = video.parent / "vertical" / relpath.rsplit("/", 1)[-1]
    dest.parent.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    with tempfile.TemporaryDirectory(prefix="clipvault-render-") as folder:
        work = Path(folder)
        (work / "overlay.ass").write_text(overlay.ass, encoding="utf-8")
        shutil.copytree(FONTS_DIR, work / "fonts", ignore=shutil.ignore_patterns("*.txt", ".*"))
        measured = ffmpeg.measure_loudness(source, start, duration) if info.has_audio else None
        partial = dest.with_name(dest.name + ".rendering.mp4")
        try:
            ffmpeg.encode(
                source=source,
                dest=partial,
                workdir=work,
                video_filter=plan.video_filter,
                audio_filter=ffmpeg.loudnorm_filter(measured, look),
                has_audio=info.has_audio,
                start=start,
                duration=duration,
                look=look,
            )
            os.replace(partial, dest)  # the finished name only ever holds a whole video
        finally:
            partial.unlink(missing_ok=True)

    still = cfg.home_dir() / "qa" / (dest.stem + ".jpg")
    report = qa.run_checks(
        output=dest,
        overlay=overlay,
        plan=plan,
        look=look,
        headline=headline,
        transcript_text=transcript.text,
        mask=mask,
        still_path=still,
    )
    seconds = (utc_now() - started).total_seconds()
    with db.transaction(conn):
        existing = conn.execute(
            "SELECT id FROM renders WHERE clip_id = ? AND layout = ?", (clip_id, chosen)
        ).fetchone()
        values = (relpath, headline, int(report.passed), report.to_json(), to_rfc3339(utc_now()))
        if existing:
            render_id = existing["id"]
            conn.execute(
                "UPDATE renders SET path = ?, headline = ?, qa_passed = ?, qa_report_json = ?, "
                "created_at = ? WHERE id = ?",
                (*values, render_id),
            )
        else:
            render_id = conn.execute(
                "INSERT INTO renders (clip_id, layout, path, headline, qa_passed, qa_report_json, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (clip_id, chosen, *values),
            ).lastrowid
    log.info(
        "clip rendered",
        extra=ctx(
            clip=clip_id,
            streamer=clip["login"],
            job="render",
            layout=chosen,
            qa_passed=report.passed,
            seconds=round(seconds, 1),
        ),
    )
    return RenderResult(render_id, clip_id, chosen, relpath, dest, headline, report, seconds)


def preview_clip(conn: sqlite3.Connection, layout: str) -> sqlite3.Row | None:
    """A recent saved clip that suits ``layout`` (for the Look editor), or None."""
    query = (
        "SELECT c.*, s.login, s.cam_rect, s.content_rect FROM clips c "
        "JOIN streamers s ON s.id = c.streamer_id WHERE c.file_path IS NOT NULL "
        "AND c.removed_at IS NULL AND c.state NOT IN ('found', 'rejected', 'dead') "
        "AND c.transcript_path IS NOT NULL AND c.duration >= 8"
    )
    order = "c.speech_wps DESC, c.created_at DESC"
    if layout == layouts.SPLIT:
        # Only a confirmed cam (a stored one that keeps missing the face is reset
        # to unchecked), and a clip already rendered in this layout if there is one.
        query += " AND s.cam_rect IS NOT NULL AND s.layout_checked_at IS NOT NULL"
        order = (
            "EXISTS (SELECT 1 FROM renders r WHERE r.clip_id = c.id AND r.layout = 'split') DESC, "
            + order
        )
    elif layout == layouts.NATIVE:
        query += " AND c.portrait_path IS NOT NULL"
    return conn.execute(f"{query} ORDER BY {order} LIMIT 1").fetchone()


def preview_frame(
    conn: sqlite3.Connection, config: cfg.Config, look: Look, layout: str, dest: Path
) -> Path:
    """Draw one frame of a recent clip in ``layout`` with the look's text, as a JPEG.

    Used by the Look editor: much faster than a full render, and nothing is saved
    in the library. Raises ``RenderError`` when no saved clip suits the layout.
    """
    clip = preview_clip(conn, layout)
    if clip is None:
        raise RenderError(f"no saved clip suits the {layout} layout yet")
    library = storage.open_library(conn, config)
    video = storage.resolve(
        library, clip["portrait_path" if layout == layouts.NATIVE else "file_path"]
    )
    if video is None:
        raise RenderError("the preview clip's file is not in the library")
    info = ffmpeg.probe(video)
    if layout == layouts.NATIVE:
        plan = layouts.native(look)
    elif layout == layouts.SPLIT:
        plan = layouts.split(info, Rect.from_json(clip["cam_rect"]),
                             Rect.from_json(clip["content_rect"]), None, look)  # fmt: skip
    else:
        plan = layouts.letterbox(info, look)
    original = storage.resolve(library, clip["file_path"])
    words = _read_transcript(original).words if original else ()
    headline = clip["headline"] or fit_headline(default_headline(clip["title"]), look)
    overlay = build_overlay(look=look, layout=layout, duration=info.duration, headline=headline,
                            login=clip["login"], words=words)  # fmt: skip
    # Show a moment with a caption on screen, a little into the first block.
    moment = min(overlay.blocks[0].start + 0.15, info.duration - 0.1) if overlay.blocks else 1.0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clipvault-preview-") as folder:
        work = Path(folder)
        (work / "overlay.ass").write_text(overlay.ass, encoding="utf-8")
        shutil.copytree(FONTS_DIR, work / "fonts", ignore=shutil.ignore_patterns("*.txt", ".*"))
        graph = f"[0:v]{plan.video_filter}[base];[base]subtitles=overlay.ass:fontsdir=fonts[v]"
        ffmpeg._ffmpeg(
            ["-y", "-loglevel", "error", "-i", str(video), "-ss", f"{max(moment, 0):.3f}",
             "-filter_complex", graph, "-map", "[v]", "-frames:v", "1", "-q:v", "3",
             str(dest.resolve())],
            cwd=work,
            what="look preview",
        )  # fmt: skip
    if not dest.is_file():
        raise RenderError("FFmpeg made no preview picture")
    return dest


def _read_transcript(video: Path) -> Transcript:
    """The clip's word timings, or an empty transcript when it has none yet."""
    words_file = video.with_suffix(".words.json")
    try:
        return Transcript.from_json(words_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return Transcript("", (), None, "none")
