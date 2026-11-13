"""Tray icon (pystray): green running, yellow paused, red needs you (SPEC §16).

Menu: Open control panel (also a double-click), Pause / Resume, Quit. The icon
runs on its own thread, so ``run`` keeps control of the main thread and Ctrl+C
still works. Without the ``desktop`` extra (pystray) there is simply no icon.
"""

from __future__ import annotations

import logging
import sys
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipvault import alerts, db
from clipvault.logging_setup import ctx

COLORS = {"green": "#1d9a55", "yellow": "#e0a800", "red": "#d93025"}
REFRESH_S = 30
log = logging.getLogger(__name__)


def status(db_path: Path) -> str:
    """green / yellow / red, the same rule as the Home screen's status card."""
    conn = db.open_db(db_path)
    try:
        if alerts.open_alerts(conn):
            return "red"
        return "yellow" if alerts.is_paused(conn) else "green"
    finally:
        conn.close()


def _image(color: str) -> Any:
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([2, 2, size - 3, size - 3], radius=14, fill="#6441a5")
    draw.polygon([(24, 18), (24, 46), (46, 32)], fill="white")
    draw.ellipse([40, 40, 62, 62], fill=COLORS[color], outline="white", width=3)
    return image


def start(
    url: str, db_path: Path, on_quit: Callable[[], None], *, on_main_thread: bool = False
) -> Any | None:
    """Show the tray icon; returns it (call ``.stop()`` on exit), or None when unavailable."""
    try:
        import pystray
    except ImportError:
        return None

    def toggle_pause(icon: Any, item: Any) -> None:
        conn = db.open_db(db_path)
        try:
            alerts.set_setting(conn, "paused", "0" if alerts.is_paused(conn) else "1")
        finally:
            conn.close()
        refresh(icon)

    def paused_label(item: Any) -> str:
        try:
            return "Resume" if status(db_path) == "yellow" else "Pause"
        except Exception:
            return "Pause / Resume"

    def quit_app(icon: Any, item: Any) -> None:
        icon.stop()
        on_quit()

    def refresh(icon: Any) -> None:
        try:
            color = status(db_path)
        except Exception as exc:  # the icon must never take the app down
            log.warning("tray status failed: %s", exc, extra=ctx(job="tray"))
            return
        icon.icon = _image(color)
        icon.title = {"green": "ClipVault: running", "yellow": "ClipVault: paused",
                      "red": "ClipVault: needs you"}[color]  # fmt: skip

    menu = pystray.Menu(
        pystray.MenuItem(
            "Open control panel", lambda icon, item: webbrowser.open(url), default=True
        ),  # fmt: skip
        pystray.MenuItem(paused_label, toggle_pause),
        pystray.MenuItem("Quit ClipVault", quit_app),
    )
    icon = pystray.Icon("clipvault", _image("green"), "ClipVault", menu)
    stop = threading.Event()

    def keep_fresh() -> None:
        while not stop.wait(REFRESH_S):
            refresh(icon)

    original_stop = icon.stop

    def stop_all() -> None:
        stop.set()
        original_stop()

    icon.stop = stop_all

    def begin(icon: Any) -> None:
        icon.visible = True
        refresh(icon)
        threading.Thread(target=keep_fresh, name="tray-refresh", daemon=True).start()

    if on_main_thread:
        # macOS draws menu-bar icons only from the main thread: the caller runs it
        # with ``run_on_main_thread`` instead of waiting on its own loop.
        icon._clipvault_begin = begin
        return icon
    try:
        icon.run_detached()
    except Exception as exc:  # no desktop session (a server, a remote shell)
        log.info("no tray icon: %s", exc, extra=ctx(job="tray"))
        return None
    begin(icon)
    return icon


def needs_main_thread() -> bool:
    """macOS needs the icon on the main thread; Windows and Linux run it on its own."""
    return sys.platform == "darwin"


def run_on_main_thread(icon: Any) -> None:
    """Show the icon and block until it is stopped (Quit, or ``icon.stop()``)."""
    icon.run(setup=icon._clipvault_begin)
