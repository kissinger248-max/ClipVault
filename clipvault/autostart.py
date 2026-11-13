"""Start ClipVault when the user logs in (SPEC §16: optional, on by default).

Only for the installed app; a source checkout has no single program to start.

- Windows: a value under ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``.
- macOS: a LaunchAgent in ``~/Library/LaunchAgents``.

The first time the installed app runs it switches this on once (``autostart_set``
in the settings table); after that the user's choice in Settings is kept.
"""

from __future__ import annotations

import logging
import os
import plistlib
import sqlite3
import sys
from pathlib import Path

from clipvault import alerts
from clipvault import config as cfg
from clipvault.logging_setup import ctx

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ClipVault"
AGENT_LABEL = "app.clipvault.desktop"
FIRST_RUN_SETTING = "autostart_set"

log = logging.getLogger(__name__)


def supported() -> bool:
    """Whether this copy can start itself at login (the installed app on Windows or macOS).

    ``CLIPVAULT_NO_AUTOSTART=1`` turns it off: test copies (the smoke test, a build's
    checks) must never register themselves to start with the computer.
    """
    if os.environ.get("CLIPVAULT_NO_AUTOSTART"):
        return False
    return cfg.is_packaged() and sys.platform in ("win32", "darwin")


def _program() -> str:
    """The program to start at login: always the windowless app, never the console one."""
    program = Path(sys.executable).resolve()
    if program.name.lower() == "clipvault-console.exe":
        program = program.with_name("ClipVault.exe")
    return str(program)


def _agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def is_enabled() -> bool:
    """Whether ClipVault is set to start at login."""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, VALUE_NAME)
            return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return _agent_path().exists()
    return False


def set_enabled(enabled: bool) -> None:
    """Switch starting at login on or off."""
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, f'"{_program()}"')
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
    elif sys.platform == "darwin":
        path = _agent_path()
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            agent = {"Label": AGENT_LABEL, "ProgramArguments": [_program()], "RunAtLoad": True,
                     "ProcessType": "Interactive"}  # fmt: skip
            path.write_bytes(plistlib.dumps(agent))
        else:
            path.unlink(missing_ok=True)
    log.info("start at login %s", "on" if enabled else "off", extra=ctx(job="autostart"))


def first_run(conn: sqlite3.Connection) -> None:
    """On the installed app's first run, switch starting at login on (once)."""
    if not supported() or alerts.get_setting(conn, FIRST_RUN_SETTING):
        return
    try:
        set_enabled(True)
    except OSError as exc:
        log.warning("could not set start at login: %s", exc, extra=ctx(job="autostart"))
    alerts.set_setting(conn, FIRST_RUN_SETTING, "1")
