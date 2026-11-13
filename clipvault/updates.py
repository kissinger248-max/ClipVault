"""Updates: find a newer ClipVault on GitHub, announce it, download it, install it.

- **Check:** the latest GitHub release (``releases/latest``) at start and then every
  6 hours, unless switched off. ``CLIPVAULT_UPDATE_FEED`` points the check at another
  address with the same JSON shape (used to test updates without publishing).
- **Announce:** a card on Home and in Settings, a tray menu item, and one phone
  notification per version (if ntfy is set up).
- **Download:** the installer for this system, checked against the release's
  ``SHA256SUMS.txt`` before it is used. Kept in ``<home>/updates`` (files the app made).
- **Install** (installed app only):
  - Windows runs the Inno Setup installer silently. It replaces the program and
    starts ClipVault again.
  - On macOS a small script waits for the app to quit, swaps ``ClipVault.app`` for
    the new one from the disk image, and opens it.
- **When:** automatic installs happen at a quiet moment (in the first minutes after
  the app starts, or during the notification quiet hours), unless switched off in
  Settings. "Update Now" installs at once.

Every job can be interrupted safely, so quitting for an update loses nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from clipvault import __version__, alerts, lifecycle
from clipvault import config as cfg
from clipvault.logging_setup import ctx
from clipvault.timeutil import parse_rfc3339, to_rfc3339, utc_now

REPO = "kissinger248-max/ClipVault"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
FEED_ENV = "CLIPVAULT_UPDATE_FEED"
OFF_ENV = "CLIPVAULT_NO_UPDATES"  # test copies: never check, download or install
ASSETS = {"win32": "ClipVault-Setup-Windows.exe", "darwin": "ClipVault-macOS.dmg"}
SUMS_NAME = "SHA256SUMS.txt"
CHECK_EVERY = timedelta(hours=6)
STARTUP_WINDOW = timedelta(minutes=15)
HTTP_TIMEOUT = httpx.Timeout(30.0, read=300.0)

# settings table keys
LATEST = "update_latest"  # JSON of the newest release found (only when newer)
CHECKED = "update_checked_at"
ERROR = "update_error"
ANNOUNCED = "update_announced"  # version already announced on the phone
AUTO = "update_auto"  # "0" = don't install automatically (default: on)
CHECKS = "update_checks"  # "0" = don't check at all (default: on)
READY = "update_ready"  # JSON {version, path} of a downloaded, verified installer
STATE = "update_state"  # "", "downloading", "installing", or an error message
INSTALLED_FROM = "update_installed_from"  # set just before installing: {"from", "to"}

log = logging.getLogger(__name__)
JOB = "updates"


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    url: str
    notes: str
    published: str
    asset_name: str
    asset_url: str
    asset_size: int
    sums_url: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str | None) -> Release | None:
        if not text:
            return None
        try:
            return cls(**json.loads(text))
        except (ValueError, TypeError):
            return None


class UpdateError(Exception):
    """An update could not be downloaded, checked or started."""


# --- versions ----------------------------------------------------------------------


def version_tuple(text: str) -> tuple[int, ...]:
    """``v0.10.2`` -> (0, 10, 2). Anything after the numbers (``-beta``) is ignored."""
    match = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(n) for n in match.group(1).split(".")) if match else ()


def is_newer(candidate: str, current: str = __version__) -> bool:
    return version_tuple(candidate) > version_tuple(current)


def asset_name() -> str | None:
    """The installer this computer needs, or None on systems without one."""
    return ASSETS.get(sys.platform)


# --- checking ----------------------------------------------------------------------


def enabled() -> bool:
    return not os.environ.get(OFF_ENV)


def fetch_latest(http: httpx.Client) -> Release | None:
    """The latest published release, or None when it has no installer for this system."""
    response = http.get(os.environ.get(FEED_ENV) or RELEASES_API,
                        headers={"Accept": "application/vnd.github+json"})  # fmt: skip
    response.raise_for_status()
    body = response.json()
    if body.get("draft") or body.get("prerelease"):
        return None
    assets = {a["name"]: a for a in body.get("assets", [])}
    wanted = asset_name()
    if not wanted or wanted not in assets or SUMS_NAME not in assets:
        return None
    tag = body.get("tag_name", "")
    return Release(
        version=".".join(str(n) for n in version_tuple(tag)),
        tag=tag,
        url=body.get("html_url") or RELEASES_PAGE,
        notes=(body.get("body") or "").strip()[:2000],
        published=body.get("published_at") or "",
        asset_name=wanted,
        asset_url=assets[wanted]["browser_download_url"],
        asset_size=int(assets[wanted].get("size") or 0),
        sums_url=assets[SUMS_NAME]["browser_download_url"],
    )


def check(conn: sqlite3.Connection, http: httpx.Client, *, now: datetime | None = None
          ) -> Release | None:  # fmt: skip
    """Look for a newer release now; remember it (or forget an old one). Returns it."""
    now = now or utc_now()
    alerts.set_setting(conn, CHECKED, to_rfc3339(now))
    try:
        release = fetch_latest(http)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        alerts.set_setting(conn, ERROR, f"{exc.__class__.__name__}: {exc}"[:300])
        log.info("update check failed: %s", exc, extra=ctx(job=JOB))
        return available(conn)
    alerts.set_setting(conn, ERROR, "")
    if release and is_newer(release.version):
        alerts.set_setting(conn, LATEST, release.to_json())
        return release
    alerts.set_setting(conn, LATEST, "")
    return None


def check_due(conn: sqlite3.Connection, now: datetime) -> bool:
    last = alerts.get_setting(conn, CHECKED)
    return not last or now - parse_rfc3339(last) >= CHECK_EVERY


def available(conn: sqlite3.Connection) -> Release | None:
    """The newer release found by the last check, if it is still newer than this copy."""
    release = Release.from_json(alerts.get_setting(conn, LATEST))
    return release if release and is_newer(release.version) else None


def auto_install(conn: sqlite3.Connection) -> bool:
    return alerts.get_setting(conn, AUTO) != "0"


def checks_on(conn: sqlite3.Connection) -> bool:
    return alerts.get_setting(conn, CHECKS) != "0"


# --- installing --------------------------------------------------------------------


def app_bundle() -> Path | None:
    """macOS: the ``ClipVault.app`` folder this copy runs from."""
    if sys.platform != "darwin":
        return None
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


def can_install() -> bool:
    """Whether this copy can update itself (the installed app, in a place it may change)."""
    if not (cfg.is_packaged() and enabled() and asset_name()):
        return False
    if sys.platform == "darwin":
        bundle = app_bundle()
        return bundle is not None and os.access(bundle.parent, os.W_OK)
    return True


def downloads_dir() -> Path:
    return cfg.home_dir() / "updates"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def expected_sha256(http: httpx.Client, release: Release) -> str:
    response = http.get(release.sums_url, follow_redirects=True)
    response.raise_for_status()
    for line in response.text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == release.asset_name:
            return parts[0].lower()
    raise UpdateError(f"{SUMS_NAME} has no checksum for {release.asset_name}")


def ready_installer(conn: sqlite3.Connection, release: Release) -> Path | None:
    """The downloaded and checked installer for ``release``, if it is still there."""
    try:
        ready = json.loads(alerts.get_setting(conn, READY) or "{}")
    except ValueError:
        return None
    path = Path(ready.get("path", ""))
    if ready.get("version") == release.version and path.is_file():
        return path
    return None


def download(conn: sqlite3.Connection, release: Release, http: httpx.Client) -> Path:
    """Download the installer once and check its SHA-256 against the release's list."""
    existing = ready_installer(conn, release)
    if existing:
        return existing
    folder = downloads_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("ClipVault-*"):  # earlier updates (only files the app made)
        old.unlink(missing_ok=True)
    expected = expected_sha256(http, release)
    target = folder / f"ClipVault-{release.version}-{release.asset_name.split('-', 1)[1]}"
    partial = target.with_name(target.name + ".part")
    with http.stream("GET", release.asset_url, follow_redirects=True) as response:
        response.raise_for_status()
        with partial.open("wb") as handle:
            for chunk in response.iter_bytes(1 << 20):
                handle.write(chunk)
    if _sha256(partial) != expected:
        partial.unlink(missing_ok=True)
        raise UpdateError("the downloaded installer doesn't match its published checksum")
    partial.replace(target)
    alerts.set_setting(conn, READY, json.dumps({"version": release.version, "path": str(target)}))
    log.info("update %s downloaded and checked", release.version, extra=ctx(job=JOB))
    return target


MAC_SWAP = """#!/bin/sh
# ClipVault update: wait for the old app to quit, swap in the new one, open it.
PID="$1"; DMG="$2"; APP="$3"
while kill -0 "$PID" 2>/dev/null; do sleep 1; done
MNT="$(mktemp -d)"
hdiutil attach -nobrowse -noverify -noautoopen -mountpoint "$MNT" "$DMG" || { open "$APP"; exit 1; }
if ditto "$MNT/ClipVault.app" "$APP.updating"; then
  rm -rf "$APP.old" && mv "$APP" "$APP.old" && mv "$APP.updating" "$APP" && rm -rf "$APP.old"
fi
hdiutil detach "$MNT" -quiet
xattr -dr com.apple.quarantine "$APP" 2>/dev/null
open "$APP" --args run --no-browser
"""


def launch_installer(installer: Path) -> None:
    """Start the installer as its own process, so it outlives this one."""
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                         creationflags=flags, close_fds=True)  # fmt: skip
    elif sys.platform == "darwin":
        bundle = app_bundle()
        if bundle is None:
            raise UpdateError("can't tell where ClipVault.app is")
        script = downloads_dir() / "install-update.sh"
        script.write_text(MAC_SWAP, encoding="utf-8")
        subprocess.Popen(["/bin/sh", str(script), str(os.getpid()), str(installer), str(bundle)],
                         start_new_session=True, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    else:
        raise UpdateError("updates install by themselves only on Windows and macOS")


def install(conn: sqlite3.Connection, release: Release, installer: Path) -> None:
    """Hand over to the installer and quit, so it can replace the program."""
    alerts.set_setting(conn, STATE, "installing")
    marker = {"from": __version__, "to": release.version, "at": to_rfc3339(utc_now())}
    alerts.set_setting(conn, INSTALLED_FROM, json.dumps(marker))
    conn.commit()
    launch_installer(installer)
    log.info("installing update %s; ClipVault quits now", release.version, extra=ctx(job=JOB))
    if not lifecycle.request_quit():
        log.warning("no running app to quit for the update", extra=ctx(job=JOB))


def quiet_moment(config: cfg.Config, now: datetime) -> bool:
    """Right after the app started, or during the notification quiet hours."""
    from clipvault.notify import ntfy

    if now - lifecycle.started_at <= STARTUP_WINDOW:
        return True
    return ntfy.quiet_until(now, config) is not None


# --- the scheduled step ------------------------------------------------------------


def announce(conn: sqlite3.Connection, config: cfg.Config, release: Release, auto: bool) -> None:
    """One phone notification per new version."""
    if alerts.get_setting(conn, ANNOUNCED) == release.version:
        return
    from clipvault.notify import ntfy

    when = ("It installs by itself at a quiet moment, or choose Update Now in Settings."
            if auto and can_install() else "Download it from the release page.")  # fmt: skip
    title = f"ClipVault {release.version} is available"
    message = f"You have {__version__}. {when}"
    ntfy.queue(conn, config, "update", title, message, click=release.url, tags=("sparkles",))
    alerts.set_setting(conn, ANNOUNCED, release.version)


def step_updates(conn: sqlite3.Connection, config: cfg.Config, now: datetime,
                 http: httpx.Client | None = None) -> str:  # fmt: skip
    """Check (every 6 hours), announce, download, and install at a quiet moment."""
    if not enabled() or not checks_on(conn):
        return ""
    own = http is None
    http = http or httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": "ClipVault"})
    try:
        release = check(conn, http, now=now) if check_due(conn, now) else available(conn)
        if release is None:
            return ""
        auto = auto_install(conn)
        announce(conn, config, release, auto)
        if not (auto and can_install()):
            return f"ClipVault {release.version} is available"
        installer = download(conn, release, http)
        if quiet_moment(config, now):
            install(conn, release, installer)
            return f"installing ClipVault {release.version}"
        return f"ClipVault {release.version} downloaded; installs at a quiet moment"
    finally:
        if own:
            http.close()


# --- "Update Now" ------------------------------------------------------------------

_now_lock = threading.Lock()


def update_now_in_background(db_path: Path) -> bool:
    """Download (if needed) and install the available update on a background thread.

    Returns False when there is nothing to install or an update is already under way.
    """
    from clipvault import db

    if not _now_lock.acquire(blocking=False):
        return False

    def work() -> None:
        conn = db.open_db(db_path)
        try:
            release = available(conn)
            if release is None or not can_install():
                return
            alerts.set_setting(conn, STATE, "downloading")
            with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": "ClipVault"}) as http:
                installer = download(conn, release, http)
            install(conn, release, installer)
        except Exception as exc:  # shown in the panel; the app keeps running
            alerts.set_setting(conn, STATE, f"Update failed: {exc}"[:300])
            log.warning("update failed: %s", exc, extra=ctx(job=JOB))
        finally:
            conn.close()
            _now_lock.release()

    threading.Thread(target=work, name="update-now", daemon=True).start()
    return True


def just_updated(conn: sqlite3.Connection, now: datetime | None = None) -> str | None:
    """The version this copy was updated to, for two days after the update."""
    try:
        record = json.loads(alerts.get_setting(conn, INSTALLED_FROM) or "{}")
        when = parse_rfc3339(record["at"])
    except (ValueError, KeyError, TypeError):
        return None
    if record.get("to") == __version__ and (now or utc_now()) - when <= timedelta(days=2):
        return record["to"]
    return None


def after_start(conn: sqlite3.Connection) -> None:
    """Tidy up after an update: clear the state, the downloaded installer and the marker."""
    if alerts.get_setting(conn, STATE) == "installing":
        alerts.set_setting(conn, STATE, "")
    ready = alerts.get_setting(conn, READY)
    if ready and just_updated(conn):
        for old in downloads_dir().glob("ClipVault-*"):
            old.unlink(missing_ok=True)
        alerts.set_setting(conn, READY, "")
