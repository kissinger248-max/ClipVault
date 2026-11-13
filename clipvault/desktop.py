"""Start-up for the installed app (the PyInstaller build's entry point).

Double-clicking ClipVault runs ``clipvault run``: scheduler, control panel, tray
icon, and the panel opens in the browser. Opening it again while it runs just shows
the panel. The build has no console window, so:

- output that would go to a console goes nowhere (everything is in the log files);
- the bundled tools (FFmpeg, ffprobe, TwitchDownloaderCLI) in ``tools/`` come first
  on PATH, so the same code finds them as on a developer's machine;
- on Windows, the tools it starts get no console window of their own either.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys

from clipvault import config as cfg
from clipvault.logging_setup import ctx

log = logging.getLogger(__name__)
TIMEZONE_SETTING = "timezone_set"


def _prepare_stdio() -> None:
    """A windowed app has no stdout/stderr; give it somewhere harmless to write."""
    for name in ("stdout", "stderr", "stdin"):
        if getattr(sys, name) is None:
            mode = "r" if name == "stdin" else "w"
            setattr(sys, name, open(os.devnull, mode, encoding="utf-8"))  # noqa: SIM115


def bundled_tools_dir() -> str:
    return str(cfg.resource_dir() / "tools")


def _add_bundled_tools() -> None:
    tools = bundled_tools_dir()
    if os.path.isdir(tools):
        os.environ["PATH"] = tools + os.pathsep + os.environ.get("PATH", "")


def _hide_child_console_windows() -> None:
    """Without this every FFmpeg or yt-dlp step would flash a black window on Windows."""
    original = subprocess.Popen.__init__
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    def quiet_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["creationflags"] = (kwargs.get("creationflags") or 0) | no_window
        if kwargs.get("stdin") is None and "input" not in kwargs:
            kwargs["stdin"] = subprocess.DEVNULL  # there is no console to read from
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = quiet_init  # type: ignore[method-assign]


def local_timezone() -> str | None:
    """The computer's time zone as an IANA name (``Europe/Berlin``), when it can be told."""
    try:
        import tzlocal

        name = tzlocal.get_localzone_name()
    except Exception:  # unknown zone or missing data: keep the default
        return None
    return name if name and "/" in name or name == "UTC" else None


def first_run(conn: sqlite3.Connection) -> None:
    """One-time set-up of the installed app: start at login, and the computer's time zone."""
    from clipvault import alerts, autostart

    autostart.first_run(conn)
    if not cfg.is_packaged() or alerts.get_setting(conn, TIMEZONE_SETTING):
        return
    zone = local_timezone()
    if zone:
        try:
            cfg.save_settings({"timezone": zone})
        except cfg.ConfigError as exc:
            log.warning("time zone %s not used: %s", zone, exc, extra=ctx(job="first_run"))
    alerts.set_setting(conn, TIMEZONE_SETTING, "1")


def main() -> None:
    """Entry point of the installed app."""
    import multiprocessing

    multiprocessing.freeze_support()
    _prepare_stdio()
    _add_bundled_tools()
    if sys.platform == "win32":
        _hide_child_console_windows()
    from clipvault.__main__ import main as cli

    sys.exit(cli(sys.argv[1:] or ["run"]))


if __name__ == "__main__":
    main()
