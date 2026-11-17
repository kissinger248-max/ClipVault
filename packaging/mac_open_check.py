"""macOS: open the built ClipVault.app the way a person does, and upgrade to it like one.

    python packaging/mac_open_check.py packaging/build/dist/ClipVault.app

Run on the throwaway build machine only: the app uses that machine's own data folder,
keychain and /Applications, as a real install does, and starts through Launch Services
(``open``).

1. **Open it twice.** Opening ClipVault starts it with its window (a separate window
   process); with the window closed, opening it again while it runs is answered and
   brings a new window; once more with the window open keeps one window. (0.3.2: "You
   can't open the application ClipVault because it is not responding".)
2. **Upgrade with saved keys.** The latest release is installed in /Applications and
   saves a panel password and a phone topic in the keychain; this build replaces it and
   is opened twice. It must still answer and open its window. (0.3.3 froze here: its
   main thread waited for macOS's permission to read a key the older version saved.
   macOS keeps asking after every update: without a paid Apple team ID a keychain entry
   belongs to one exact build; a self-made signing identity was tried and didn't help.)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8765"
INSTALLED = Path("/Applications/ClipVault.app")
RELEASES = "https://github.com/kissinger248-max/ClipVault/releases"
LATEST_DMG = f"{RELEASES}/latest/download/ClipVault-macOS.dmg"
KEY_NAMES = ("twitch_client_id", "twitch_client_secret", "groq_api_key", "llm_api_key",
             "uploadpost_api_key", "twitch_app_token", "ntfy_topic", "panel_password_hash",
             "panel_session_key", "twitch_user_token")  # fmt: skip


def check(condition: bool, what: str) -> None:
    if not condition:
        raise SystemExit(f"MAC OPEN CHECK FAILED: {what}")
    print(f"  ok: {what}")


def binary(app: Path) -> Path:
    return app / "Contents" / "MacOS" / "ClipVault"


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


def wait_for_panel(http: httpx.Client, seconds: float = 120) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if http.get("/healthz").status_code == 200:
                return True
        except httpx.TransportError:
            pass
        time.sleep(1)
    return False


def stop_all() -> None:
    subprocess.run(["pkill", "-9", "-f", "ClipVault.app/Contents/MacOS/ClipVault"], check=False)
    time.sleep(2)


def forget_keys() -> None:
    for name in KEY_NAMES:
        subprocess.run(["security", "delete-generic-password", "-s", "clipvault", "-a", name],
                       capture_output=True, check=False)  # fmt: skip


def install(dmg_or_app: Path) -> None:
    """Put a ClipVault.app (from a .dmg or a folder) in /Applications, replacing the old one."""
    shutil.rmtree(INSTALLED, ignore_errors=True)
    if dmg_or_app.suffix == ".dmg":
        mount = Path(tempfile.mkdtemp())
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-noautoopen", "-mountpoint",
                        str(mount), str(dmg_or_app)], check=True, capture_output=True)  # fmt: skip
        try:
            subprocess.run(["ditto", str(mount / "ClipVault.app"), str(INSTALLED)], check=True)
        finally:
            subprocess.run(["hdiutil", "detach", str(mount), "-quiet"], check=False)
    else:
        subprocess.run(["ditto", str(dmg_or_app), str(INSTALLED)], check=True)


# --- 1. open it twice -------------------------------------------------------------------


def open_twice(app: Path) -> None:
    print("1. open it twice")
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
    stop_all()


# --- 2. upgrade with saved keys ------------------------------------------------------------


def upgrade_with_saved_keys(app: Path, work: Path) -> None:
    print("2. upgrade from the latest release, with keys it saved")
    forget_keys()
    dmg = work / "latest.dmg"
    with httpx.Client(follow_redirects=True, timeout=300) as http:
        dmg.write_bytes(http.get(LATEST_DMG).content)
    install(dmg)
    old = binary(INSTALLED)
    subprocess.run([str(old), "pause"], check=True, capture_output=True, timeout=120)
    check(open_app(INSTALLED).returncode == 0, "the latest release starts")
    with httpx.Client(base_url=BASE, timeout=10) as http:
        check(wait_for_panel(http), "its panel answers")
        password = "upgrade-check-1"
        made = http.post("/setup-password", data={"password": password, "again": password})
        check(made.status_code == 303, "it saves a panel password in the keychain")
    subprocess.run([str(old), "notify", "setup"], capture_output=True, timeout=120, check=False)
    stop_all()
    install(app)
    first = open_app(INSTALLED)
    check(first.returncode == 0, f"the new version starts over it ({first.stderr.strip()})")
    time.sleep(1)
    again = open_app(INSTALLED)
    check(again.returncode == 0, f"opening it again at once is answered ({again.stderr.strip()})")
    with httpx.Client(base_url=BASE, timeout=10) as http:
        check(window(http) is not None, "it opens its window although the old keys wait")
        later = open_app(INSTALLED)
        check(later.returncode == 0, f"and answers later too ({later.stderr.strip()})")
        check(http.get("/healthz").status_code == 200, "the app still answers")
    stop_all()


def main() -> int:
    app = Path(sys.argv[1]).resolve()
    work = Path(tempfile.mkdtemp(prefix="clipvault-open-check-"))
    try:
        open_twice(app)
        upgrade_with_saved_keys(app, work)
    finally:
        stop_all()
        shutil.rmtree(work, ignore_errors=True)
    print("mac open check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
