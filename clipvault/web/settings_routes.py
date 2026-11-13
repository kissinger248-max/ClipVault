"""Settings, the setup wizard (SPEC §15 steps 1-4 and 7) and the phone page."""

from __future__ import annotations

import re
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse

from clipvault import alerts
from clipvault import config as cfg
from clipvault.web import auth

# Measured in M2 and M4 on real clips (MB): a 30 s clip, and a 40 s chat moment cut from a VOD.
CLIP_MB = {"720p": 5.3, "1080p": 9.6}
MOMENT_MB = {"720p": 16.0, "1080p": 30.0}
VERTICAL_MB = 36.0  # the phone version of a clip (1080x1920 at 10 Mbit/s, measured in M7)
WIZARD_STEPS = 6  # mode, Twitch, streamers, storage, look, phone
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def estimate_gb_per_month(streamers: list[dict], quality: str) -> float:
    """Rough disk use per month: each day's top clips, chat moments from one stream a day,
    and the phone version of each."""
    per_day = 0.0
    for streamer in streamers:
        if not streamer["save_enabled"]:
            continue
        clips = streamer["top_n"] if streamer["min_views"] is None else 5
        moments = streamer["vod_moments"]
        per_day += clips * CLIP_MB[quality] + moments * MOMENT_MB[quality]
        per_day += (clips + moments) * VERTICAL_MB  # every clip also gets its phone version
    return per_day * 30 / 1024


def lan_address() -> str | None:
    """This computer's address on the local network (no packet is sent), or None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # documentation-only address; UDP connect sends nothing
            return probe.getsockname()[0]
    except OSError:
        return None


def qr_svg(text: str) -> str:
    """A QR code as inline SVG."""
    import qrcode
    import qrcode.image.svg

    image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    return image.to_string(encoding="unicode")


def register(
    app: FastAPI,
    *,
    page: Callable[..., HTMLResponse],
    back: Callable[..., Any],
    conn: Callable[[], Any],
    config: Callable[[], cfg.Config],
    home: Path,
) -> None:
    """Add the settings, wizard and phone routes to the app."""

    def streamer_rows() -> list[dict]:
        c = conn()
        try:
            return [dict(r) for r in c.execute("SELECT * FROM streamers ORDER BY login")]
        finally:
            c.close()

    def save(updates: dict[str, Any], target: str, message: str):
        try:
            cfg.save_settings(updates, home / cfg.CONFIG_FILENAME)
        except cfg.ConfigError as exc:
            return back(target, f"Not saved: {exc}")
        return back(target, message)

    def folder_info(current: cfg.Config) -> dict[str, Any]:
        """Where the library is, in words ("in your user folder"), and its free space."""
        import shutil

        root = current.library_root
        try:
            inside_home = root.resolve().is_relative_to(Path.home().resolve())
        except (OSError, ValueError):
            inside_home = False
        probe = root if root.exists() else root.parent
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            free = None
        return {"folder_home": inside_home, "folder_free": free}

    def phone_info(current: cfg.Config) -> dict[str, Any]:
        from clipvault.notify import ntfy

        topic = ntfy.get_topic()
        address = lan_address()
        reachable = current.panel.host not in ("127.0.0.1", "localhost")
        panel_url = f"http://{address}:{current.panel.port}" if address else ""
        return {
            "topic": topic,
            "ntfy_url": ntfy.subscribe_url(current, topic) if topic else "",
            "ntfy_qr": qr_svg(ntfy.subscribe_url(current, topic)) if topic else "",
            "panel_url": panel_url,
            "panel_qr": qr_svg(panel_url) if panel_url and reachable else "",
            "reachable": reachable,
        }

    # --- settings -----------------------------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        current = config()
        keys = {name: bool(cfg.get_secret(name)) for name in cfg.USER_SECRET_NAMES}
        estimate = estimate_gb_per_month(streamer_rows(), current.quality)
        from clipvault import autostart
        from clipvault.twitch import user_auth

        token = user_auth.stored()
        starts_at_login = autostart.is_enabled() if autostart.supported() else None
        return page(request, "settings.html", tab="settings", config=current, keys=keys,
                    starts_at_login=starts_at_login,
                    estimate=estimate, twitch_user=(token.login or "your account") if token else "",
                    twitch_login=app.state.panel.twitch_login, sign_in_mode=sign_in_mode(),
                    **phone_info(current), **folder_info(current))  # fmt: skip

    @app.post("/settings/storage")
    def save_storage(
        library_root: str = Form(...),
        quality: str = Form("720p"),
        keep_days: str = Form("60"),
        min_free: str = Form("15"),
        next: str = Form("/settings"),
    ):
        try:
            updates = {
                "library_root": library_root.strip(),
                "quality": quality,
                "retention.keep_days_below_median": int(keep_days),
                "retention.min_free_space_pct": int(min_free),
            }
        except ValueError:
            return back(next, "Not saved: the numbers must be whole numbers.")
        if not library_root.strip():
            return back(next, "Not saved: choose a folder for the library.")
        return save(updates, next, "Storage settings saved.")

    @app.post("/settings/schedule")
    def save_schedule(
        interval: str = Form("30"),
        battery: str = Form(""),
        transcripts: str = Form(""),
        timezone: str = Form("America/Chicago"),
        autostart_on: str = Form(""),
    ):
        from clipvault import autostart

        if autostart.supported() and autostart.is_enabled() != bool(autostart_on):
            try:
                autostart.set_enabled(bool(autostart_on))
            except OSError as exc:
                return back("/settings", f"Could not change starting at login: {exc}")
        try:
            minutes = int(interval)
        except ValueError:
            return back("/settings", "Not saved: the interval must be a whole number of minutes.")
        updates = {
            "discover_interval_min": minutes,
            "heavy_jobs_on_battery": bool(battery),
            "transcription.enabled": bool(transcripts),
            "timezone": timezone.strip(),
        }
        return save(updates, "/settings", "Saved. A running ClipVault picks this up on restart.")

    @app.post("/settings/notifications")
    def save_notifications(
        quiet_from: str = Form("23:00"), quiet_to: str = Form("08:00"), summary: str = Form("09:00")
    ):
        updates = {
            "notifications.quiet_hours": [quiet_from, quiet_to],
            "notifications.summary_time": summary,
        }
        return save(updates, "/settings#phone", "Notification times saved.")

    @app.post("/settings/ntfy")
    def setup_ntfy(next: str = Form("/settings#phone")):
        from clipvault.notify import ntfy

        ntfy.ensure_topic()
        return back(next, "Topic made. Scan the code with the phone, then send a test.")

    @app.post("/settings/ntfy-test")
    def test_ntfy(next: str = Form("/settings#phone")):
        from clipvault.notify import ntfy

        c = conn()
        try:
            current = config()
            ntfy.queue(c, current, "test", "ClipVault test",
                       "Notifications work. Nothing is wrong.", priority=3)  # fmt: skip
            result = ntfy.flush(c, current)
        finally:
            c.close()
        if result.failed or not result.sent:
            return back(next, "Could not send; is this computer online?")
        return back(next, "Test sent; it should reach the phone within a minute.")

    @app.post("/settings/phone-access")
    def phone_access(allow: str = Form(""), next: str = Form("/settings#phone")):
        host = "0.0.0.0" if allow else "127.0.0.1"
        message = (
            "Saved. Restart ClipVault, then open the address below on the phone (same Wi-Fi)."
            if allow
            else "Saved. After a restart only this computer can open the panel."
        )
        return save({"panel.host": host}, next, message)

    @app.post("/settings/twitch")
    def save_twitch(
        client_id: str = Form(""), client_secret: str = Form(""), next: str = Form("/settings")
    ):
        from clipvault.secret_prompt import clean_secret

        stored = []
        for name, value in (
            ("twitch_client_id", client_id),
            ("twitch_client_secret", client_secret),
        ):
            cleaned = clean_secret(value)
            if cleaned:
                cfg.set_secret(name, cleaned)
                stored.append(name)
        if stored:
            cfg.delete_secret("twitch_app_token")  # a cached token belongs to the old keys
        return back(next, _check_twitch() if stored else "Nothing entered; nothing changed.")

    @app.post("/settings/password")
    def change_password(
        request: Request, current: str = Form(...), new: str = Form(...), again: str = Form(...)
    ):
        if not auth.check_password(current):
            return back("/settings#password", "The current password is wrong.")
        if new != again:
            return back("/settings#password", "The two new passwords are different.")
        try:
            auth.set_password(new)
        except ValueError as exc:
            return back("/settings#password", str(exc).capitalize() + ".")
        return back("/login", "Password changed. Log in again (other devices are logged out too).")

    # --- Twitch login for live chat ---------------------------------------------------------

    @app.post("/settings/twitch-login")
    def twitch_login_start(next: str = Form("/settings#twitch-login")):
        if not next.startswith("/") or next.startswith("//"):
            next = "/settings#twitch-login"  # only ever back into the panel
        import threading

        import httpx

        from clipvault.twitch import user_auth

        state = app.state.panel
        http = httpx.Client(timeout=15.0)
        try:
            login = user_auth.start_login(http)
        except (user_auth.LoginNeeded, httpx.HTTPError) as exc:
            http.close()
            return back(next, f"Can't start the sign-in: {exc}")
        state.twitch_login = {"code": login.user_code, "url": login.verification_uri,
                              "error": ""}  # fmt: skip

        def wait() -> None:
            try:
                user_auth.finish_login(http, login)
                state.twitch_login = None
            except Exception as exc:  # shown on the page
                state.twitch_login = {**state.twitch_login, "error": str(exc)}
            finally:
                http.close()

        threading.Thread(target=wait, name="twitch-login", daemon=True).start()
        return back(next)

    @app.post("/settings/twitch-logout")
    def twitch_logout():
        from clipvault.twitch import user_auth

        user_auth.logout()
        if sign_in_mode():
            message = "Signed out of Twitch. Sign in again to keep finding clips."
            return back("/settings#twitch-login", message)
        return back("/settings#twitch-login", "Logged out of Twitch. Live chat is no longer read.")

    # --- look editor ----------------------------------------------------------------------

    @app.get("/look/preview/{layout}.jpg")
    def look_preview(
        layout: str, style: str = "", accent: str = "", size: int = 0, credit: str = ""
    ):
        import dataclasses
        import hashlib

        from fastapi.responses import FileResponse

        from clipvault.render import engine, layouts
        from clipvault.render.ffmpeg import RenderError
        from clipvault.render.template import load_look

        fallback = FileResponse(Path(__file__).parent / "static" / "no-thumb.svg",
                                media_type="image/svg+xml")  # fmt: skip
        if layout not in (layouts.LETTERBOX, layouts.SPLIT, layouts.NATIVE):
            return fallback
        current = config()
        wanted = dataclasses.replace(
            current.look,
            headline_style=style if style in ("box", "outline") else current.look.headline_style,
            accent=accent if _COLOR.match(accent or "") else current.look.accent,
            caption_size=size if 56 <= size <= 96 else current.look.caption_size,
            show_credit=credit == "1" if credit in ("0", "1") else current.look.show_credit,
        )
        look = load_look(dataclasses.replace(current, look=wanted))
        c = conn()
        try:
            clip = engine.preview_clip(c, layout)
            if clip is None:
                return fallback
            key = f"{layout}|{clip['id']}|{wanted}|{clip['headline']}".encode()
            dest = home / "cache" / "look" / f"{hashlib.sha1(key).hexdigest()[:16]}.jpg"
            if not dest.is_file():
                try:
                    engine.preview_frame(c, current, look, layout, dest)
                except RenderError:
                    return fallback
        finally:
            c.close()
        return FileResponse(dest, media_type="image/jpeg")

    @app.post("/settings/look")
    def save_look(
        style: str = Form("box"),
        accent: str = Form("#FFD400"),
        size: str = Form("72"),
        credit: str = Form(""),
        next: str = Form("/settings#look"),
    ):
        try:
            caption_size = int(size)
        except ValueError:
            return back(next, "Not saved: the caption size must be a number.")
        updates = {
            "look.headline_style": style,
            "look.accent": accent.upper(),
            "look.caption_size": caption_size,
            "look.show_credit": bool(credit),
        }
        return save(updates, next, "Look saved. New vertical copies use it.")

    # --- setup wizard -------------------------------------------------------------------

    @app.get("/setup/{step}", response_class=HTMLResponse)
    def wizard(request: Request, step: int):
        step = min(max(step, 1), WIZARD_STEPS)
        current = config()
        streamers = streamer_rows()
        keys = cfg.own_twitch_keys()
        from clipvault.twitch import user_auth

        token = user_auth.stored()
        return page(request, "setup.html", step=step, steps=WIZARD_STEPS, config=current,
                    streamers=streamers, has_keys=bool(keys), sign_in_mode=sign_in_mode(),
                    twitch_user=(token.login or "your account") if token else "",
                    twitch_login=app.state.panel.twitch_login,
                    estimate=estimate_gb_per_month(streamers, current.quality),
                    **phone_info(current), **folder_info(current))  # fmt: skip

    @app.post("/setup/1")
    def wizard_mode(mode: str = Form("archive")):
        if mode != "archive":
            return back("/setup/1", "Posting arrives in a later version; start with saving.")
        return save({"mode": "archive"}, "/setup/2", "")

    @app.post("/setup/done")
    def wizard_done():
        c = conn()
        try:
            alerts.set_setting(c, "setup_done", "1")
        finally:
            c.close()
        return back("/", "All set. ClipVault looks for clips on its own while it runs.")


def sign_in_mode() -> bool:
    """True when ClipVault connects through the user's Twitch sign-in (no own developer keys)."""
    from clipvault.twitch import user_auth

    return (
        not cfg.own_twitch_keys() and bool(cfg.bundled_twitch_client_id()) and user_auth.available()
    )


def _check_twitch() -> str:
    """Try the stored Twitch keys; returns a message for the user."""
    from clipvault import twitch

    try:
        client = twitch.build_client()
    except twitch.MissingCredentials:
        return "Saved. Both the client ID and the client secret are needed."
    try:
        client.get_users(logins=["twitch"])
    except twitch.TwitchAuthError:
        return "Saved, but Twitch refused these keys. Check them on dev.twitch.tv."
    except Exception as exc:
        return f"Saved, but Twitch couldn't be reached to check them ({exc.__class__.__name__})."
    finally:
        client.close()
    return "Twitch keys saved and working."
