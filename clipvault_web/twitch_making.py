"""Making a run from a Twitch link: save each moment, caption it, then hand it to rendering.

Downloads (plan section 5):
- **"My channel"** (a streamer signed in with ``channel:manage:clips``): Twitch's official
  Get Clips Download, always allowed; the token is deleted when the run ends.
- **Any other channel**: yt-dlp on the server, only while the download switch is on
  (``store.downloads_on``; OFF unless turned on on purpose). When it is off, or it goes off
  during the run, the moments are shown with Twitch's own player instead ("embed") and
  nothing is downloaded. Opted-out channels never get this far.

Only the moments are ever downloaded (a clip, or a short section of a VOD), and the
downloads are deleted as soon as the phone versions are made.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import statistics
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from clipvault import db
from clipvault.fetch import ytdlp
from clipvault.fetch.ytdlp import DownloadError
from clipvault.logging_setup import ctx
from clipvault.process import transcribe
from clipvault.process.headlines import ClipSignals
from clipvault.process.transcribe import Transcriber
from clipvault.render import facecam, ffmpeg
from clipvault.render.ffmpeg import RenderError
from clipvault.render.template import Look
from clipvault.timeutil import to_rfc3339, utc_now

from . import moments, store, twitch_web
from .making import RunFailed, choose_headlines
from .settings import WebSettings
from .twitch_web import Plan

QUALITY = "720p"
SECTION_PAD_S = 2.0
CHAT_MAX_VOD_S = 4 * 3600  # longer streams take too long to read the chat of
FETCHING = ("waiting", "fetching", "listening")

log = logging.getLogger(__name__)
Fetch = Callable[[sqlite3.Row, sqlite3.Row, Path], Path]


# --- starting a run ---------------------------------------------------------------------------


def create_run(
    conn: sqlite3.Connection,
    settings: WebSettings,
    plan: Plan,
    *,
    run_id: str,
    source_url: str,
    visitor: str | None,
    embed_only: bool,
    grant: tuple[str, str] | None = None,
) -> None:
    """Record a Twitch run and its moments. With ``embed_only`` (downloads off) the run is
    finished at once: Twitch's own players are shown, nothing is downloaded."""
    now = utc_now()
    status = "done" if embed_only else "queued"
    from datetime import timedelta

    with db.transaction(conn):
        conn.execute(
            "INSERT INTO runs (id, kind, status, source_name, source_url, duration, name, visitor, "
            "step, twitch_user_id, twitch_login, twitch_name, is_live, embed_only, more_found, "
            "created_at, finished_at, expires_at, median_views) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, plan.kind, status, plan.title, source_url, plan.vod_length, plan.name,
             visitor, "Ready" if embed_only else "Waiting for its turn", plan.user_id,
             plan.login, plan.name, int(plan.is_live), int(embed_only), plan.more_found,
             to_rfc3339(now), to_rfc3339(now) if embed_only else None,
             to_rfc3339(now + timedelta(hours=settings.keep_hours)),
             plan.median_views or next((i.median for i in plan.items if i.median), None)),
        )  # fmt: skip
        for n, item in enumerate(plan.items, start=1):
            _insert_item(conn, run_id, n, item, "embed" if embed_only else "waiting",
                         plan.vod_url)  # fmt: skip
        if grant and not embed_only:
            conn.execute("INSERT INTO grants (run_id, editor_id, access_token) VALUES (?, ?, ?)",
                         (run_id, grant[0], grant[1]))  # fmt: skip


def _insert_item(conn, run_id: str, n: int, item: twitch_web.Item, status: str,
                 vod_url: str | None) -> None:  # fmt: skip
    section = item.source_kind == "section"
    conn.execute(
        "INSERT INTO run_clips (run_id, n, start, end, score, source_kind, source_url, clip_slug, "
        "title, views, vod_id, embed_url, vod_start, vod_end, status) "
        "VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, n, item.duration or (item.end - item.start), float(item.views or 0),
         item.source_kind, vod_url if section else item.url, item.slug, item.title, item.views,
         item.vod_id, item.embed_url, item.start if section else None,
         item.end if section else None, status),
    )  # fmt: skip


# --- making it ----------------------------------------------------------------------------------


def prepare(
    conn: sqlite3.Connection,
    settings: WebSettings,
    run: sqlite3.Row,
    look: Look,
    transcribers: list[Transcriber],
    *,
    fetch: Fetch | None = None,
    find_chat: Callable[..., list[tuple[float, float]]] | None = None,
) -> Callable[[sqlite3.Row], tuple[Path, ffmpeg.MediaInfo] | None]:
    """Save and caption every moment not done yet, write headlines and find the face cam;
    returns where each clip's video is, for rendering."""
    run_id = run["id"]
    conn.execute("UPDATE runs SET status = 'finding' WHERE id = ?", (run_id,))
    if run["kind"] == "twitch_vod":
        _more_from_chat(conn, settings, run, find_chat)
    rows = store.clips(conn, run_id)
    if not rows:
        raise RunFailed("Nobody has clipped this stream yet, so there's nothing to make.")
    grant = conn.execute("SELECT * FROM grants WHERE run_id = ?", (run_id,)).fetchone()
    pending = [r["n"] for r in rows if r["status"] in FETCHING]
    if pending and grant is None and not store.downloads_on(conn, settings):
        _to_embeds(conn, run_id)
        pending = []
    store.set_step(conn, run_id, "Saving the moments", 0, len(pending))
    if pending:

        def one(n: int) -> None:
            c = store.open_store(settings)
            try:
                save_and_listen(c, settings, run_id, n, grant, transcribers, fetch)
            finally:
                c.close()

        with ThreadPoolExecutor(max_workers=min(settings.render_threads, len(pending))) as pool:
            list(pool.map(one, pending))
    store.set_step(conn, run_id, "Writing the headlines", 0, 0)
    write_headlines(conn, run_id, look)
    store.set_step(conn, run_id, "Choosing the layout", 0, 0)
    _find_cam(conn, settings, run_id, look)
    ready = [r for r in store.clips(conn, run_id) if r["status"] in ("ready", "making")]
    store.set_step(conn, run_id, "Making the phone versions", 0, len(ready))

    def source_for(row: sqlite3.Row) -> tuple[Path, ffmpeg.MediaInfo] | None:
        path = store.resolve(settings, row["source_path"])
        if path is None:
            return None
        try:
            return path, ffmpeg.probe(path)
        except RenderError:
            return None

    return source_for


def _to_embeds(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute(
        "UPDATE run_clips SET status = 'embed' WHERE run_id = ? "
        "AND status IN ('waiting', 'fetching', 'listening')",
        (run_id,),
    )


def save_and_listen(
    conn: sqlite3.Connection,
    settings: WebSettings,
    run_id: str,
    n: int,
    grant: sqlite3.Row | None,
    transcribers: list[Transcriber],
    fetch: Fetch | None = None,
) -> None:
    """Download one moment (if not yet) and transcribe it; failures are recorded per clip."""
    run = store.get_run(conn, run_id)
    row = conn.execute("SELECT * FROM run_clips WHERE run_id = ? AND n = ?", (run_id, n)).fetchone()
    folder = store.run_dir(settings, run_id)
    folder.mkdir(parents=True, exist_ok=True)
    log_ctx = ctx(job="web", run=run_id, clip=str(n), streamer=run["twitch_login"])
    path = store.resolve(settings, row["source_path"])
    if path is None:
        if grant is None and not store.downloads_on(conn, settings):
            conn.execute("UPDATE run_clips SET status = 'embed' WHERE run_id = ? AND n = ?",
                         (run_id, n))  # fmt: skip
            return
        _status(conn, run_id, n, "fetching")
        dest = folder / f"src{n}.mp4"
        try:
            if grant is not None:
                path = official_download(settings, run, row, grant, dest)
            else:
                path = (fetch or download)(run, row, dest)
        except (DownloadError, twitch_web.LinkProblem, httpx.HTTPError, OSError) as exc:
            if grant is None and store.note_download(conn, False):
                log.error("downloads failed again and again: switched to Twitch's own players",
                          extra=log_ctx)  # fmt: skip
            conn.execute(
                "UPDATE run_clips SET status = 'failed', error = ? WHERE run_id = ? AND n = ?",
                (f"Couldn't save this moment: {exc}"[:300], run_id, n),
            )
            log.warning("web download failed: %s", exc, extra=log_ctx)
            return
        if grant is None:
            store.note_download(conn, True)
        conn.execute("UPDATE run_clips SET source_path = ? WHERE run_id = ? AND n = ?",
                     (store.relpath(settings, path), run_id, n))  # fmt: skip
    if row["words_json"] is None:
        _status(conn, run_id, n, "listening")
        try:
            info = ffmpeg.probe(path)
        except RenderError as exc:
            conn.execute("UPDATE run_clips SET status = 'failed', error = ? "
                         "WHERE run_id = ? AND n = ?", (str(exc)[:300], run_id, n))  # fmt: skip
            return
        start = max(info.duration - moments.MAX_S, 0.0)  # a Short is at most 59 s: keep the end
        try:
            heard = moments.listen(path, 0.0, info.duration, transcribers)
        except transcribe.TranscribeError as exc:
            log.warning("no captions for a moment: %s", exc, extra=log_ctx)
            heard = None
        words = [w for w in (heard.words if heard else ()) if w.end > start]
        conn.execute(
            "UPDATE run_clips SET start = ?, end = ?, text = ?, language = ?, words_json = ?, "
            "status = 'ready' WHERE run_id = ? AND n = ?",
            (start, info.duration, " ".join(w.text for w in words),
             heard.language if heard else None,
             json.dumps([[w.text, round(w.start - start, 3), round(w.end - start, 3)]
                         for w in words]), run_id, n),
        )  # fmt: skip
    else:
        _status(conn, run_id, n, "ready")
    done = conn.execute(
        "SELECT count(*) FROM run_clips WHERE run_id = ? AND status NOT IN "
        "('waiting', 'fetching', 'listening')", (run_id,)).fetchone()[0]  # fmt: skip
    conn.execute("UPDATE runs SET step_done = ? WHERE id = ?", (done, run_id))


def _status(conn: sqlite3.Connection, run_id: str, n: int, status: str) -> None:
    conn.execute(
        "UPDATE run_clips SET status = ?, started_at = ? WHERE run_id = ? AND n = ?",
        (status, time.time(), run_id, n),
    )


def download(run: sqlite3.Row, row: sqlite3.Row, dest: Path) -> Path:
    """yt-dlp on the server: the clip, or only the moment's section of the VOD."""
    if row["source_kind"] == "section":
        return ytdlp.download_section(row["source_url"], dest, QUALITY, row["vod_start"],
                                      row["vod_end"])  # fmt: skip
    return ytdlp.download_clip(row["source_url"], dest, QUALITY)


def official_download(
    settings: WebSettings, run: sqlite3.Row, row: sqlite3.Row, grant: sqlite3.Row, dest: Path,
    http: httpx.Client | None = None,
) -> Path:  # fmt: skip
    """Twitch's own download of a signed-in streamer's clip (Get Clips Download)."""
    client = twitch_web.user_client(settings, grant["access_token"], http)
    links = client.get_clip_downloads(grant["editor_id"], run["twitch_user_id"], [row["clip_slug"]])
    url = (links.get(row["clip_slug"]) or {}).get("landscape_download_url")
    if not url:
        raise DownloadError("Twitch gave no download link for this clip", permanent=True)
    partial = dest.with_name(dest.name + ".part")
    getter = http or download_http()
    try:
        with getter.stream("GET", url) as response:
            response.raise_for_status()
            with partial.open("wb") as out:
                for chunk in response.iter_bytes(1 << 20):
                    out.write(chunk)
        if partial.stat().st_size == 0:
            raise DownloadError("Twitch's download was empty")
        os.replace(partial, dest)
    finally:
        partial.unlink(missing_ok=True)
        if http is None:
            getter.close()
    return dest


def download_http() -> httpx.Client:
    """The client Twitch's official download links are fetched with (tests replace it)."""
    return httpx.Client(timeout=120, follow_redirects=True)


def write_headlines(conn: sqlite3.Connection, run_id: str, look: Look) -> None:
    """Headlines for the moments that have none yet, one after another so no two clips of
    the run get the same one. The full writer: the viewer's title, views, reactions, chat."""
    run = store.get_run(conn, run_id)
    rows = store.clips(conn, run_id)
    views = [r["views"] for r in rows if r["views"]]
    median = run["median_views"] or (statistics.median(views) if views else None)
    used_texts = [r["headline"] for r in rows if r["headline"]]
    used_patterns: list[str] = []
    for row in rows:
        if row["status"] != "ready" or row["headline"]:
            continue
        signals = ClipSignals(
            clip_id=f"{run_id}:{row['n']}", login=run["twitch_login"] or "",
            name=run["twitch_name"] or run["twitch_login"] or "", title=row["title"] or "",
            source="viewer" if row["source_kind"] == "clip" else "vod_spike",
            views=row["views"], median_views=median, text=row["text"] or "",
            language=row["language"], recent=tuple(used_patterns),
        )  # fmt: skip
        options = choose_headlines(signals, look, tuple(used_texts))
        best = options[0]
        used_texts.append(best.text)
        used_patterns.insert(0, best.pattern)
        conn.execute(
            "UPDATE run_clips SET headline = ?, key_word = ?, headline_options = ? "
            "WHERE run_id = ? AND n = ?",
            (best.text, best.key_word, json.dumps([h.text for h in options[1:]]), run_id,
             row["n"]),
        )  # fmt: skip


def _find_cam(conn: sqlite3.Connection, settings: WebSettings, run_id: str, look: Look) -> None:
    """The streamer's face cam, found once from all the run's clips (as the app does from a
    streamer's recent clips). Stored as '' when there is none."""
    run = store.get_run(conn, run_id)
    if run["cam_rect"] is not None:
        return
    found = (store.resolve(settings, r["source_path"]) for r in store.clips(conn, run_id))
    videos = [p for p in found if p is not None]
    cam = None
    if len(videos) >= 2:
        try:
            cam = facecam.detect_streamer_cam(videos, run["twitch_login"] or "")
        except Exception as exc:  # noqa: BLE001 - the face cam is optional
            log.warning("face cam check skipped: %s", exc, extra=ctx(job="web", run=run_id))
    if cam is not None and cam.w < look.split.min_cam_width:
        cam = None
    conn.execute("UPDATE runs SET cam_rect = ? WHERE id = ?",
                 (cam.to_json() if cam else "", run_id))  # fmt: skip


def _more_from_chat(conn, settings: WebSettings, run: sqlite3.Row, find_chat) -> None:
    """A VOD with fewer viewer clips than wanted: add the moments where chat went wild
    (the app's chat-spike scoring), when the chat tool is installed. Done once."""
    run_id = run["id"]
    rows = store.clips(conn, run_id)
    want = settings.clips_per_run - len(rows)
    if want <= 0 or store.get_setting(conn, f"chat_read:{run_id}"):
        return
    store.set_setting(conn, f"chat_read:{run_id}", "1")
    store.set_step(conn, run_id, "Reading the stream's chat", 0, 0)
    covered = [(r["vod_start"] if r["vod_start"] is not None else 0.0, r["end"]) for r in rows]
    try:
        spans = (find_chat or _chat_spikes)(settings, run, want, covered)
    except Exception as exc:  # noqa: BLE001 - chat is a bonus; the viewer clips still get made
        log.warning("could not read the stream's chat: %s", exc, extra=ctx(job="web", run=run_id))
        return
    vod_url = run["source_url"]
    for i, (start, end) in enumerate(spans[:want], start=len(rows) + 1):
        item = twitch_web.Item("section", vod_url or "", title=run["source_name"] or "",
                               vod_id=None, start=max(start - SECTION_PAD_S, 0.0),
                               end=end + SECTION_PAD_S)  # fmt: skip
        _insert_item(conn, run_id, i, item, "waiting", vod_url)


def _chat_spikes(settings: WebSettings, run: sqlite3.Row, want: int,
                 covered: list[tuple[float, float]]) -> list[tuple[float, float]]:  # fmt: skip
    """Download the VOD's chat (TwitchDownloaderCLI) and find where it spiked."""
    from clipvault.discover import vod_spikes
    from clipvault.twitch import chat_replay

    tool = chat_replay.find_tool(settings.data)
    length = run["duration"] or 0
    if tool is None or not run["source_url"] or not length or length > CHAT_MAX_VOD_S:
        return []
    vod_id = run["source_url"].rstrip("/").rsplit("/", 1)[-1]
    chat_file = store.run_dir(settings, run["id"]) / "chat.json.gz"
    chat_replay.download_chat(tool, vod_id, chat_file, timeout=20 * 60)
    try:
        messages = chat_replay.load_chat(chat_file)
    finally:
        chat_file.unlink(missing_ok=True)
    picked = vod_spikes.pick_moments(messages, length_s=length, covered=covered, keep=want)
    return [(float(m.start), float(m.end)) for m in picked]


def finished_at() -> str:
    return to_rfc3339(utc_now())
