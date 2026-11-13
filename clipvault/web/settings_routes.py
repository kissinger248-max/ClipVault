"""Settings, the setup wizard (SPEC §15 steps 1-4 and 7) and the phone page."""

from __future__ import annotations

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
WIZARD_STEPS = 5  # mode, Twitch, streamers, storage, phone


def estimate_gb_per_month(streamers: list[dict], quality: str) -> float:
    """Rough disk use per month: each day's top clips plus chat moments from one stream a day."""
    per_day = 0.0
    for streamer in streamers:
        if not streamer["save_enabled"]:
            continue
        clips = streamer["top_n"] if streamer["min_views"] is None else 5
        per_day += clips * CLIP_MB[quality] + streamer["vod_moments"] * MOMENT_MB[quality]
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
        return page(request, "settings.html", tab="settings", config=current, keys=keys,
                    estimate=estimate, **phone_info(current))  # fmt: skip

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
    ):
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

    # --- setup wizard -------------------------------------------------------------------

    @app.get("/setup/{step}", response_class=HTMLResponse)
    def wizard(request: Request, step: int):
        step = min(max(step, 1), WIZARD_STEPS)
        current = config()
        streamers = streamer_rows()
        keys = cfg.get_secret("twitch_client_id") and cfg.get_secret("twitch_client_secret")
        return page(request, "setup.html", step=step, steps=WIZARD_STEPS, config=current,
                    streamers=streamers, has_keys=bool(keys),
                    estimate=estimate_gb_per_month(streamers, current.quality),
                    **phone_info(current))  # fmt: skip

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
