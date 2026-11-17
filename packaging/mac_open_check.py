"""macOS: open the built ClipVault.app the way a person does, again while it runs, and check it.

    python packaging/mac_open_check.py packaging/build/dist/ClipVault.app

Run on the throwaway build machine only: the app starts with that machine's own data
folder and keychain, as a fresh install would, through Launch Services (``open``).

What it checks (0.3.2 failed the second one: "You can't open the application
ClipVault because it is not responding"):

1. Opening ClipVault starts it and opens its window (a separate window process).
2. With the window closed, opening ClipVault again is answered (no "not responding")
   and opens a new window.
3. Opening it once more while the window is open keeps one window.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8765"


def check(condition: bool, what: str) -> None:
    if not condition:
        raise SystemExit(f"MAC OPEN CHECK FAILED: {what}")
    print(f"  ok: {what}")


def open_app(app: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["open", str(app)], capture_output=True, text=True, timeout=60)


def window(http: httpx.Client, *, not_pid: int | None = None, seconds: float = 120) -> dict | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            status = http.get("/app/window/status").json()
        except (httpx.TransportError, ValueError):
            status = {}
        if status.get("running") and status.get("polled_s_ago") is not None:
            if not_pid is None or status["pid"] != not_pid:
                return status
        time.sleep(1)
    return None


def main() -> int:
    app = Path(sys.argv[1]).resolve()
    try:
        first = open_app(app)
        check(first.returncode == 0, f"open starts ClipVault ({first.stderr.strip()})")
        with httpx.Client(base_url=BASE, timeout=10) as http:
            status = window(http)
            check(status is not None, "ClipVault opens with its window")
            first_pid = status["pid"]
            subprocess.run(["kill", str(first_pid)], check=False)  # like closing the window
            time.sleep(3)
            again = open_app(app)
            problem = again.stderr.strip()
            check(again.returncode == 0, f"opening it again while it runs is answered ({problem})")
            status = window(http, not_pid=first_pid)
            check(status is not None, "opening it again brings a new window")
            once_more = open_app(app)
            check(once_more.returncode == 0, "opening it with the window open is answered")
            time.sleep(3)
            final = http.get("/app/window/status").json()
            check(final["pid"] == status["pid"], "still one window")
            check(http.get("/healthz").status_code == 200, "the app still answers")
    finally:
        subprocess.run(["pkill", "-f", f"{app}/Contents/MacOS/ClipVault"], check=False)
    print("mac open check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
