"""Process-wide hooks for the running app: when it started, and how to make it quit.

``clipvault run`` registers its quit function here, so other parts (an update about
to replace the program, the tray) can ask the whole app to stop cleanly.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime

from clipvault.timeutil import utc_now

started_at: datetime = utc_now()
# Set when the app begins to quit: long loops stop between clips instead of finishing.
stopping = threading.Event()
_quit: Callable[[], None] | None = None
_lock = threading.Lock()


def on_quit(function: Callable[[], None] | None) -> None:
    """Register what stops the running app (``None`` when it has stopped)."""
    global _quit
    with _lock:
        _quit = function


def mark_started() -> None:
    global started_at
    started_at = utc_now()


def request_quit() -> bool:
    """Ask the running app to stop; False when nothing is running in this process."""
    with _lock:
        function = _quit
    if function is None:
        return False
    function()
    return True
