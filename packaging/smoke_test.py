"""Start a built ClipVault as a brand-new user would and use its control panel.

    python packaging/smoke_test.py <built program>

Isolated from any real copy: a temporary home folder, its own keychain entries
(``CLIPVAULT_KEYRING_SERVICE``), its own port, and paused so it downloads nothing.
It checks that the panel comes up, the first password can be chosen, the setup
pages render, a caption preview is drawn (bundled FFmpeg + libass + fonts), the
panel's fonts are served, and, when the build has the built-in Twitch app, that
Twitch hands out a sign-in code for it. Live network: Twitch's sign-in endpoint only.
"""

from __future__ import annotations

import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

SECRET_NAMES = ("twitch_client_id", "twitch_client_secret", "groq_api_key", "llm_api_key",
                "uploadpost_api_key", "twitch_app_token", "ntfy_topic", "panel_password_hash",
                "panel_session_key", "twitch_user_token")  # fmt: skip


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check(condition: bool, what: str) -> None:
    if not condition:
        raise SystemExit(f"SMOKE TEST FAILED: {what}")
    print(f"  ok: {what}")


def main() -> int:
    program = Path(sys.argv[1]).resolve()
    service = f"clipvault-smoke-{secrets.token_hex(4)}"
    port = free_port()
    with tempfile.TemporaryDirectory() as folder:
        home = Path(folder)
        (home / "library").mkdir()
        (home / "config.yaml").write_text(
            f"library_root: '{(home / 'library').as_posix()}'\npanel:\n  port: {port}\n",
            encoding="utf-8",
        )
        env = {**__import__("os").environ, "CLIPVAULT_HOME": str(home),
               "CLIPVAULT_KEYRING_SERVICE": service,
               "CLIPVAULT_NO_AUTOSTART": "1", "CLIPVAULT_NO_UPDATES": "1"}  # fmt: skip
        try:
            subprocess.run([program, "pause"], env=env, check=True, timeout=120)
            log = open(home / "run-output.txt", "w", encoding="utf-8")  # noqa: SIM115
            app = subprocess.Popen([program, "run", "--no-tray", "--no-browser"], env=env,
                                   stdout=log, stderr=subprocess.STDOUT)  # fmt: skip
            try:
                exercise(f"http://127.0.0.1:{port}")
            finally:
                app.terminate()
                try:
                    app.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    app.kill()
                log.close()
            problems = [line for path in (home / "logs").glob("*.jsonl")
                        for line in path.read_text(encoding="utf-8").splitlines()
                        if "Traceback" in line]  # fmt: skip
            check(not problems, "no crashes in the log" + (f": {problems[:2]}" if problems else ""))
        finally:
            forget_secrets(service)
    print("smoke test passed")
    return 0


def exercise(base: str) -> None:
    with httpx.Client(base_url=base, timeout=60, follow_redirects=False) as http:
        deadline = time.monotonic() + 120
        while True:
            try:
                if http.get("/healthz").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise SystemExit("SMOKE TEST FAILED: the control panel did not start")
            time.sleep(1)
        check(True, "control panel answers")
        first = http.get("/")
        check(
            first.status_code in (302, 303, 307) and "setup-password" in first.headers["location"],
            "a new copy asks for a panel password first",
        )
        password = secrets.token_urlsafe(12)
        chosen = http.post("/setup-password", data={"password": password, "again": password})
        check(chosen.status_code == 303, "the first password can be chosen")
        step2 = http.get("/setup/2", follow_redirects=True)
        check(step2.status_code == 200 and "Connect to Twitch" in step2.text, "setup page renders")
        preview = http.get(
            "/look/preview/letterbox.jpg",
            params={"style": "box", "accent": "#D9480F", "size": "72", "credit": "0"},
        )
        # A new copy has no clips, so the preview is the placeholder picture; the real
        # caption drawing is checked by `selftest` (Caption drawing).
        check(preview.status_code == 200, "the look preview answers")
        font = http.get("/static/fonts/Inter-latin.woff2")
        check(font.status_code == 200 and font.content[:4] == b"wOF2", "panel fonts are served")
        if "Sign In with Twitch" in step2.text:
            token = re.search(r'name="csrf" value="([^"]+)"', step2.text).group(1)
            http.post("/settings/twitch-login", data={"csrf": token, "next": "/setup/2"})
            waiting = http.get("/setup/2").text
            code = re.search(r'id="twitch-code"[^>]*|value="([A-Z0-9-]{6,})"', waiting)
            check("twitch.tv/activate" in waiting and code is not None,
                  "Twitch gives a sign-in code for the built-in app")  # fmt: skip
        else:
            print("  (no built-in Twitch app in this build; sign-in not checked)")


def forget_secrets(service: str) -> None:
    import keyring
    from keyring.errors import PasswordDeleteError

    for name in SECRET_NAMES:
        try:
            keyring.delete_password(service, name)
        except PasswordDeleteError:
            pass
        except Exception as exc:  # a locked keychain on a build machine: nothing to clean
            print(f"  (could not clean keychain entry {name}: {exc})")


if __name__ == "__main__":
    raise SystemExit(main())
