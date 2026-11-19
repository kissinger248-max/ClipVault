"""The website's settings, read from environment variables (no config file, no keychain).

On a server, secrets come from the host's secret store as environment variables at
start; they are never written to a file or to git (hard rule 6). Every name starts with
``CLIPVAULT_WEB_``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_DOWNLOAD_URL = "https://clipvault-site-six.vercel.app/#download"


def _int(name: str, default: int) -> int:
    raw = os.environ.get(f"CLIPVAULT_WEB_{name}", "").strip()
    return int(raw) if raw.isdigit() else default


@dataclass(frozen=True)
class WebSettings:
    """Everything the website can be told at start."""

    data: Path  # runs, uploads in progress, the database and logs
    clips_per_run: int = 5
    max_upload_mb: int = 2048
    max_minutes: int = 60
    keep_hours: int = 24
    workers: int = 1  # runs made at the same time
    render_threads: int = 5  # phone versions of one run made at the same time
    app_download_url: str = APP_DOWNLOAD_URL
    groq_api_key: str | None = field(default=None, repr=False)
    # The website's OWN Twitch app (never the desktop app's keys: plan section 5, item 1).
    twitch_client_id: str | None = None
    twitch_client_secret: str | None = field(default=None, repr=False)
    # Where people reach the website; Twitch sends them back here after signing in, so the
    # website's Twitch app must list <public_url>/auth/twitch/callback exactly. Twitch takes
    # plain http only for localhost, hence localhost (not 127.0.0.1) on this computer.
    public_url: str = "http://localhost:8800"
    downloads_default: bool = False  # server downloads of other channels (the switch's start)
    contact_email: str = ""  # takedowns and problem reports
    # W4 limits
    runs_per_day: int = 3  # per visitor without sign-in
    runs_per_day_signed_in: int = 5
    max_queue: int = 30  # waiting runs before new ones are turned away ("busy")
    one_run_at_a_time: bool = True  # per visitor
    turnstile_site_key: str = ""
    turnstile_secret: str | None = field(default=None, repr=False)

    @property
    def db_path(self) -> Path:
        return self.data / "web.db"

    @property
    def runs_dir(self) -> Path:
        return self.data / "runs"

    @property
    def incoming_dir(self) -> Path:
        return self.data / "incoming"

    @property
    def logs_dir(self) -> Path:
        return self.data / "logs"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def from_env(data: Path | None = None) -> WebSettings:
    """Settings from ``CLIPVAULT_WEB_*`` variables; ``data`` overrides ``CLIPVAULT_WEB_DATA``."""
    folder = data or Path(os.environ.get("CLIPVAULT_WEB_DATA") or "web-data")
    return WebSettings(
        data=folder.expanduser().resolve(),
        clips_per_run=_int("CLIPS_PER_RUN", 5),
        max_upload_mb=_int("MAX_UPLOAD_MB", 2048),
        max_minutes=_int("MAX_MINUTES", 60),
        keep_hours=_int("KEEP_HOURS", 24),
        workers=max(_int("WORKERS", 1), 1),
        render_threads=max(_int("RENDER_THREADS", 5), 1),
        app_download_url=os.environ.get("CLIPVAULT_WEB_APP_URL") or APP_DOWNLOAD_URL,
        groq_api_key=os.environ.get("CLIPVAULT_WEB_GROQ_API_KEY") or None,
        twitch_client_id=os.environ.get("CLIPVAULT_WEB_TWITCH_CLIENT_ID") or None,
        twitch_client_secret=os.environ.get("CLIPVAULT_WEB_TWITCH_CLIENT_SECRET") or None,
        public_url=(os.environ.get("CLIPVAULT_WEB_PUBLIC_URL") or "http://localhost:8800").rstrip(
            "/"
        ),
        downloads_default=os.environ.get("CLIPVAULT_WEB_TWITCH_DOWNLOADS", "").lower() == "on",
        contact_email=os.environ.get("CLIPVAULT_WEB_CONTACT", ""),
        runs_per_day=_int("RUNS_PER_DAY", 3),
        runs_per_day_signed_in=_int("RUNS_PER_DAY_SIGNED_IN", 5),
        max_queue=_int("MAX_QUEUE", 30),
        one_run_at_a_time=os.environ.get("CLIPVAULT_WEB_ONE_AT_A_TIME", "on") != "off",
        turnstile_site_key=os.environ.get("CLIPVAULT_WEB_TURNSTILE_SITE_KEY", ""),
        turnstile_secret=os.environ.get("CLIPVAULT_WEB_TURNSTILE_SECRET") or None,
    )
