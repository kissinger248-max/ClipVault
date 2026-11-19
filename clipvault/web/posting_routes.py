"""The Posting page: Platform Setup, Connected Accounts, Queue, Post manually and Settings.

Every change is a form POST carrying the panel's form token (``csrf``) and a JSON ``payload`` field, so the
panel's CSRF protection covers it like any other form. Reads are GETs that return JSON. Secrets are never
returned: the page only learns whether a field is saved.
"""

from __future__ import annotations

import html
import json
import sqlite3
import webbrowser
from urllib.parse import urlparse
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from clipvault import config as cfg
from clipvault import db
from clipvault.posting import captiongen, oauth, platforms, store, uploads
from clipvault.posting.oauth import OAuthError
from clipvault.posting.secrets import Secrets


def register(app: FastAPI, *, page: Callable[..., HTMLResponse], conn: Callable[[], sqlite3.Connection],
             config: Callable[[], cfg.Config], secrets: Secrets | None = None) -> None:
    secrets = secrets or Secrets()

    async def body(request: Request) -> dict[str, Any]:
        form = await request.form()
        try:
            data = json.loads(str(form.get("payload") or "{}"))
        except ValueError:
            raise HTTPException(400, "bad request")
        return data if isinstance(data, dict) else {}

    def port_of(request: Request) -> int:
        return request.url.port or 80

    class Db:
        def __enter__(self) -> sqlite3.Connection:
            self.c = conn()
            return self.c

        def __exit__(self, *exc) -> None:
            self.c.close()

    def library(c: sqlite3.Connection):
        return uploads.library_for(c, config())

    @app.get("/posting", response_class=HTMLResponse)
    def posting_page(request: Request):
        return page(request, "posting.html", tab="posting")

    # --- Platform setup --------------------------------------------------------------------------
    @app.get("/api/posting/setup")
    def setup(request: Request):
        with Db() as c:
            out = {}
            for key, spec in platforms.PLATFORMS.items():
                out[key] = {"label": spec["label"], "guide": spec["guide"], "scopes": spec["scopes"],
                            "oauth": spec["oauth"],
                            "redirect_uri": platforms.redirect_uri(key, port_of(request)) if spec["oauth"] else None,
                            "fields": [{"key": k, "label": lbl, "saved": secrets.has(f"cred:{key}:{k}")}
                                       for k, lbl in spec["fields"]]}
            return {"platforms": out, "settings": store.all_settings(c)}

    @app.post("/api/posting/credentials/{platform}")
    async def save_credentials(platform: str, request: Request):
        spec = platforms.PLATFORMS.get(platform)
        if not spec:
            raise HTTPException(404)
        data = await body(request)
        n = 0
        for k, _label in spec["fields"]:
            v = str(data.get(k) or "").strip()
            if v:
                secrets.set(f"cred:{platform}:{k}", v)
                n += 1
        return {"saved": n}

    @app.post("/api/posting/credentials/{platform}/clear")
    def clear_credentials(platform: str):
        spec = platforms.PLATFORMS.get(platform)
        if not spec:
            raise HTTPException(404)
        for k, _label in spec["fields"]:
            secrets.delete(f"cred:{platform}:{k}")
        return {"ok": True}

    @app.post("/api/posting/settings")
    async def save_settings(request: Request):
        data = await body(request)
        with Db() as c:
            for k, v in data.items():
                try:
                    store.set_setting(c, k, str(v))
                except ValueError:
                    raise HTTPException(400, f"unknown setting {k}")
                if k == "dry_run" and str(v) == "0":      # going live: practice jobs must be queued again for real
                    c.execute("DELETE FROM upload_jobs WHERE dry_run=1")
            if {"min_gap_minutes", "jitter_minutes"} & set(data):
                uploads.respace(c)                        # what is already queued follows the new spacing
            return store.all_settings(c)

    # --- Connected accounts ----------------------------------------------------------------------
    @app.get("/api/posting/accounts")
    def accounts():
        with Db() as c:
            err = c.execute("SELECT value FROM posting_settings WHERE key='last_connect_error'").fetchone()
            return {"accounts": [dict(a) for a in c.execute("SELECT * FROM social_accounts ORDER BY platform, id")],
                    "rules": [dict(a) for a in c.execute("SELECT * FROM routing_rules ORDER BY id")],
                    "connect_error": err["value"] if err else ""}

    @app.post("/api/posting/connect/{platform}")
    async def connect(platform: str, request: Request):
        data = await body(request)
        with Db() as c:
            try:
                return {"url": oauth.begin(c, secrets, platform, str(data.get("label") or ""), port_of(request))}
            except OAuthError as e:
                raise HTTPException(400, str(e))

    @app.post("/api/posting/open-external")
    async def open_external(request: Request):
        """Open a platform's sign-in page in the computer's own browser (the app window can't show it).

        Only the platforms' own https sign-in addresses are accepted, and only from this computer."""
        if (request.client.host if request.client else "") not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "Only from this computer.")
        url = str((await body(request)).get("url") or "")
        allowed = {urlparse(str(spec["auth_url"])).hostname for spec in platforms.PLATFORMS.values() if spec.get("auth_url")}
        parts = urlparse(url)
        if parts.scheme != "https" or parts.hostname not in allowed:
            raise HTTPException(400, "Not a sign-in address.")
        opened = open_in_browser(url)
        return {"opened": opened}

    @app.get("/oauth/{platform}/callback")
    def callback(request: Request, platform: str, code: str = "", state: str = "", error: str = "",
                 error_description: str = ""):
        """Where the platform sends the person back. Public (the sign-in may happen in another browser);
        safe because ``state`` must match a one-time value made when Connect was pressed."""
        if platform not in platforms.PLATFORMS:
            raise HTTPException(404)
        label = platforms.PLATFORMS[platform]["label"]

        def failed(message: str, status: int = 400) -> HTMLResponse:
            with Db() as c:     # shown on the Posting page too, so the app window explains what went wrong
                c.execute("INSERT OR REPLACE INTO posting_settings VALUES ('last_connect_error', ?)",
                          (f"{label}: {message}",))
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>Couldn't connect</title><body style='font-family:-apple-system,system-ui,sans-serif;"
                "max-width:34em;margin:15vh auto;padding:0 1em;text-align:center'><h1>Couldn't connect</h1>"
                f"<p>{html.escape(label)}: {html.escape(message)}</p><p>Go back to ClipVault and press Connect again.</p></body>", status)

        if error or not code:
            if error == "access_denied":
                return failed("you didn't approve the connection on the sign-in page.")
            return failed(f"the sign-in was cancelled or refused ({error_description or error or 'no code came back'}).")
        with Db() as c:
            try:
                oauth.finish(c, secrets, platform, code, state, port_of(request))
            except OAuthError as e:
                return failed(str(e))
            c.execute("DELETE FROM posting_settings WHERE key='last_connect_error'")
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Connected</title><body style='font-family:-apple-system,system-ui,sans-serif;max-width:30em;"
            "margin:15vh auto;padding:0 1em;text-align:center'><h1>Connected ✓</h1>"
            "<p>You can close this tab and go back to ClipVault. The account now shows under "
            "Posting → Connected Accounts.</p></body>")

    @app.post("/api/posting/connect-error/clear")
    def clear_connect_error():
        with Db() as c:
            c.execute("DELETE FROM posting_settings WHERE key='last_connect_error'")
        return {"ok": True}

    @app.post("/api/posting/accounts/{account_id}/disconnect")
    def disconnect(account_id: int):
        with Db() as c:
            oauth.disconnect(c, secrets, account_id)
        return {"ok": True}

    @app.post("/api/posting/rules")
    async def add_rule(request: Request):
        b = await body(request)
        with Db() as c:
            c.execute("INSERT INTO routing_rules (streamer, clip_type, account_id) VALUES (?,?,?)",
                      (str(b.get("streamer") or "*").strip().lower() or "*",
                       str(b.get("clip_type") or "*").strip().lower() or "*", int(b["account_id"])))
        return {"ok": True}

    @app.post("/api/posting/rules/{rule_id}/delete")
    def del_rule(rule_id: int):
        with Db() as c:
            c.execute("DELETE FROM routing_rules WHERE id=?", (rule_id,))
        return {"ok": True}

    # --- Captions ---------------------------------------------------------------------------------
    def caption_for(c: sqlite3.Connection, clip_id: str, force: bool = False):
        clip = uploads.clip_info(c, clip_id)
        if clip is None:
            raise HTTPException(404, "clip not found")
        pkg, facts = uploads.ensure_caption(c, library(c), clip, force=force)
        return {"caption": pkg, "facts": facts}

    @app.get("/api/posting/caption/{clip_id}")
    def caption(clip_id: str):
        with Db() as c:
            return caption_for(c, clip_id)

    @app.post("/api/posting/caption/{clip_id}/regenerate")
    def regenerate(clip_id: str):
        with Db() as c:
            return caption_for(c, clip_id, force=True)

    @app.post("/api/posting/caption/{clip_id}")
    async def edit_caption(clip_id: str, request: Request):
        data = await body(request)
        with Db() as c:
            try:
                return {"caption": uploads.save_edit(c, clip_id, data.get("per_platform") or {})}
            except KeyError:
                raise HTTPException(404, "no caption yet")

    # --- Queue and manual list --------------------------------------------------------------------
    @app.get("/api/posting/queue")
    def queue():
        with Db() as c:
            out = uploads.overview(c)
            titles = {r["id"]: r["title"] for r in c.execute("SELECT id, title FROM clips WHERE id IN (SELECT clip_id FROM upload_jobs)")}
            for key in ("queue", "manual", "done"):
                for j in out[key]:
                    j["title"] = titles.get(j["clip_id"]) or j["clip_id"]
            out["ready"] = [{"id": r["id"], "title": r["title"], "streamer": r["display_name"] or r["login"],
                             "blocked": r["blocked"], "qa_failed": r["qa_failed"]}
                            for r in uploads.ready_clips(c, library(c))]
            return out

    @app.post("/api/posting/enqueue/{clip_id}")
    def enqueue(clip_id: str):
        with Db() as c:
            try:
                return {"jobs": uploads.enqueue(c, library(c), clip_id)}
            except uploads.NotAllowed as e:
                raise HTTPException(403, str(e))

    @app.post("/api/posting/jobs/{job_id}/posted")
    def posted(job_id: int):
        with Db() as c:
            try:
                uploads.mark_posted(c, job_id)
            except KeyError:
                raise HTTPException(404)
        return {"ok": True}

    @app.post("/api/posting/jobs/{job_id}/retry")
    def retry(job_id: int):
        with Db() as c:
            uploads.retry_job(c, job_id)
        return {"ok": True}

    @app.post("/api/posting/reveal/{clip_id}")
    def reveal(clip_id: str):
        with Db() as c:
            clip = uploads.clip_info(c, clip_id)
            path = uploads.clip_path(library(c), clip) if clip else None
        if path is None:
            raise HTTPException(404, "The video file wasn't found.")
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", str(path)], check=False)
        return {"ok": True}

    @app.post("/api/posting/run")
    def run_now():
        with Db() as c:
            return run_round(c, config())


def open_in_browser(url: str) -> bool:
    """Ask the operating system to open ``url`` in the default browser (a separate function so tests can replace it)."""
    try:
        return bool(webbrowser.open(url))
    except webbrowser.Error:
        return False


def run_round(c: sqlite3.Connection, config: cfg.Config) -> dict:
    """One pass of the posting step: queue (auto-approve), upload what is due, write a few captions ahead."""
    library = uploads.library_for(c, config)
    out = uploads.tick(c, library, Secrets())
    made = 0
    for row in c.execute(uploads.CLIP_SQL + "WHERE s.post_enabled = 1 AND EXISTS (SELECT 1 FROM renders r WHERE r.clip_id = c.id "
                         "AND r.path IS NOT NULL) AND NOT EXISTS (SELECT 1 FROM clip_captions x WHERE x.clip_id = c.id) "
                         "ORDER BY c.updated_at DESC LIMIT 2").fetchall():
        uploads.ensure_caption(c, library, dict(row))
        made += 1
    out["captions"] = made
    return out


def step_posting(conn: sqlite3.Connection, config: cfg.Config, now) -> str:
    """The scheduler's posting step (every minute): quiet unless something happened."""
    out = run_round(conn, config)
    ran = out["ran"]
    if not (out["queued"] or ran or out["captions"]):
        return ""
    return f"queued {out['queued']}, ran {sum(ran.values())}, {out['captions']} captions"
