"""The website: upload a video, watch the clips being made, download them.

Pages: ``/`` (upload), ``/r/{id}`` (one run: progress, then the clips). The run id is
the private link: nothing lists runs, and every file of a run is deleted after
``keep_hours``.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from clipvault.render import ffmpeg
from clipvault.render.ffmpeg import RenderError

from . import events, store
from .settings import WebSettings
from .worker import Worker

HERE = Path(__file__).parent
VIDEO_TYPES = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
MIN_SECONDS = 15
CHUNK = 1024 * 1024
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def create_app(settings: WebSettings, *, start_worker: bool = True) -> FastAPI:
    """The website, with its background worker (tests pass ``start_worker=False``)."""
    for folder in (settings.runs_dir, settings.incoming_dir):
        folder.mkdir(parents=True, exist_ok=True)
    store.open_store(settings).close()  # migrate before the first request
    worker = Worker(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.stop()

    app = FastAPI(
        title="ClipVault Web", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.state.worker = worker
    salt = os.environ.get("CLIPVAULT_WEB_SALT") or secrets.token_hex(16)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")  # run links are private
        if request.url.path.startswith("/r/"):
            response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        return response

    def page(request: Request, name: str, status_code: int = 200, **values: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request, name, {"settings": settings, **values}, status_code=status_code
        )

    def visitor(request: Request) -> str:
        address = request.client.host if request.client else ""
        return hashlib.sha256(f"{salt}|{address}".encode()).hexdigest()[:16]

    # --- pages --------------------------------------------------------------------------

    def counted(name: str, detail: str = "") -> None:
        conn = store.open_store(settings)
        try:
            events.count(conn, name, detail)
        finally:
            conn.close()

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        counted("page_view", "home")
        return page(request, "try.html", max_mb=settings.max_upload_mb,
                    max_minutes=settings.max_minutes)  # fmt: skip

    @app.get("/r/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str):
        conn = store.open_store(settings)
        try:
            run = store.get_run(conn, run_id)
            if run is None:
                return page(request, "gone.html", 404, title="This link doesn't work",
                            text="Check that it was copied whole, or make new clips.")  # fmt: skip
            if run["status"] == "expired":
                return page(request, "gone.html", 410, title="These clips were deleted",
                            text=f"Clips are kept for {settings.keep_hours} hours. "
                                 "Upload the video again to make them again.")  # fmt: skip
            state = run_state(conn, run)
            events.count(conn, "page_view", "run")
        finally:
            conn.close()
        return page(request, "run.html", run=state)

    # --- the way to the app, and what pages report -----------------------------------------

    @app.get("/go/app")
    def go_app(source: str = Query("", alias="from")):
        """Every "get the app" link: counted (which nudge), then on to the download page."""
        where = source if source in events.LINKS else "other"
        counted("nudge_clicked", where)
        return RedirectResponse(settings.app_download_url, status_code=303)

    @app.post("/api/event")
    async def page_event(request: Request):
        """A page says it showed a nudge (navigator.sendBeacon). Only known names count."""
        try:
            body = await request.json()
        except ValueError:
            return Response(status_code=204)
        name, detail = str(body.get("name", "")), str(body.get("detail", ""))
        if name in events.FROM_PAGES and detail in events.NUDGES:
            await run_in_threadpool(counted, name, detail)
        return Response(status_code=204)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # --- uploading ----------------------------------------------------------------------

    @app.post("/api/upload")
    async def upload(request: Request):
        """The video arrives as the raw request body (so its size is checked as it streams);
        its name, the optional name of who is in it and the rights box come as headers."""
        if request.headers.get("x-rights") != "yes":
            return _error("Please confirm you have the right to use this video.", 400)
        name = unquote(request.headers.get("x-file-name", "")).strip()[:200] or "video.mp4"
        extension = Path(name).suffix.lower()
        if extension not in VIDEO_TYPES:
            return _error("Please upload an MP4, MOV, MKV or WebM video.", 415)
        size = int(request.headers.get("content-length") or 0)
        if size > settings.max_upload_bytes:
            return _error(f"That file is too big: the limit is {settings.max_upload_mb:,} MB.",
                          413)  # fmt: skip
        who = unquote(request.headers.get("x-who", "")).strip()
        who = re.sub(r"\s+", " ", _UNSAFE.sub("", who))[:40] or None

        run_id = store.new_run_id()
        part = settings.incoming_dir / f"{run_id}.part"
        received = 0
        try:
            with part.open("wb") as out:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > settings.max_upload_bytes:
                        raise _TooBig
                    out.write(chunk)
        except _TooBig:
            part.unlink(missing_ok=True)
            return _error(f"That file is too big: the limit is {settings.max_upload_mb:,} MB.",
                          413)  # fmt: skip
        except Exception:  # the visitor closed the tab or lost the connection
            part.unlink(missing_ok=True)
            raise
        if received == 0:
            part.unlink(missing_ok=True)
            return _error("The file arrived empty. Please try again.", 400)

        try:
            info = await run_in_threadpool(ffmpeg.probe, part)
        except RenderError:
            part.unlink(missing_ok=True)
            return _error("That doesn't look like a video we can read.", 415)
        problem = None
        if info.duration < MIN_SECONDS:
            problem = f"The video needs to be at least {MIN_SECONDS} seconds long."
        elif info.duration > settings.max_minutes * 60 + 1:
            problem = f"The video can be at most {settings.max_minutes} minutes long."
        elif not info.has_audio:
            problem = "The video has no sound, so there's nothing to find moments in."
        if problem:
            part.unlink(missing_ok=True)
            return _error(problem, 400)

        folder = store.run_dir(settings, run_id)
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / f"source{extension}"
        os.replace(part, source)
        conn = store.open_store(settings)
        try:
            store.create_run(conn, settings, run_id=run_id, kind="upload", source_name=name,
                             source=source, duration=info.duration, width=info.width,
                             height=info.height, name=who, visitor=visitor(request))  # fmt: skip
        finally:
            conn.close()
        worker.wake()
        await run_in_threadpool(counted, "run_started", "upload")
        return {"id": run_id, "url": f"/r/{run_id}"}

    # --- a run --------------------------------------------------------------------------

    @app.get("/api/runs/{run_id}")
    def run_status(run_id: str):
        conn = store.open_store(settings)
        try:
            run = store.get_run(conn, run_id)
            if run is None:
                return _error("No such run.", 404)
            return run_state(conn, run)
        finally:
            conn.close()

    def clip_file(run_id: str, n: int, column: str) -> tuple[Any, Path | None]:
        conn = store.open_store(settings)
        try:
            row = conn.execute(
                "SELECT c.*, r.status AS run_status FROM run_clips c "
                "JOIN runs r ON r.id = c.run_id WHERE c.run_id = ? AND c.n = ?",
                (run_id, n),
            ).fetchone() if store.RUN_ID.match(run_id) else None  # fmt: skip
        finally:
            conn.close()
        if row is None or row["run_status"] == "expired":
            return row, None
        return row, store.resolve(settings, row[column])

    @app.get("/r/{run_id}/clip/{n}.mp4")
    def clip_video(run_id: str, n: int, download: int = 0):
        row, path = clip_file(run_id, n, "path")
        if path is None:
            return Response("Not found", 404)
        if download:
            counted("clip_downloaded")
            return FileResponse(path, media_type="video/mp4",
                                filename=download_name(n, row["headline"]))  # fmt: skip
        return FileResponse(path, media_type="video/mp4")

    @app.get("/r/{run_id}/clip/{n}.jpg")
    def clip_thumb(run_id: str, n: int):
        _, path = clip_file(run_id, n, "thumb_path")
        if path is None:
            return Response("Not found", 404)
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/r/{run_id}/clips.zip")
    def all_clips(run_id: str):
        conn = store.open_store(settings)
        try:
            run = store.get_run(conn, run_id)
            rows = store.clips(conn, run_id) if run and run["status"] == "done" else []
        finally:
            conn.close()
        made = [(r, store.resolve(settings, r["path"])) for r in rows]
        made = [(r, p) for r, p in made if p is not None]
        if not made:
            return Response("Not found", 404)
        target = store.run_dir(settings, run_id) / "clips.zip"
        if not target.is_file():
            partial = target.with_suffix(".zip.part")
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_STORED) as archive:
                for row, path in made:
                    archive.write(path, download_name(row["n"], row["headline"]))
            os.replace(partial, target)
        counted("zip_downloaded")
        return FileResponse(target, media_type="application/zip", filename="ClipVault clips.zip")

    return app


class _TooBig(Exception):
    pass


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def download_name(n: int, headline: str | None) -> str:
    """'ClipVault 1 - Nobody was READY for this.mp4' (safe on every computer)."""
    words = re.sub(r"\s+", " ", _UNSAFE.sub("", headline or "")).strip(" .")[:70]
    return f"ClipVault {n}" + (f" - {words}" if words else "") + ".mp4"


# Expected seconds for each clip step (the cards' progress bars fill in about this time).
EXPECTED_S = {"fetching": 15, "listening": 12, "making": 35}
# The steps a card shows: found, saved, subtitles, phone version.
STEPS_DONE = {"waiting": 3, "fetching": 1, "listening": 2, "ready": 3, "making": 3,
              "done": 4, "failed": 0}  # fmt: skip


def run_state(conn, run) -> dict[str, Any]:
    """Everything the run page shows, as plain data (also the polling answer)."""
    rows = store.clips(conn, run["id"])
    base = f"/r/{quote(run['id'])}"
    clips = []
    for row in rows:
        made = row["status"] == "done" and row["path"]
        status = row["status"]
        clips.append({
            "n": row["n"],
            "status": status,
            "steps": STEPS_DONE.get(status, 0),
            "started": row["started_at"],
            "expected": EXPECTED_S.get(status),
            "headline": row["headline"],
            "seconds": round(row["end"] - row["start"]),
            "at": round(row["start"]),
            "video": f"{base}/clip/{row['n']}.mp4" if made else None,
            "thumb": f"{base}/clip/{row['n']}.jpg" if made and row["thumb_path"] else None,
            "download": f"{base}/clip/{row['n']}.mp4?download=1" if made else None,
        })  # fmt: skip
    finished = run["status"] in ("done", "failed")
    return {
        "id": run["id"],
        "now": time.time(),
        "status": run["status"],
        "source": run["source_name"],
        "step": run["step"],
        "step_done": run["step_done"],
        "step_total": run["step_total"],
        "ahead": store.queue_position(conn, run),
        "error": run["error"],
        "more_found": run["more_found"],
        "expires": store.expires_in_words(run),
        "clips": clips,
        "zip": f"{base}/clips.zip" if run["status"] == "done" else None,
        # One nudge toward the app per screen: while waiting, then after the clips are made.
        "nudge": None if run["status"] == "failed" else ("more" if finished else "working"),
    }
