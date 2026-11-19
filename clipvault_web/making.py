"""Making one run: find the moments, write their headlines, render the phone versions.

Every step is recorded in the database before the next starts, so a crash or restart
picks up where it stopped (hard rule 7): moments already found are not looked for
again, finished clips are not made again, and a clip that was half made is made again
from scratch (its file is only ever whole: ``engine.make_vertical``).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from clipvault import db
from clipvault.logging_setup import ctx
from clipvault.process import headlines, transcribe
from clipvault.process.headlines import ClipSignals, Headline
from clipvault.process.transcribe import Transcriber, Word
from clipvault.render import engine, facecam, ffmpeg, layouts
from clipvault.render.ass import layout_headline
from clipvault.render.ffmpeg import RenderError
from clipvault.render.layouts import Rect
from clipvault.render.template import Look, load_look
from clipvault.timeutil import to_rfc3339, utc_now

from . import events, store
from . import moments as finder
from .settings import WebSettings

WATERMARK = "Made with ClipVault"
NO_NAME = "Bro"  # "Bro was NOT ready for this": when nobody says who is in the video
HEADLINE_OPTIONS = 4
CAM_FRAMES = 12  # frames sampled across the whole video to find a steady face cam
FACE_FRAMES = 4  # frames sampled in each moment to check the face is in the cam
PORTRAIT = 1.2  # height/width above this: already vertical
_CHAT = re.compile(r"\bchat\b", re.I)
_local: transcribe.LocalTranscriber | None = None

log = logging.getLogger(__name__)


class RunFailed(Exception):
    """The run can't be made; the message is shown to the visitor."""


def providers(settings: WebSettings) -> list[Transcriber]:
    """Groq when the website has its own key, always the local model as the fallback."""
    global _local
    found: list[Transcriber] = []
    if settings.groq_api_key:
        import httpx

        found.append(transcribe.GroqTranscriber(settings.groq_api_key, httpx.Client(timeout=120)))
    if _local is None:
        _local = transcribe.LocalTranscriber()  # the model loads once, on first use
    found.append(_local)
    return found


def web_look() -> Look:
    """The app's default look, with the small "Made with ClipVault" line under the
    headline (it goes through the same safe-box check as all text)."""
    look = load_look()
    look["credit"] = {**look["credit"], "enabled": True, "text": WATERMARK}
    return look


# --- headlines ------------------------------------------------------------------------------


def headline_options(
    text: str,
    language: str | None,
    name: str | None,
    look: Look,
    *,
    clip_key: str,
    used_patterns: tuple[str, ...] = (),
    used_texts: tuple[str, ...] = (),
) -> list[Headline]:
    """Hook headlines for a moment of an upload, best first (the app's built-in writer).

    Without a name the headlines say "Bro"; headlines about chat are left out (an
    upload has no chat).
    """
    signals = ClipSignals(
        clip_id=clip_key, login="", name=headlines.spoken_name(name) if name else NO_NAME,
        source="upload", text=text, language=language, recent=used_patterns,
    )  # fmt: skip
    mask = transcribe.mask_pattern(transcribe.load_mask_words())

    def size_of(candidate: str) -> float | None:
        shape = layout_headline(candidate, look)
        return shape.size if shape.fits else None

    usable = [h for h in headlines.offline_candidates(signals) if not _CHAT.search(h.text)]
    ranked = headlines.choose(usable, signals, size_of, blocked=mask, exclude=used_texts)
    return ranked[:HEADLINE_OPTIONS] or [
        Headline("Wait for the ENDING", "ENDING", "hook", "hook:0")
    ]


# --- layout ---------------------------------------------------------------------------------


def find_cam(source: Path, info: ffmpeg.MediaInfo, look: Look) -> Rect | None:
    """A steady face cam in a landscape video (a stream with a webcam), or None."""
    if info.height > info.width * PORTRAIT:
        return None
    try:
        frames = facecam.load_frames(source, CAM_FRAMES)
        cam = facecam.find_cam_rect(frames, [facecam.detect_faces(f) for f in frames])
    except (facecam.FacecamUnavailable, RenderError) as exc:
        log.warning("face cam check skipped: %s", exc, extra=ctx(job="web"))
        return None
    return cam if cam is not None and cam.w >= look.split.min_cam_width else None


def face_in_cam(
    source: Path, cam: Rect, start: float, end: float
) -> tuple[bool, tuple[float, float] | None]:
    """Is the face inside the cam during this moment? (``facecam.check_clip`` for a
    stretch of a long video.)"""
    import statistics
    import tempfile

    try:
        import cv2
        import numpy as np
    except ImportError:
        return False, None
    centres: list[tuple[float, float]] = []
    seen = 0
    with tempfile.TemporaryDirectory(prefix="clipvault-web-face-") as folder:
        for i in range(FACE_FRAMES):
            second = start + (end - start) * (i + 1) / (FACE_FRAMES + 1)
            target = Path(folder) / f"{i}.png"
            try:
                ffmpeg.extract_frame(source, second, target)
            except RenderError:
                continue
            image = cv2.imdecode(np.frombuffer(target.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                continue
            seen += 1
            height, width = image.shape[:2]
            faces = facecam.detect_faces(image)
            if faces and facecam.is_big_centred(faces[0], width, height):
                return False, None
            inside = [f for f in faces if cam.contains(f[0] + f[2] / 2, f[1] + f[3] / 2)]
            if inside:
                x, y, w, h = inside[0]
                centres.append((x + w / 2, y + h / 2))
    if not centres or len(centres) * 2 <= seen:
        return False, None
    return True, (statistics.median(c[0] for c in centres),
                  statistics.median(c[1] for c in centres))  # fmt: skip


def plan_for(
    clip: sqlite3.Row, source: Path, info: ffmpeg.MediaInfo, cam: Rect | None, look: Look
) -> layouts.Plan:
    """The layout of one clip: already vertical → fill the frame; a face cam with the face
    in it → split; otherwise the whole picture letterboxed."""
    if info.height > info.width * PORTRAIT:
        return layouts.native(look)
    if cam is not None:
        ok, face = face_in_cam(source, cam, clip["start"], clip["end"])
        if ok:
            return layouts.split(info, cam, None, face, look)
    return layouts.letterbox(info, look)


# --- the run -----------------------------------------------------------------------------------


def make_run(
    settings: WebSettings,
    run_id: str,
    *,
    transcribers: list[Transcriber] | None = None,
    find: Callable[..., tuple[list[finder.Moment], int]] = finder.find_moments,
    render: Callable[..., Any] | None = None,
) -> str:
    """Make (or finish making) one run; returns its final status (done or failed)."""
    conn = store.open_store(settings)
    try:
        run = store.get_run(conn, run_id)
        if run is None:
            raise RunFailed("This run doesn't exist.")
        log_ctx = ctx(job="web", run=run_id)
        source = store.resolve(settings, run["source_path"])
        if source is None:
            raise RunFailed("The video is no longer here. Please upload it again.")
        info = ffmpeg.probe(source)
        look = web_look()
        if not store.clips(conn, run_id):
            _find(conn, settings, run, source, info, look, transcribers, find)
        conn.execute("UPDATE runs SET status = 'making' WHERE id = ?", (run_id,))
        _render_all(settings, run_id, source, info, look, render or render_clip)
        rows = store.clips(conn, run_id)
        made = sum(1 for r in rows if r["status"] == "done")
        status = "done" if made else "failed"
        with db.transaction(conn):
            conn.execute(
                "UPDATE runs SET status = ?, step = ?, step_done = ?, step_total = ?, "
                "finished_at = ?, source_path = NULL, error = ? WHERE id = ?",
                (status, "Ready" if made else "Failed", made, len(rows),
                 to_rfc3339(utc_now()), None if made else "None of the clips could be made.",
                 run_id),
            )  # fmt: skip
        source.unlink(missing_ok=True)  # the upload is only needed while making the clips
        events.count(conn, "run_finished", status)
        log.info("run finished", extra={**log_ctx, "status": status, "clips": made})
        return status
    except RunFailed as exc:
        _fail(conn, run_id, str(exc))
        events.count(conn, "run_finished", "failed")
        return "failed"
    finally:
        conn.close()


def _find(conn, settings, run, source, info, look, transcribers, find) -> None:
    """Find the moments and write their headlines; record them all in one transaction."""
    run_id = run["id"]
    conn.execute("UPDATE runs SET status = 'finding' WHERE id = ?", (run_id,))

    def progress(step: str, done: int, total: int) -> None:
        store.set_step(conn, run_id, step, done, total)

    started = time.monotonic()
    found, more = find(source, info.duration, settings.clips_per_run,
                       transcribers if transcribers is not None else providers(settings),
                       progress)  # fmt: skip
    if not found:
        raise RunFailed("We couldn't find a moment to clip in this video.")
    progress("Choosing the layout", 0, 0)
    cam = find_cam(source, info, look)
    used_patterns: list[str] = []
    used_texts: list[str] = []
    rows = []
    for n, moment in enumerate(found, start=1):
        options = headline_options(
            moment.text, moment.language, run["name"], look, clip_key=f"{run_id}:{n}",
            used_patterns=tuple(used_patterns), used_texts=tuple(used_texts),
        )  # fmt: skip
        best = options[0]
        used_patterns.insert(0, best.pattern)
        used_texts.append(best.text)
        rows.append((
            run_id, n, moment.start, moment.end, moment.score, ", ".join(moment.reasons),
            moment.text, json.dumps([[w.text, w.start, w.end] for w in moment.words]),
            moment.language, best.text, best.key_word,
            json.dumps([h.text for h in options[1:]]),
        ))  # fmt: skip
    with db.transaction(conn):
        conn.executemany(
            "INSERT INTO run_clips (run_id, n, start, end, score, reasons, text, words_json, "
            "language, headline, key_word, headline_options, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting')",
            rows,
        )
        conn.execute(
            "UPDATE runs SET more_found = ?, cam_rect = ?, status = 'making', step = ?, "
            "step_done = 0, step_total = ? WHERE id = ?",
            (more, cam.to_json() if cam else None, "Making the phone versions", len(rows), run_id),
        )
    log.info("moments found", extra=ctx(job="web", run=run_id, clips=len(rows), more=more,
                                        seconds=round(time.monotonic() - started, 1)))  # fmt: skip


def _render_all(settings, run_id, source, info, look, render) -> None:
    """Make every clip not made yet, several at once."""
    conn = store.open_store(settings)
    try:
        pending = [r["n"] for r in store.clips(conn, run_id) if r["status"] != "done"]
    finally:
        conn.close()
    if not pending:
        return

    def one(n: int) -> None:
        c = store.open_store(settings)  # one connection per thread
        try:
            render(c, settings, run_id, n, source, info, look)
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=min(settings.render_threads, len(pending))) as pool:
        list(pool.map(one, pending))


def render_clip(
    conn: sqlite3.Connection,
    settings: WebSettings,
    run_id: str,
    n: int,
    source: Path,
    info: ffmpeg.MediaInfo,
    look: Look,
) -> None:
    """Make clip ``n`` of a run and record the result (a failure is recorded, not raised)."""
    clip = conn.execute(
        "SELECT * FROM run_clips WHERE run_id = ? AND n = ?", (run_id, n)
    ).fetchone()
    run = store.get_run(conn, run_id)
    folder = store.run_dir(settings, run_id)
    conn.execute(
        "UPDATE run_clips SET status = 'making', started_at = ? WHERE run_id = ? AND n = ?",
        (time.time(), run_id, n),
    )
    started = time.monotonic()
    try:
        cam = Rect.from_json(run["cam_rect"])
        plan = plan_for(clip, source, info, cam, look)
        words = tuple(Word(t, s, e) for t, s, e in json.loads(clip["words_json"] or "[]"))
        mask = transcribe.mask_pattern(transcribe.load_mask_words())
        fake_row = {"id": f"{run_id}:{n}", "title": "", "game_name": ""}
        headline, emojis = engine.hook_emojis_for(fake_row, clip["headline"], clip["text"] or "",
                                                  look, False, mask, ask=False)  # fmt: skip
        dest = folder / f"clip{n}.mp4"
        report = engine.make_vertical(
            source=source, info=info, plan=plan, dest=dest, start=clip["start"],
            duration=clip["end"] - clip["start"], look=look, headline=headline,
            key_word=clip["key_word"], words=words, login="", emojis=emojis, mask=mask,
            transcript_text=clip["text"] or "", still_path=folder / f"qa{n}.jpg",
        )  # fmt: skip
        thumb = folder / f"clip{n}.jpg"
        try:
            ffmpeg.extract_frame(dest, min(float(look.export.cover_second), 2.0), thumb)
        except RenderError:
            thumb = None
        conn.execute(
            "UPDATE run_clips SET status = 'done', layout = ?, path = ?, thumb_path = ?, "
            "qa_passed = ?, qa_report_json = ?, seconds = ?, error = NULL "
            "WHERE run_id = ? AND n = ?",
            (plan.layout, store.relpath(settings, dest),
             store.relpath(settings, thumb) if thumb and thumb.is_file() else None,
             int(report.passed), report.to_json(), round(time.monotonic() - started, 1),
             run_id, n),
        )  # fmt: skip
        log.info("web clip made", extra=ctx(job="web", run=run_id, clip=n, layout=plan.layout,
                                            qa_passed=report.passed))  # fmt: skip
    except (RenderError, OSError, ValueError) as exc:
        conn.execute("UPDATE run_clips SET status = 'failed', error = ? WHERE run_id = ? AND n = ?",
                     (str(exc)[:300], run_id, n))  # fmt: skip
        log.error("web clip failed: %s", exc, extra=ctx(job="web", run=run_id, clip=n))
    finally:
        done = conn.execute(
            "SELECT count(*) FROM run_clips WHERE run_id = ? AND status IN ('done', 'failed')",
            (run_id,),
        ).fetchone()[0]
        conn.execute("UPDATE runs SET step_done = ? WHERE id = ?", (done, run_id))


def _fail(conn: sqlite3.Connection, run_id: str, message: str) -> None:
    conn.execute(
        "UPDATE runs SET status = 'failed', error = ?, step = 'Failed', finished_at = ? "
        "WHERE id = ?",
        (message, to_rfc3339(utc_now()), run_id),
    )
    log.warning("run failed: %s", message, extra=ctx(job="web", run=run_id))
