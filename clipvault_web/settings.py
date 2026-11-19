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
    )
