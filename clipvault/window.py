"""ClipVault's own app window (pywebview: WebView2 on Windows, WebKit on macOS).

The control panel opens in a ClipVault window with its own taskbar or Dock button,
instead of a browser tab. One window lives as long as the app:

- closing it only hides it; ClipVault keeps working in the background (tray icon);
- the desktop icon, the Start menu, the Dock or the tray brings it back;
- Quit in the tray (or an update) really closes it.

The window signs itself in: it opens ``/app/enter`` with a token that exists only
in this process, and the panel turns that into the usual login cookie. The panel
password still guards every other way in (a phone, a browser). Links to other
sites open in the normal browser; "Save Video" downloads go to the Downloads folder.

``webview.start`` must own the main thread, so ``run`` blocks until ``close``.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipvault.logging_setup import ctx

log = logging.getLogger(__name__)
JOB = "window"
TITLE = "ClipVault"

_window: Any = None
_quitting = threading.Event()
_token = secrets.token_urlsafe(32)


def available() -> bool:
    """Whether the window library is installed (it is in the installed app)."""
    try:
        import webview  # noqa: F401
    except Exception:  # missing, or no window system on this machine
        return False
    return True


def token_ok(candidate: str | None) -> bool:
    """The window's sign-in token (constant-time comparison)."""
    return bool(candidate) and hmac.compare_digest(candidate, _token)


def is_open() -> bool:
    return _window is not None and not _quitting.is_set()


def show() -> bool:
    """Bring the window to the front; False when there is no window."""
    if not is_open():
        return False
    try:
        _window.show()
        _window.restore()  # un-minimise
        _window.on_top = True  # raise it above other windows, then let it behave normally
        _window.on_top = False
    except Exception as exc:  # the window must never take the app down
        log.warning("could not show the window: %s", exc, extra=ctx(job=JOB))
        return False
    return True


def close() -> None:
    """Really close the window (Quit or an update): ``run`` then returns."""
    _quitting.set()
    if _window is not None:
        try:
            _window.destroy()
        except Exception as exc:
            log.info("window already closed: %s", exc, extra=ctx(job=JOB))


def _hide_instead_of_close() -> bool:
    if _quitting.is_set():
        return True  # let it close
    try:
        _window.hide()
    except Exception as exc:
        log.warning("could not hide the window: %s", exc, extra=ctx(job=JOB))
    return False  # cancel the close; ClipVault keeps running


def run(base_url: str, storage: Path, *, visible: bool = True,
        started: Callable[[], None] | None = None) -> None:  # fmt: skip
    """Open the window and run the window system until ``close`` (blocks the main thread).

    ``storage`` keeps the window's cookies, so the sign-in lasts across restarts.
    ``started`` runs on a helper thread once the window system is up.
    """
    global _window
    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.settings["SHOW_DEFAULT_MENUS"] = False
    _quitting.clear()
    storage.mkdir(parents=True, exist_ok=True)
    _window = webview.create_window(
        TITLE,
        f"{base_url}/app/enter?t={_token}",
        width=1280,
        height=860,
        min_size=(380, 560),
        hidden=not visible,
        background_color="#F5F5F7",
        text_select=True,
    )
    _window.events.closing += _hide_instead_of_close
    log.info("app window opening (%s)", "shown" if visible else "hidden", extra=ctx(job=JOB))
    try:
        webview.start(started, private_mode=False, storage_path=str(storage))
    finally:
        _window = None
        log.info("app window closed", extra=ctx(job=JOB))
