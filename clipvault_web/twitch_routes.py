"""The website's Twitch pages: pasted links, streamer search, sign-in, "My channel",
opting out, and reporting a problem (docs/WEB_VERSION_PLAN.md, W3)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from clipvault.timeutil import to_rfc3339, utc_now
from clipvault.twitch.api import TwitchAPIError, TwitchClient

from . import accounts, events, limits, store, twitch_making, twitch_web
from .settings import WebSettings
from .twitch_web import LinkProblem

SEARCH_CACHE_S = 60
REASONS = {
    "copyright": "It uses my content without permission",
    "my-content": "I'm in it and want it removed",
    "abuse": "It's harmful or abusive",
    "other": "Something else",
}


def add(
    app: FastAPI,
    settings: WebSettings,
    *,
    page: Callable[..., HTMLResponse],
    counted: Callable[[str, str], None],
    visitor_of: Callable[[Request], str],
    guard: Callable[[Request, Any], Any],
    twitch: TwitchClient | None = None,
    http: httpx.Client | None = None,
) -> None:
    """Add the Twitch routes. ``twitch`` and ``http`` stand in for Twitch in tests."""
    state: dict[str, Any] = {"client": twitch, "http": http, "search": {}}
    searches = limits.RateLimiter(per_minute=30)
    lock = threading.Lock()

    def client() -> TwitchClient:
        with lock:
            if state["client"] is None:
                state["client"] = twitch_web.build_client(settings)
            return state["client"]

    def web_http() -> httpx.Client:
        with lock:
            if state["http"] is None:
                state["http"] = httpx.Client(timeout=twitch_web.HTTP_TIMEOUT_S)
            return state["http"]

    def session_of(request: Request, conn) -> accounts.Session | None:
        return accounts.current(conn, request.cookies.get(accounts.SESSION_COOKIE))

    def problem(message: str, status: int = 400) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    # --- a pasted link ---------------------------------------------------------------------

    @app.post("/api/twitch")
    async def start_from_link(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return problem("Paste a Twitch link or type a streamer's name.")
        return await run_in_threadpool(_start, request, str(body.get("link", ""))[:300])

    def _start(request: Request, text: str, mine: accounts.Session | None = None):
        conn = store.open_store(settings)
        try:
            refused = guard(request, conn)
            if refused:
                from .server import refusal_reply

                return refusal_reply(refused)
            try:
                if mine is not None:
                    items, more = twitch_web.channel_clips(client(), mine.user_id,
                                                           settings.clips_per_run)  # fmt: skip
                    if not items:
                        raise LinkProblem("Your channel has no clips of at least 15 seconds "
                                          "from the last month yet.")  # fmt: skip
                    plan = twitch_web.Plan(
                        "twitch_mine",
                        mine.user_id,
                        mine.login,
                        mine.name,
                        f"{mine.name}: my channel",
                        False,
                        items,
                        more,
                    )
                    link_text = f"https://www.twitch.tv/{mine.login}"
                else:
                    link = twitch_web.parse_link(text)
                    plan = twitch_web.plan_run(client(), link, settings.clips_per_run)
                    link_text = text
            except LinkProblem as exc:
                return problem(str(exc))
            if mine is None and store.opted_out(conn, plan.user_id, plan.login):
                return problem(
                    f"{plan.name} asked not to be clipped here. Their own channel and "
                    "the free app are the places to find their moments.",
                    403,
                )
            if plan.kind == "twitch_vod" and not plan.items and not _chat_tool():
                return problem("Nobody has clipped this stream yet, so there's nothing to make.")
            grant = (mine.user_id, mine.token) if mine and mine.token else None
            embed = grant is None and not store.downloads_on(conn, settings)
            run_id = store.new_run_id()
            twitch_making.create_run(conn, settings, plan, run_id=run_id, source_url=link_text,
                                     visitor=visitor_of(request), embed_only=embed,
                                     grant=grant)  # fmt: skip
            events.count(conn, "run_started", plan.kind + (":embed" if embed else ""))
        finally:
            conn.close()
        if not embed:
            app.state.worker.wake()
        return {"id": run_id, "url": f"/r/{run_id}"}

    def _chat_tool() -> bool:
        from clipvault.twitch import chat_replay

        return chat_replay.find_tool(settings.data) is not None

    # --- streamer search -------------------------------------------------------------------

    @app.get("/api/search")
    def search_streamers(request: Request, q: str = Query("", max_length=60)):
        query = q.strip().lower()
        if len(query) < 2 or not searches.allow(visitor_of(request)):
            return {"results": []}
        cached = state["search"].get(query)
        now = utc_now().timestamp()
        if cached and now - cached[0] < SEARCH_CACHE_S:
            results = cached[1]
        else:
            try:
                results = twitch_web.search(client(), query)
            except (LinkProblem, TwitchAPIError, httpx.HTTPError):
                return {"results": []}
            if len(state["search"]) > 500:
                state["search"].clear()
            state["search"][query] = (now, results)
        conn = store.open_store(settings)
        try:  # an opt-out counts at once, cached or not
            shown = [r for r in results if not store.opted_out(conn, r.get("id"), r["login"])]
        finally:
            conn.close()
        return {"results": shown}

    # --- signing in --------------------------------------------------------------------------

    @app.get("/auth/twitch")
    def sign_in(purpose: str = Query("mine", alias="for")):
        if not settings.twitch_client_id or purpose not in accounts.SCOPES:
            return RedirectResponse("/", status_code=303)
        state_value, cookie = accounts.new_state(purpose)
        reply = RedirectResponse(accounts.authorize_url(settings, purpose, state_value), 303)
        secure = settings.public_url.startswith("https")
        reply.set_cookie(accounts.STATE_COOKIE, cookie, max_age=600, httponly=True,
                         samesite="lax", secure=secure)  # fmt: skip
        return reply

    @app.get("/auth/twitch/callback")
    def signed_in(request: Request, code: str = "", state: str = "", error: str = ""):
        cookie = request.cookies.get(accounts.STATE_COOKIE)
        if error:
            return page(request, "message.html", title="Sign-in cancelled",
                        text="Nothing was changed.", link="/", link_text="Back")  # fmt: skip
        conn = store.open_store(settings)
        try:
            purpose = accounts.check_state(cookie, state)
            session = accounts.finish_sign_in(conn, settings, code, purpose, web_http())
        except (accounts.SignInFailed, TwitchAPIError, httpx.HTTPError) as exc:
            message = str(exc) if isinstance(exc, accounts.SignInFailed) else (
                "Twitch didn't answer. Please try again.")  # fmt: skip
            return page(request, "message.html", 400, title="Sign-in didn't work", text=message,
                        link="/", link_text="Back")  # fmt: skip
        finally:
            conn.close()
        reply = RedirectResponse("/mine" if purpose == "mine" else "/optout", status_code=303)
        reply.delete_cookie(accounts.STATE_COOKIE)
        reply.set_cookie(accounts.SESSION_COOKIE, session.id, max_age=accounts.SESSION_DAYS * 86400,
                         httponly=True, samesite="lax",
                         secure=settings.public_url.startswith("https"))  # fmt: skip
        return reply

    @app.post("/auth/logout")
    def sign_out(request: Request, csrf: str = Form("")):
        session_id = request.cookies.get(accounts.SESSION_COOKIE)
        conn = store.open_store(settings)
        try:
            if accounts.check_form(conn, session_id, csrf):
                accounts.sign_out(conn, settings, session_id, web_http())
        finally:
            conn.close()
        reply = RedirectResponse("/", status_code=303)
        reply.delete_cookie(accounts.SESSION_COOKIE)
        return reply

    # --- my channel ----------------------------------------------------------------------------

    @app.get("/mine", response_class=HTMLResponse)
    def my_channel(request: Request):
        conn = store.open_store(settings)
        try:
            session = session_of(request, conn)
            token = accounts.form_token(conn, session.id) if session else ""
        finally:
            conn.close()
        return page(request, "mine.html", session=session, csrf=token,
                    enabled=bool(settings.twitch_client_id))  # fmt: skip

    @app.post("/api/mine")
    async def start_my_channel(request: Request):
        conn = store.open_store(settings)
        try:
            session = session_of(request, conn)
            ok = session is not None and accounts.check_form(
                conn, session.id, request.headers.get("x-form"))  # fmt: skip
        finally:
            conn.close()
        if not ok or session is None:
            return problem("Please sign in with Twitch again.", 401)
        if not session.can_download:
            return problem("Your Twitch sign-in has expired. Please sign in again.", 401)
        return await run_in_threadpool(_start, request, "", session)

    # --- opting out ----------------------------------------------------------------------------

    @app.get("/optout", response_class=HTMLResponse)
    def optout_page(request: Request):
        conn = store.open_store(settings)
        try:
            session = session_of(request, conn)
            blocked = bool(session) and store.opted_out(conn, session.user_id, session.login)
            token = accounts.form_token(conn, session.id) if session else ""
        finally:
            conn.close()
        return page(request, "optout.html", session=session, blocked=blocked, csrf=token,
                    enabled=bool(settings.twitch_client_id))  # fmt: skip

    @app.post("/optout")
    def optout_change(request: Request, action: str = Form(""), csrf: str = Form("")):
        conn = store.open_store(settings)
        try:
            session = session_of(request, conn)
            if session is None or not accounts.check_form(conn, session.id, csrf):
                return RedirectResponse("/optout", status_code=303)
            if action == "block":
                store.opt_out(conn, settings, session.user_id, session.login, "signed-in")
                counted("optout", "block")
            elif action == "allow":
                conn.execute("DELETE FROM optouts WHERE twitch_user_id = ?", (session.user_id,))
                counted("optout", "allow")
        finally:
            conn.close()
        return RedirectResponse("/optout", status_code=303)

    # --- reporting a problem -------------------------------------------------------------------

    @app.get("/report", response_class=HTMLResponse)
    def report_page(request: Request, run: str = ""):
        return page(request, "report.html", run_id=run if store.RUN_ID.match(run or "") else "",
                    reasons=REASONS, sent=False)  # fmt: skip

    @app.post("/report", response_class=HTMLResponse)
    def report_send(request: Request, run: str = Form(""), reason: str = Form("other"),
                    details: str = Form(""), contact: str = Form("")):  # fmt: skip
        conn = store.open_store(settings)
        try:
            conn.execute(
                "INSERT INTO reports (run_id, reason, details, contact, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run if store.RUN_ID.match(run or "") else None,
                 reason if reason in REASONS else "other", details.strip()[:4000] or None,
                 contact.strip()[:200] or None, to_rfc3339(utc_now())),
            )  # fmt: skip
        finally:
            conn.close()
        counted("report", reason if reason in REASONS else "other")
        return page(request, "report.html", run_id="", reasons=REASONS, sent=True)
