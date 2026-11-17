"""ClipVault's own app window, in a process of its own (pywebview: WebView2 / WebKit).

Why a separate process: in 0.3.2 the window lived inside the running app, and bringing it
forward changed a window setting from the wrong thread. On Windows that deadlocks the
whole Python process (seen on a real install: the window froze, finding and downloading
clips stopped, and closing the frozen window killed everything). Now the clip work and
the window can't take each other down.

Two halves:

- **In the running app** (``clipvault run``): :class:`WindowManager` starts the window
  process when ClipVault is opened (start, desktop icon, Start menu, tray, Dock), asks it
  to come to the front when it is already open, and closes it when the app quits. This
  half never loads a GUI toolkit.
- **In the window process** (``clipvault window``): :func:`run_window` opens the panel on
  the process's own main thread, signs itself in with a token the app passed in its
  environment (never over the network), and checks in with the app twice a second: to
  come forward when asked, and to close itself when the app is gone.

Closing the window ends the window process; ClipVault keeps working from the tray or
menu bar. Links to other sites open in the normal browser; "Save Video" downloads go to
the Downloads folder.
"""

from __future__ import annotations

import hmac
import importlib.util
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipvault import config as cfg
from clipvault.logging_setup import ctx

log = logging.getLogger(__name__)
JOB = "window"
TITLE = "ClipVault"
TOKEN_ENV = "CLIPVAULT_WINDOW_TOKEN"
TOKEN_HEADER = "X-ClipVault-Window"
POLL_PATH = "/app/window/poll"
POLL_EVERY_S = 0.5
GIVE_UP_AFTER_S = 8.0  # no answer from the app for this long: it is gone, close the window
SHOWN_WAIT_S = 60  # how long the window may take to appear before checking in anyway
CLOSE_WAIT_S = 5

_manager: WindowManager | None = None


def available() -> bool:
    """Whether the window library is installed (it is in the installed app).

    Only looks for it: the running app never loads the GUI toolkit itself.
    """
    try:
        return importlib.util.find_spec("webview") is not None
    except (ImportError, ValueError):
        return False


def window_command(base_url: str) -> list[str]:
    """How the running app starts the window process (the same program, ``window``)."""
    if cfg.is_packaged():
        return [sys.executable, "window", "--url", base_url]
    return [sys.executable, "-m", "clipvault", "window", "--url", base_url]


def allow_foreground() -> None:
    """Windows: let the window process take the foreground.

    A program in the background may not pull its window to the front on its own; the
    program the user just clicked (a second launch, the tray) can hand that right on.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    except (OSError, AttributeError):
        pass


# --- in the running app ---------------------------------------------------------------


class WindowManager:
    """Starts, raises and closes the window process for the running app."""

    def __init__(
        self,
        base_url: str,
        home: Path,
        *,
        command: list[str] | None = None,
        spawn: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.base_url = base_url
        self.home = home
        self.token = secrets.token_urlsafe(32)
        self._command = command or window_command(base_url)
        self._spawn = spawn
        self._process: Any = None
        self._raise_count = 0
        self._last_poll: float | None = None
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def open(self) -> bool:
        """Show the window: start it, or bring the open one forward. False if it can't start."""
        with self._lock:
            allow_foreground()
            if self.running():
                self._raise_count += 1
                return True
            env = {**os.environ, TOKEN_ENV: self.token, "CLIPVAULT_HOME": str(self.home)}
            options: dict[str, Any] = {"env": env, "stdin": subprocess.DEVNULL,
                                       "stdout": subprocess.DEVNULL,
                                       "stderr": subprocess.DEVNULL}  # fmt: skip
            if sys.platform == "win32":
                options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            try:
                self._process = self._spawn(self._command, **options)
            except OSError as exc:
                log.warning("the app window could not start: %s", exc, extra=ctx(job=JOB))
                self._process = None
                return False
            self._last_poll = None
            log.info("app window starting", extra=ctx(job=JOB, pid=self._process.pid))
            return True

    def token_ok(self, candidate: str | None) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate or "", self.token)

    def poll(self, candidate: str | None) -> int | None:
        """The window process checking in; returns how often it was asked to come forward."""
        if not self.token_ok(candidate):
            return None
        self._last_poll = time.monotonic()
        return self._raise_count

    def status(self) -> dict[str, Any]:
        """For checks: is a window process running, and when did it last check in."""
        polled = None if self._last_poll is None else time.monotonic() - self._last_poll
        return {
            "running": self.running(),
            "pid": self._process.pid if self.running() else None,
            "polled_s_ago": None if polled is None else round(polled, 1),
        }

    def close(self) -> None:
        """Close the window process (Quit, an update)."""
        with self._lock:
            process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=CLOSE_WAIT_S)
        except subprocess.TimeoutExpired:
            process.kill()


def set_manager(manager: WindowManager | None) -> None:
    global _manager
    _manager = manager


def manager() -> WindowManager | None:
    """The running app's window manager, or None when it shows the panel in the browser."""
    return _manager


def token_ok(candidate: str | None) -> bool:
    """The window's sign-in token (constant-time comparison); False without a window."""
    return _manager is not None and _manager.token_ok(candidate)


def show() -> bool:
    """Open the window or bring it forward; False when this app has no window."""
    return _manager is not None and _manager.open()


# --- in the window process --------------------------------------------------------------


def run_window(
    base_url: str,
    token: str,
    storage: Path,
    *,
    get: Callable[..., Any] | None = None,
) -> int:
    """Open the panel in ClipVault's window; blocks until the window is closed.

    ``storage`` keeps the window's cookies, so the sign-in lasts across restarts.
    ``get`` replaces the HTTP call to the app (tests).
    """
    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.settings["SHOW_DEFAULT_MENUS"] = False
    storage.mkdir(parents=True, exist_ok=True)
    win = webview.create_window(
        TITLE,
        f"{base_url}/app/enter?t={token}",
        width=1280,
        height=860,
        min_size=(380, 560),
        background_color="#F5F5F7",
        text_select=True,
    )
    stop = threading.Event()
    watcher = threading.Thread(
        target=_watch_app, args=(win, base_url, token, stop, get), name="window-watch", daemon=True
    )
    watcher.start()
    log.info("app window opening", extra=ctx(job=JOB))
    try:
        webview.start(private_mode=False, storage_path=str(storage))
    finally:
        stop.set()
        log.info("app window closed", extra=ctx(job=JOB))
    return 0


def _watch_app(
    win: Any, base_url: str, token: str, stop: threading.Event, get: Callable[..., Any] | None
) -> None:
    """Check in with the running app: come forward when asked, close when it is gone."""
    if get is None:
        import httpx

        with httpx.Client(timeout=httpx.Timeout(2.0, connect=1.0)) as client:
            _watch_app(win, base_url, token, stop, client.get)
        return
    if not win.events.shown.wait(SHOWN_WAIT_S):
        log.warning("the window did not appear within %d s", SHOWN_WAIT_S, extra=ctx(job=JOB))
    last_answer = time.monotonic()
    seen: int | None = None
    while not stop.wait(POLL_EVERY_S):
        count = _check_in(get, base_url, token)
        if count is None:
            if time.monotonic() - last_answer >= GIVE_UP_AFTER_S:
                log.info("ClipVault is no longer running; closing its window", extra=ctx(job=JOB))
                _close(win)
                return
            continue
        last_answer = time.monotonic()
        if seen is not None and count > seen:
            bring_forward(win)
        seen = count


def _check_in(get: Callable[..., Any], base_url: str, token: str) -> int | None:
    try:
        reply = get(base_url + POLL_PATH, headers={TOKEN_HEADER: token})
        if reply.status_code != 200:
            return None
        return int(reply.json()["raise"])
    except Exception:  # the app is gone, restarting, or answered nonsense
        return None


def bring_forward(win: Any) -> None:
    """Un-minimise the window and put it in front.

    On Windows this uses the Win32 calls directly: changing pywebview window settings
    from this (non-GUI) thread is what froze 0.3.2.
    """
    try:
        if sys.platform == "win32":
            _bring_forward_windows()
        else:
            win.restore()
            win.show()
    except Exception as exc:  # the window must never take the process down
        log.warning("could not bring the window forward: %s", exc, extra=ctx(job=JOB))


def _bring_forward_windows() -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    pid = os.getpid()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _param):  # type: ignore[no-untyped-def]
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        visible = user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd)
        if owner.value == pid and visible:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(each, 0)
    if not found:
        return False
    hwnd = found[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


def _close(win: Any) -> None:
    try:
        win.destroy()
    except Exception as exc:
        log.info("window already closed: %s", exc, extra=ctx(job=JOB))
