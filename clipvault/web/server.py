"""The control panel: a small FastAPI app with server-rendered pages (SPEC §15).

Pages are plain HTML forms that work without JavaScript (every action is a POST
followed by a redirect); ``static/app.js`` only adds small conveniences. Each
request opens its own database connection, like every other thread in the app.

Security (see ``auth.py``): every page except login and the static files needs
the login cookie; every POST needs the form token; the first password can only
be set from this computer itself.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from clipvault import __version__, alerts, db
from clipvault import config as cfg
from clipvault.library import storage
from clipvault.logging_setup import ctx
from clipvault.timeutil import to_rfc3339, utc_now
from clipvault.web import auth

HERE = Path(__file__).parent
PUBLIC_PATHS = ("/login", "/setup-password", "/static/", "/manifest.webmanifest", "/healthz",
                "/app/")  # fmt: skip
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
PAGE_SIZE = 24
LIVE_CACHE_S = 120
FAILED_LOGIN_DELAY_S = 1.0

log = logging.getLogger(__name__)


class PanelState:
    """Things the panel keeps between requests."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.rendering: set[str] = set()
        self.twitch_login: dict | None = None  # code being entered at twitch.tv/activate
        self.render_errors: dict[str, str] = {}
        self.executor = ThreadPoolExecutor(1, thread_name_prefix="panel-render")
        self.live: dict[str, str] = {}
        self.live_checked = 0.0
        self.thumb_locks: dict[str, threading.Lock] = {}
        self.locks_guard = threading.Lock()

    def thumb_lock(self, clip_id: str) -> threading.Lock:
        """One lock per clip: different thumbnails are made in parallel, one clip's only once."""
        with self.locks_guard:
            return self.thumb_locks.setdefault(clip_id, threading.Lock())


def _asset_version(folder: Path) -> str:
    """A short tag that changes whenever a stylesheet, script or font changes.

    Added to the links as ?v=, so browsers fetch the new files after an update
    instead of keeping a cached copy.
    """
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(folder).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def _update_info(c: sqlite3.Connection) -> dict[str, Any]:
    """What the Home card and Settings show about updates."""
    from clipvault import updates

    state = alerts.get_setting(c, updates.STATE) or ""
    return {
        "current": __version__,
        "available": updates.available(c),
        "can_install": updates.can_install(),
        "auto": updates.auto_install(c),
        "checks": updates.checks_on(c),
        "checked_at": alerts.get_setting(c, updates.CHECKED) or "",
        "error": alerts.get_setting(c, updates.ERROR) or "",
        "busy": state in ("downloading", "installing"),
        "state": state,
        "just_updated": updates.just_updated(c),
        "releases": updates.RELEASES_PAGE,
    }


def create_app(home: Path | None = None) -> FastAPI:
    """Build the control-panel app (the home folder decides which database it uses)."""
    home = home or cfg.home_dir()
    app = FastAPI(
        title="ClipVault",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(_check_form_token)],
    )
    app.state.panel = PanelState(home)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["urlpart"] = lambda value: quote(str(value), safe="")
    templates.env.filters["megabytes"] = lambda size: f"{(size or 0) / 1_048_576:,.1f} MB"
    templates.env.filters["clock"] = _clock
    templates.env.filters["highlight"] = _highlight
    templates.env.filters["ago"] = _ago
    templates.env.filters["when"] = lambda stamp: _when(stamp, config().timezone)
    templates.env.filters["compact"] = _compact
    templates.env.filters["fromjson"] = _fromjson
    templates.env.filters["gigabytes"] = lambda size: f"{(size or 0) / 1e9:,.0f} GB"
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    assets = _asset_version(HERE / "static")

    def conn() -> sqlite3.Connection:
        return db.open_db(home / cfg.DB_FILENAME)

    def config() -> cfg.Config:
        return cfg.load_config(home / cfg.CONFIG_FILENAME)

    def page(request: Request, name: str, **values: Any) -> HTMLResponse:
        session = request.cookies.get(auth.COOKIE)
        values.setdefault("tab", "")
        values["csrf"] = auth.csrf_token(session)
        values["flash"] = request.query_params.get("msg", "")
        values["version"] = __version__
        values["assets"] = assets
        with _db(conn) as c:
            if "problems" not in values:
                values["problems"] = len(alerts.open_alerts(c))
            values["update"] = _update_info(c)
        return templates.TemplateResponse(request, name, values)

    def back(url: str, message: str = "") -> RedirectResponse:
        if message:
            # The message goes before any "#section" part, which browsers never send.
            path, hash_mark, section = url.partition("#")
            path += ("&" if "?" in path else "?") + urlencode({"msg": message})
            url = path + hash_mark + section
        return RedirectResponse(url, status_code=303)

    # --- security ----------------------------------------------------------------

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith(PUBLIC_PATHS):
            return await call_next(request)
        if not auth.has_password():
            return RedirectResponse("/setup-password", status_code=303)
        if not auth.session_valid(request.cookies.get(auth.COOKIE)):
            return RedirectResponse("/login?next=" + quote(path), status_code=303)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "version": __version__}

    # --- the app window (clipvault/window.py) ----------------------------------------

    @app.get("/app/enter")
    def app_enter(request: Request, t: str = ""):
        """The ClipVault window signs itself in with its in-process token."""
        from clipvault import window

        if not _is_local(request) or not window.token_ok(t):
            return back("/login")
        if not auth.has_password():
            return back("/setup-password")  # the first password is still chosen once
        return _logged_in(back("/"))

    @app.get("/app/show")
    def app_show(request: Request) -> dict:
        """Opening ClipVault again opens its window, or brings the open one forward."""
        from clipvault import window

        if not _is_local(request):
            return {"shown": False}
        return {"shown": window.show()}

    @app.get("/app/window/poll")
    def app_window_poll(request: Request):
        """The window process checking in (it holds the token the app gave it)."""
        from clipvault import window

        manager = window.manager()
        count = None
        if manager is not None and _is_local(request):
            count = manager.poll(request.headers.get(window.TOKEN_HEADER))
        if count is None:
            return Response(status_code=403)
        return {"raise": count}

    @app.get("/app/window/status")
    def app_window_status(request: Request) -> dict:
        """Whether a window process is running and checking in (used by the build checks)."""
        from clipvault import window

        manager = window.manager()
        if manager is None or not _is_local(request):
            return {"running": False, "pid": None, "polled_s_ago": None}
        return manager.status()

    @app.get("/setup-password", response_class=HTMLResponse)
    def setup_password_form(request: Request):
        if auth.has_password():
            return back("/login")
        local = _is_local(request)
        return page(request, "password.html", local=local, error="")

    @app.post("/setup-password")
    def setup_password(request: Request, password: str = Form(...), again: str = Form(...)):
        if auth.has_password():
            return back("/login")
        if not _is_local(request):
            return HTMLResponse("Set the first password on the computer running ClipVault.", 403)
        error = ""
        if password != again:
            error = "The two passwords are different."
        else:
            try:
                auth.set_password(password)
            except ValueError as exc:
                error = str(exc).capitalize() + "."
        if error:
            return page(request, "password.html", local=True, error=error)
        return _logged_in(back("/"))

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if not auth.has_password():
            return back("/setup-password")
        return page(request, "login.html", error="", next=request.query_params.get("next", "/"))

    @app.post("/login")
    def login(request: Request, password: str = Form(...), next: str = Form("/")):
        if auth.check_password(password):
            target = next if next.startswith("/") and not next.startswith("//") else "/"
            return _logged_in(back(target))
        time.sleep(FAILED_LOGIN_DELAY_S)  # slows down guessing
        log.warning("failed panel login", extra=ctx(job="panel"))
        return page(request, "login.html", error="Wrong password.", next=next)

    @app.post("/logout")
    def logout():
        response = back("/login")
        response.delete_cookie(auth.COOKIE)
        return response

    # --- home -------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home_page(request: Request):
        with _db(conn) as c:
            if alerts.get_setting(c, "setup_done") != "1":
                return back("/setup/1")
            open_alerts = alerts.open_alerts(c)
            paused = alerts.is_paused(c)
            today = _local_midnight_utc(config())
            counts = {
                "saved": c.execute(
                    "SELECT count(*) FROM clips WHERE fetched_at >= ?", (today,)
                ).fetchone()[0],  # fmt: skip
                "waiting": c.execute(
                    "SELECT count(*) FROM clips WHERE state = 'found' AND removed_at IS NULL"
                ).fetchone()[0],
                "library": c.execute(
                    "SELECT count(*), coalesce(sum(file_size), 0) FROM clips "
                    "WHERE file_path IS NOT NULL AND removed_at IS NULL "
                    "AND state NOT IN ('found', 'dead')"
                ).fetchone(),
            }
            counts["live"] = c.execute(
                "SELECT count(*) FROM clips WHERE source = 'live_spike' AND created_at >= ?",
                (today,),
            ).fetchone()[0]
            counts["free"] = _free_bytes(storage.open_library(c, config()))
            newest = _clip_rows(c, f"{EDITED} AND c.removed_at IS NULL", [], "c.created_at DESC", 8)
            editing = _editing_count(c, config())
            last = c.execute(
                "SELECT max(last_success) FROM runs WHERE job LIKE 'step:%'"
            ).fetchone()[0]
            live = live_view(c)
        status = "red" if open_alerts else ("yellow" if paused else "green")
        return page(request, "home.html", tab="home", status=status, alerts=open_alerts,
                    problems=len(open_alerts),
                    paused=paused, counts=counts, newest=newest, last_run=last,
                    editing=editing, live=live)  # fmt: skip

    @app.post("/pause")
    def pause():
        with _db(conn) as c:
            alerts.set_setting(c, "paused", "1")
        return back("/", "Paused. Nothing new is looked for or downloaded until you resume.")

    @app.post("/resume")
    def resume():
        with _db(conn) as c:
            alerts.set_setting(c, "paused", "0")
        return back("/", "Running again.")

    # --- clips -------------------------------------------------------------------------

    @app.get("/clips", response_class=HTMLResponse)
    def clips_page(request: Request):
        q = request.query_params
        filters = {
            key: q.get(key, "")
            for key in ("streamer", "source", "q", "starred", "min_views", "sort")
        }
        page_no = max(int(q.get("page", "1") or 1), 1)
        where, params = ["c.file_path IS NOT NULL", "c.removed_at IS NULL",
                         "c.state NOT IN ('found', 'dead')", EDITED], []  # fmt: skip
        if filters["streamer"]:
            where.append("s.login = ?")
            params.append(filters["streamer"])
        if filters["source"] == "chat":
            where.append("c.source IN ('vod_spike', 'live_spike')")
        elif filters["source"] == "viewer":
            where.append("c.source IN ('viewer', 'auto')")
        if filters["starred"]:
            where.append("c.starred = 1")
        if filters["min_views"].isdigit():
            where.append("coalesce(c.view_count, 0) >= ?")
            params.append(int(filters["min_views"]))
        snippets: dict[str, str] = {}
        with _db(conn) as c:
            if filters["q"].strip():
                from clipvault.library.search import search_transcripts

                found = search_transcripts(c, filters["q"], limit=200)
                snippets = {row["id"]: row["snippet"] for row in found}
                if not snippets:
                    where.append("0")
                else:
                    where.append(f"c.id IN ({', '.join('?' * len(snippets))})")
                    params.extend(snippets)
            order = {
                "views": "coalesce(c.view_count, 0) DESC",
                "oldest": "c.created_at ASC",
            }.get(filters["sort"], "c.created_at DESC")
            total = c.execute(
                "SELECT count(*) FROM clips c JOIN streamers s ON s.id = c.streamer_id WHERE "
                + " AND ".join(where),
                params,
            ).fetchone()[0]
            rows = _clip_rows(c, " AND ".join(where), params, order, PAGE_SIZE,
                              (page_no - 1) * PAGE_SIZE)  # fmt: skip
            logins = [r[0] for r in c.execute("SELECT login FROM streamers ORDER BY login")]
            editing = _editing_count(c, config())
            # Loading cards for clips on their way: on the first page of the plain list only.
            plain = page_no == 1 and not any(filters.values())
            live = live_view(c) if plain else None
        pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        query = {k: v for k, v in filters.items() if v}
        return page(request, "clips.html", tab="clips", clips=rows, filters=filters, total=total,
                    page_no=page_no, pages=pages, logins=logins, snippets=snippets,
                    query=urlencode(query), editing=editing, live=live)  # fmt: skip

    @app.get("/clips/{clip_id}", response_class=HTMLResponse)
    def clip_page(request: Request, clip_id: str, view: str = ""):
        state: PanelState = app.state.panel
        with _db(conn) as c:
            rows = _clip_rows(c, "c.id = ?", [clip_id], "c.id", 1)
            if not rows:
                return HTMLResponse("No such clip.", 404)
            renders = c.execute(
                "SELECT * FROM renders WHERE clip_id = ? ORDER BY id DESC", (clip_id,)
            ).fetchall()
            text = c.execute(
                "SELECT text FROM transcripts WHERE clip_id = ?", (clip_id,)
            ).fetchone()
            library = storage.open_library(c, config())
        original = view == "original" or not rows[0]["render_id"]
        lines = _transcript_lines(library, rows[0], phone=not original)
        current = next((r for r in renders if r["id"] == rows[0]["render_id"]), None)
        checks = _fromjson(current["qa_report_json"]).get("checks", []) if current else []
        folder = " › ".join(
            [config().library_root.name, *rows[0]["file_path"].split("/")[:-1]]
        ) if rows[0]["file_path"] else ""  # fmt: skip
        return page(request, "clip.html", tab="clips", clip=rows[0], renders=renders,
                    current=current, checks=checks, folder=folder,
                    transcript=text[0] if text else "", rendering=clip_id in state.rendering,
                    original=original, lines=lines,
                    render_error=state.render_errors.get(clip_id, ""))  # fmt: skip

    @app.get("/media/{clip_id}")
    def media(clip_id: str, kind: str = "video", download: int = 0):
        with _db(conn) as c:
            library = storage.open_library(c, config())
            if kind == "vertical":
                row = c.execute(
                    "SELECT path FROM renders WHERE clip_id = ? AND path IS NOT NULL "
                    "ORDER BY created_at DESC, id DESC LIMIT 1",
                    (clip_id,),
                ).fetchone()
            else:
                row = c.execute("SELECT file_path FROM clips WHERE id = ?", (clip_id,)).fetchone()
        path = storage.resolve(library, row[0]) if row and row[0] else None
        if path is None or not storage.is_inside_library(library, path):
            return Response("File not found.", 404)
        if download:
            return FileResponse(path, media_type="video/mp4", filename=path.name)
        return FileResponse(path, media_type="video/mp4")

    @app.get("/thumb/{clip_id}")
    def thumbnail(clip_id: str, kind: str = "video", render: int = 0, at: str = ""):
        state: PanelState = app.state.panel
        vertical = kind == "vertical"
        # Keyed by when the phone version was made: a re-made version gets a new picture.
        made = "".join(ch for ch in at if ch.isdigit())[:14]
        name = storage.safe_id(clip_id) + (f"_v{render}_{made}" if vertical else "")
        target = state.home / "cache" / "thumbs" / f"{name}.jpg"
        if not target.is_file():
            with _db(conn) as c:
                library = storage.open_library(c, config())
                if vertical:
                    row = c.execute(
                        "SELECT path FROM renders WHERE clip_id = ? AND path IS NOT NULL "
                        "ORDER BY created_at DESC, id DESC LIMIT 1",
                        (clip_id,),
                    ).fetchone()
                else:
                    row = c.execute(
                        "SELECT file_path FROM clips WHERE id = ?", (clip_id,)
                    ).fetchone()
            video = storage.resolve(library, row[0]) if row and row[0] else None
            if video is None:
                return FileResponse(HERE / "static" / "no-thumb.svg", media_type="image/svg+xml")
            with state.thumb_lock(clip_id):
                if not target.is_file():
                    _make_thumbnail(video, target, 360 if vertical else 480)
        if not target.is_file():
            return FileResponse(HERE / "static" / "no-thumb.svg", media_type="image/svg+xml")
        return FileResponse(target, media_type="image/jpeg",
                            headers={"Cache-Control": "max-age=86400"})  # fmt: skip

    @app.post("/clips/{clip_id}/star")
    def star(clip_id: str, next: str = Form("")):
        with _db(conn) as c:
            c.execute("UPDATE clips SET starred = 1 - starred WHERE id = ?", (clip_id,))
        return back(_safe_next(next, f"/clips/{quote(clip_id, safe='')}"))

    @app.post("/clips/{clip_id}/delete")
    def delete(clip_id: str):
        from clipvault.library import retention

        with _db(conn) as c:
            library = storage.open_library(c, config())
            starred = c.execute("SELECT starred FROM clips WHERE id = ?", (clip_id,)).fetchone()
            if starred and starred[0]:
                return back(f"/clips/{quote(clip_id, safe='')}", "Starred clips can't be deleted.")
            if not library.available:
                return back("/clips", "The library folder can't be found; nothing was deleted.")
            removed = retention.remove_clip_files(c, library, clip_id, "user", "panel")
        return back("/clips", "Clip deleted; it won't be downloaded again." if removed
                    else "Nothing to delete.")  # fmt: skip

    @app.post("/clips/{clip_id}/save-to-photos")
    def save_to_photos(clip_id: str):
        """Import the phone version into the Mac's Photos app (macOS only)."""
        import subprocess
        import sys

        dest = f"/clips/{quote(clip_id, safe='')}"
        if sys.platform != "darwin":
            # Not a Mac: keep the old behaviour and download the file.
            return RedirectResponse(f"/media/{quote(clip_id, safe='')}?kind=vertical&download=1", 303)
        with _db(conn) as c:
            library = storage.open_library(c, config())
            row = c.execute(
                "SELECT path FROM renders WHERE clip_id = ? AND path IS NOT NULL "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (clip_id,),
            ).fetchone()
        path = storage.resolve(library, row[0]) if row and row[0] else None
        if path is None or not storage.is_inside_library(library, path):
            return back(dest, "The video file wasn't found.")
        script = (
            "on run argv\n"
            "set f to POSIX file (item 1 of argv)\n"
            'tell application "Photos" to import {f} skip check duplicates true\n'
            "end run"
        )
        try:
            r = subprocess.run(["osascript", "-e", script, str(path)],
                               capture_output=True, text=True, timeout=120)  # fmt: skip
        except (OSError, subprocess.TimeoutExpired):
            return back(dest, "Couldn't reach Photos. Try again.")
        if r.returncode != 0:
            return back(dest, "Photos didn't accept it. In System Settings > Privacy & Security > "
                        "Automation, allow ClipVault to control Photos, then try again.")  # fmt: skip
        return back(dest, "Saved to Photos.")

    @app.post("/clips/{clip_id}/vertical")
    def make_vertical(clip_id: str, layout: str = Form("")):
        state: PanelState = app.state.panel
        if clip_id not in state.rendering:
            state.rendering.add(clip_id)
            state.render_errors.pop(clip_id, None)
            state.executor.submit(_render, state, home, clip_id, layout or None)
        return back(f"/clips/{quote(clip_id, safe='')}",
                    "Making the vertical copy; this takes about half a minute.")  # fmt: skip

    # --- streamers ------------------------------------------------------------------------

    @app.get("/streamers", response_class=HTMLResponse)
    def streamers_page(request: Request):
        with _db(conn) as c:
            rows = c.execute(
                "SELECT s.*, (SELECT count(*) FROM clips c WHERE c.streamer_id = s.id "
                "AND c.file_path IS NOT NULL AND c.removed_at IS NULL "
                "AND c.state NOT IN ('found', 'dead')) AS saved, "
                "(SELECT coalesce(sum(c.file_size), 0) FROM clips c WHERE c.streamer_id = s.id "
                "AND c.removed_at IS NULL) AS bytes "
                "FROM streamers s ORDER BY s.login"
            ).fetchall()
        live = _live_now(
            app.state.panel, [r["twitch_user_id"] for r in rows if r["twitch_user_id"]]
        )
        from clipvault.web.settings_routes import estimate_gb_per_month

        estimate = estimate_gb_per_month([dict(r) for r in rows], config().quality)
        return page(request, "streamers.html", tab="streamers", streamers=rows, live=live,
                    estimate=estimate)  # fmt: skip

    @app.post("/streamers/add")
    def add_streamer(login: str = Form(...), next: str = Form("/streamers")):
        from clipvault.streamers import StreamersFileError, import_streamers, parse_streamers_text

        try:
            entries = parse_streamers_text(login.strip())
        except StreamersFileError as exc:
            return back(_safe_next(next, "/streamers"), str(exc).strip())
        if not entries:
            return back(_safe_next(next, "/streamers"), "Type a Twitch login or channel link.")
        with _db(conn) as c:
            with db.transaction(c):
                result = import_streamers(c, entries)
            message = _refresh_profiles(c)
        name = entries[0].login
        text = f"{name} added." if result.added else f"{name} is already on the list."
        return back(_safe_next(next, "/streamers"), f"{text} {message}".strip())

    @app.post("/streamers/{streamer_id}/update")
    def update_streamer(
        streamer_id: int,
        save: str = Form(""),
        top_n: str = Form("5"),
        min_views: str = Form(""),
        vod_moments: str = Form("3"),
        layout: str = Form(""),
    ):
        try:
            values = (
                1 if save else 0,
                _int_in(top_n, 1, 100, "Clips per day"),
                _int_in(min_views, 0, None, "Minimum views") if min_views.strip() else None,
                _int_in(vod_moments, 0, 50, "Chat moments per stream"),
                layout if layout in ("letterbox", "split", "native") else None,
                streamer_id,
            )
        except ValueError as exc:
            return back("/streamers", str(exc))
        with _db(conn) as c:
            c.execute(
                "UPDATE streamers SET save_enabled = ?, top_n = ?, min_views = ?, vod_moments = ?, "
                "layout_override = ? WHERE id = ?",
                values,
            )
        return back("/streamers", "Saved.")

    @app.post("/streamers/{streamer_id}/remove")
    def remove_streamer(streamer_id: int):
        with _db(conn) as c:
            row = c.execute("SELECT login FROM streamers WHERE id = ?", (streamer_id,)).fetchone()
            if row is None:
                return back("/streamers")
            has_clips = c.execute(
                "SELECT 1 FROM clips WHERE streamer_id = ? LIMIT 1", (streamer_id,)
            ).fetchone()
            if has_clips:
                # Their clips stay in the library; saving simply stops.
                c.execute(
                    "UPDATE streamers SET save_enabled = 0, post_enabled = 0 WHERE id = ?",
                    (streamer_id,),
                )
                return back("/streamers", f"Saving stopped for {row[0]}. Their saved clips stay.")
            with db.transaction(c):
                c.execute("DELETE FROM runs WHERE job = ?", (f"discover:{streamer_id}",))
                c.execute("DELETE FROM vods WHERE streamer_id = ?", (streamer_id,))
                c.execute("DELETE FROM streamers WHERE id = ?", (streamer_id,))
        return back("/streamers", f"{row[0]} removed.")

    # --- alerts -----------------------------------------------------------------------------

    @app.get("/alerts", response_class=HTMLResponse)
    def alerts_page(request: Request):
        with _db(conn) as c:
            rows = c.execute(
                "SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY id DESC"
            ).fetchall()
        return page(request, "alerts.html", tab="home", alerts=rows)

    @app.post("/alerts/{alert_id}/dismiss")
    def dismiss(alert_id: int):
        with _db(conn) as c:
            c.execute(
                "UPDATE alerts SET resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
                "WHERE id = ?",
                (alert_id,),
            )
        return back("/alerts", "Dismissed. It comes back if the problem happens again.")

    # --- settings and setup live in their own module --------------------------------------

    from clipvault.web import activity_routes, settings_routes

    settings_routes.register(app, page=page, back=back, conn=conn, config=config, home=home)
    clip_card = templates.env.get_template("_macros.html").module.clip_card
    activity_routes.register(app, back=back, conn=conn, config=config, card=clip_card,
                             clip_rows=_clip_rows, local_midnight=_local_midnight_utc)  # fmt: skip

    def live_view(c: sqlite3.Connection) -> dict[str, Any]:
        """The live view's first answer, written into the page so it shows at once."""
        current = config()
        return activity_routes.payload(
            c, current, since=0, after=to_rfc3339(utc_now()), card=clip_card,
            clip_rows=_clip_rows, today=_local_midnight_utc(current))  # fmt: skip

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(HERE / "static" / "manifest.webmanifest",
                            media_type="application/manifest+json")  # fmt: skip

    @app.exception_handler(FormTokenError)
    async def form_token_error(request: Request, exc: FormTokenError):
        return HTMLResponse("This form expired. Go back, reload the page and try again.", 403)

    return app


# --- helpers -------------------------------------------------------------------------------


class FormTokenError(Exception):
    """A POST without a valid form token (CSRF protection)."""


async def _check_form_token(request: Request) -> None:
    """Every POST needs the token from the page it came from (except the login forms).

    Runs as a FastAPI dependency, so the form is parsed once and the route still
    receives it (reading it in middleware would leave the route an empty form).
    """
    if request.method != "POST" or request.url.path in ("/login", "/setup-password"):
        return
    form = await request.form()
    if not auth.csrf_valid(request.cookies.get(auth.COOKIE), str(form.get("csrf") or "")):
        raise FormTokenError


class _db:
    """``with _db(conn) as c:`` opens a connection for one request and closes it."""

    def __init__(self, factory) -> None:
        self.factory = factory

    def __enter__(self) -> sqlite3.Connection:
        self.conn = self.factory()
        return self.conn

    def __exit__(self, *exc) -> None:
        self.conn.close()


def _logged_in(response: RedirectResponse) -> RedirectResponse:
    response.set_cookie(
        auth.COOKIE,
        auth.make_session(),
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,
        samesite="strict",
    )
    return response


def _is_local(request: Request) -> bool:
    return bool(request.client and request.client.host in LOCAL_HOSTS)


def _safe_next(target: str, fallback: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") else fallback


def _int_in(text: str, low: int, high: int | None, label: str) -> int:
    try:
        value = int(text.strip())
    except ValueError:
        raise ValueError(f"{label} must be a whole number.") from None
    if value < low or (high is not None and value > high):
        limit = f"between {low} and {high}" if high is not None else f"at least {low}"
        raise ValueError(f"{label} must be {limit}.")
    return value


def _highlight(snippet: str) -> Markup:
    """Search snippet with the matched words (between [ and ]) marked, safely escaped."""
    escaped = str(escape(snippet))
    return Markup(escaped.replace("[", "<mark>").replace("]", "</mark>"))


def _transcript_lines(library: storage.Library, clip: sqlite3.Row, phone: bool) -> list[dict]:
    """The clip's subtitle lines with their start times, for the clip page.

    For the phone version the times count from where it starts (clips over 59 s
    keep their last 59 s), so picking a line can move the video there.
    """
    from clipvault.process.transcribe import Transcript, cues_from_words

    video = storage.resolve(library, clip["file_path"])
    words_file = video.with_suffix(".words.json") if video else None
    if words_file is None or not words_file.is_file():
        return []
    try:
        words = Transcript.from_json(words_file.read_text(encoding="utf-8")).words
    except (OSError, ValueError, KeyError, TypeError):
        return []
    offset = max((clip["duration"] or 0) - 59, 0) if phone else 0
    lines = []
    for cue in cues_from_words(words):
        if cue.end <= offset:
            continue
        start = max(cue.start - offset, 0)
        lines.append({"start": round(start, 2), "end": round(cue.end - offset, 2),
                      "label": _clock(start), "text": cue.text})  # fmt: skip
    return lines


def _when(stamp: str | None, tz_name: str) -> str:
    """ "Nov 13, 05:41" in the user's time zone."""
    from zoneinfo import ZoneInfo

    from clipvault.timeutil import parse_rfc3339

    if not stamp:
        return ""
    try:
        local = parse_rfc3339(stamp).astimezone(ZoneInfo(tz_name))
    except ValueError:
        return ""
    return f"{local:%b} {local.day}, {local:%H:%M}"


def _ago(stamp: str | None) -> str:
    """ "25 min ago", "2 h ago", "yesterday", "3 days ago" or a date."""
    from clipvault.timeutil import parse_rfc3339, utc_now

    if not stamp:
        return ""
    try:
        seconds = (utc_now() - parse_rfc3339(stamp)).total_seconds()
    except ValueError:
        return ""
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    if seconds < 2 * 86400:
        return "yesterday"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)} days ago"
    return stamp[:10]


def _compact(number: Any) -> str:
    """1234 -> "1.2K", 48100 -> "48.1K"."""
    value = int(number or 0)
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1000:.1f}K".replace(".0K", "K")
    return f"{value / 1_000_000:.1f}M".replace(".0M", "M")


def _fromjson(text: str | None) -> dict:
    import json

    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _free_bytes(library: storage.Library) -> int | None:
    import shutil

    try:
        return shutil.disk_usage(library.write_base if library.available else library.cache).free
    except OSError:
        return None


def _clock(seconds: Any) -> str:
    if seconds is None:
        return ""
    whole = int(float(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def _local_midnight_utc(config: cfg.Config) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from clipvault.timeutil import to_rfc3339

    tz = ZoneInfo(config.timezone)
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return to_rfc3339(midnight)


def _clip_rows(
    c: sqlite3.Connection, where: str, params: list, order: str, limit: int, offset: int = 0
) -> list[sqlite3.Row]:
    return c.execute(
        "SELECT c.*, s.login, s.display_name, "
        "(SELECT count(*) FROM renders r WHERE r.clip_id = c.id AND r.path IS NOT NULL) "
        "AS renders, "
        "(SELECT r.id FROM renders r WHERE r.clip_id = c.id AND r.path IS NOT NULL "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS render_id, "
        "(SELECT r.created_at FROM renders r WHERE r.clip_id = c.id AND r.path IS NOT NULL "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS render_at "
        f"FROM clips c JOIN streamers s ON s.id = c.streamer_id WHERE {where} "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()


EDITED = "EXISTS (SELECT 1 FROM renders r WHERE r.clip_id = c.id AND r.path IS NOT NULL)"


def _editing_count(c: sqlite3.Connection, config: cfg.Config) -> int:
    """Saved clips whose phone version hasn't been made yet."""
    from clipvault import jobs

    return len(jobs.clips_to_render(c, config))


def _make_thumbnail(video: Path, target: Path, width: int = 480) -> None:
    from clipvault.render import ffmpeg

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        ffmpeg._ffmpeg(
            ["-y", "-loglevel", "error", "-ss", "1", "-i", str(video), "-frames:v", "1",
             "-vf", f"scale={width}:-2", "-q:v", "5", str(target)],
            what="thumbnail",
        )  # fmt: skip
    except ffmpeg.RenderError as exc:
        log.warning("no thumbnail: %s", exc, extra=ctx(job="panel"))


def _render(state: PanelState, home: Path, clip_id: str, layout: str | None) -> None:
    from clipvault.render import engine

    try:
        conn = db.open_db(home / cfg.DB_FILENAME)
        try:
            config = cfg.load_config(home / cfg.CONFIG_FILENAME)
            result = engine.render_clip(conn, config, clip_id, layout=layout)
            if not result.report.passed:
                failed = ", ".join(check.name for check in result.report.failures())
                state.render_errors[clip_id] = f"Made, but the quality check flagged: {failed}"
        finally:
            conn.close()
    except Exception as exc:  # shown on the clip page; the panel keeps running
        state.render_errors[clip_id] = str(exc)[:300]
        log.error("panel render failed: %s", exc, extra=ctx(clip=clip_id, job="panel"))
    finally:
        state.rendering.discard(clip_id)


def _live_now(state: PanelState, user_ids: list[str]) -> dict[str, str]:
    """Who is live right now (checked at most every 2 minutes; empty when offline)."""
    if not user_ids or time.time() - state.live_checked < LIVE_CACHE_S:
        return state.live
    from clipvault import twitch

    state.live_checked = time.time()
    try:
        client = twitch.build_client()
    except twitch.MissingCredentials:
        return state.live
    try:
        state.live = client.get_live_stream_ids(user_ids)
    except Exception as exc:  # live status is a nicety; never break the page for it
        log.warning("live status unavailable: %s", exc, extra=ctx(job="panel"))
    finally:
        client.close()
    return state.live


def _refresh_profiles(c: sqlite3.Connection) -> str:
    """Look new streamers up on Twitch (name, avatar); returns a note for the user."""
    from clipvault import twitch
    from clipvault.streamers import refresh_profiles

    try:
        client = twitch.build_client()
    except twitch.MissingCredentials:
        return "Their Twitch profile is looked up once the Twitch keys are set."
    try:
        missing = refresh_profiles(c, client)
    except Exception as exc:
        log.warning("profile lookup failed: %s", exc, extra=ctx(job="panel"))
        return "Twitch couldn't be reached; the profile is looked up later."
    finally:
        client.close()
    if missing:
        return "Twitch doesn't know: " + ", ".join(missing) + ". Check the spelling."
    return ""
