"""Clip downloads with yt-dlp, for the private archive only (hard rule 3).

yt-dlp runs as a separate process rather than being imported. That way the daily
update takes effect without restarting ClipVault, a hung download can be killed on
a timeout, and a crash in yt-dlp can't take the app down.

From source it runs as ``python -m yt_dlp`` and updates with pip. The installed app
has no pip, so it uses yt-dlp's official standalone program instead: a copy ships in
its ``tools/`` folder, is copied to ``<home>/bin`` on first use (the app folder may
not be writable, and changing a signed Mac app would break it), and updates itself
there with ``yt-dlp -U``.

Checked against yt-dlp 2026.10.26 on a real clip: Twitch clips come as plain mp4
in 360/480/720/1080(/1440)p, plus ``portrait-*`` vertical versions for streamers
who use Dual Format.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from clipvault import config as cfg
from clipvault import procs, runs
from clipvault.logging_setup import ctx
from clipvault.timeutil import parse_rfc3339, to_rfc3339, utc_now

DOWNLOAD_TIMEOUT_S = 300
UPDATE_TIMEOUT_S = 300
UPDATE_EVERY = timedelta(hours=24)
UPDATE_JOB = "ytdlp_update"
# Error texts that mean "this clip is gone for good"; retrying can't help.
_PERMANENT_MARKERS = ("no longer available", "does not exist", "has been deleted", "http error 404")

Runner = Callable[..., Any]
log = logging.getLogger(__name__)


def _run(*args: Any, **kwargs: Any) -> Any:
    """The one place this module starts a process (tests replace it).

    A timeout ends the whole tree the tool started (``procs.run``).
    """
    return procs.run(*args, **kwargs)


class DownloadError(Exception):
    """A download failed. ``permanent`` means the clip itself is gone."""

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


def standalone_name() -> str:
    return "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"


def standalone_path() -> Path:
    """Where the installed app keeps its own updatable copy of yt-dlp."""
    return cfg.home_dir() / "bin" / standalone_name()


def ensure_standalone() -> Path:
    """The installed app's yt-dlp, copied from the bundled tools on first use."""
    target = standalone_path()
    if not target.exists():
        bundled = cfg.resource_dir() / "tools" / standalone_name()
        if not bundled.exists():
            raise DownloadError(f"yt-dlp is missing from the app ({bundled})")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundled, target)
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def ytdlp_command() -> list[str]:
    """Command prefix that runs yt-dlp (standalone in the installed app, else the module)."""
    if cfg.is_packaged():
        return [str(ensure_standalone())]
    return [sys.executable, "-m", "yt_dlp"]


def update_command() -> list[str]:
    """How yt-dlp is brought up to date: its own updater, or pip from source."""
    if cfg.is_packaged():
        return [*ytdlp_command(), "-U"]
    return [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "yt-dlp"]


def format_selector(quality: str) -> str:
    """yt-dlp format expression for the quality setting (landscape versions only).

    ``720p`` takes the best version up to 720p, or the smallest one if the clip
    only exists in something bigger. ``1080p`` does the same with a 1080p cap.
    """
    cap = 720 if quality == "720p" else 1080
    landscape = "[format_id!^=portrait]"
    return f"best[height<={cap}]{landscape}/worst{landscape}/best"


def download_clip(
    url: str,
    dest: Path,
    quality: str,
    *,
    runner: Runner | None = None,
    timeout: float = DOWNLOAD_TIMEOUT_S,
) -> Path:
    """Download the clip at ``url`` to ``dest`` (an ``.mp4`` path) and return ``dest``.

    yt-dlp writes to ``dest`` + ``.part`` and renames it when the download is
    complete, so a file at ``dest`` is always a whole file. Raises
    ``DownloadError`` on failure.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    template = str(dest.with_suffix("")).replace("%", "%%") + ".%(ext)s"
    command = [
        *ytdlp_command(),
        "--format",
        format_selector(quality),
        "--remux-video",
        "mp4",
        "--output",
        template,
        "--no-playlist",
        "--no-progress",
        "--no-warnings",
        "--no-mtime",
        "--no-overwrites",
        "--continue",
        "--retries",
        "3",
        "--socket-timeout",
        "20",
        url,
    ]
    try:
        result = (runner or _run)(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(f"download took longer than {timeout:.0f} s and was stopped") from exc
    except OSError as exc:
        raise DownloadError(f"could not start yt-dlp: {exc}") from exc

    if result.returncode != 0:
        message = _error_line(result.stderr) or f"yt-dlp exited with code {result.returncode}"
        permanent = any(marker in message.lower() for marker in _PERMANENT_MARKERS)
        raise DownloadError(message, permanent=permanent)
    if not dest.is_file() or dest.stat().st_size == 0:
        raise DownloadError("yt-dlp reported success but the file is missing or empty")
    return dest


def download_section(
    url: str,
    dest: Path,
    quality: str,
    start: float,
    end: float,
    *,
    runner: Runner | None = None,
    timeout: float = DOWNLOAD_TIMEOUT_S,
) -> Path:
    """Download only ``start``..``end`` seconds of a VOD (never the whole VOD, SPEC §6).

    yt-dlp fetches just the stream segments it needs and FFmpeg joins them, so
    the cut snaps to the nearest keyframe (Twitch: every 2 s); moments are padded
    enough for that. Raises ``DownloadError``.
    """
    if end <= start:
        raise DownloadError(f"empty section {start}-{end}", permanent=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    template = str(dest.with_suffix("")).replace("%", "%%") + ".%(ext)s"
    command = [
        *ytdlp_command(),
        "--format", format_selector(quality),
        "--download-sections", f"*{start:.0f}-{end:.0f}",
        "--remux-video", "mp4", "--output", template,
        "--no-playlist", "--no-progress", "--no-warnings", "--no-mtime", "--no-overwrites",
        "--retries", "3", "--socket-timeout", "20", url,
    ]  # fmt: skip
    try:
        result = (runner or _run)(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(f"cut took longer than {timeout:.0f} s and was stopped") from exc
    except OSError as exc:
        raise DownloadError(f"could not start yt-dlp: {exc}") from exc
    if result.returncode != 0:
        message = _error_line(result.stderr) or f"yt-dlp exited with code {result.returncode}"
        lowered = message.lower()
        permanent = any(marker in lowered for marker in _PERMANENT_MARKERS) or (
            "subscriber" in lowered or "sub-only" in lowered
        )
        raise DownloadError(message, permanent=permanent)
    if not dest.is_file() or dest.stat().st_size == 0:
        raise DownloadError("yt-dlp reported success but the file is missing or empty")
    return dest


def download_portrait(
    url: str, dest: Path, *, runner: Runner | None = None, timeout: float = DOWNLOAD_TIMEOUT_S
) -> Path | None:
    """Download Twitch's own vertical version of a clip; None when the clip has none.

    Only streamers using Twitch's Dual Format have one. It is the input for the
    "native" layout (SPEC 10.2).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    template = str(dest.with_suffix("")).replace("%", "%%") + ".%(ext)s"
    command = [
        *ytdlp_command(),
        "--format", "best[format_id^=portrait]",
        "--remux-video", "mp4", "--output", template,
        "--no-playlist", "--no-progress", "--no-warnings", "--no-mtime", "--no-overwrites",
        "--retries", "3", "--socket-timeout", "20", url,
    ]  # fmt: skip
    try:
        result = (runner or _run)(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise DownloadError(f"portrait download failed: {exc}") from exc
    if result.returncode != 0:
        message = _error_line(result.stderr)
        if "requested format is not available" in message.lower():
            return None
        raise DownloadError(message or "portrait download failed")
    return dest if dest.is_file() and dest.stat().st_size > 0 else None


def _error_line(stderr: str | None) -> str:
    """Pick the line that says what went wrong out of yt-dlp's error output."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("ERROR:")]
    chosen = errors[-1] if errors else (lines[-1] if lines else "")
    return chosen.removeprefix("ERROR:").strip()[:500]


def update_if_due(
    conn: sqlite3.Connection, *, now: datetime | None = None, runner: Runner | None = None
) -> bool:
    """Update yt-dlp if the last attempt was over a day ago; True if it ran.

    Twitch changes break old yt-dlp versions regularly, so staying current is
    what keeps downloads working (SPEC §6). A failed update is logged and tried
    again the next day; it never stops downloads.
    """
    now = now or utc_now()
    last = runs.get_cursor(conn, UPDATE_JOB)
    if last and now - parse_rfc3339(last) < UPDATE_EVERY:
        return False
    runs.mark_started(conn, UPDATE_JOB)
    try:
        command = update_command()
        result = (runner or _run)(command, capture_output=True, text=True, timeout=UPDATE_TIMEOUT_S)
        failed = result.stderr or result.stdout or "update failed"
        error = "" if result.returncode == 0 else failed.strip()[-500:]
    except (subprocess.TimeoutExpired, OSError, DownloadError) as exc:
        error = str(exc)
    if error:
        log.warning("yt-dlp update failed: %s", error, extra=ctx(job=UPDATE_JOB))
        runs.mark_error(conn, UPDATE_JOB, error)
        # Still move the cursor: one attempt a day, not one before every download run.
        conn.execute("UPDATE runs SET cursor = ? WHERE job = ?", (to_rfc3339(now), UPDATE_JOB))
    else:
        runs.mark_success(conn, UPDATE_JOB, cursor=to_rfc3339(now))
        log.info("yt-dlp is up to date", extra=ctx(job=UPDATE_JOB))
    return True
