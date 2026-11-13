"""Running outside tools (yt-dlp, FFmpeg, TwitchDownloaderCLI) with a timeout that holds.

``subprocess.run(timeout=...)`` kills only the program it started. yt-dlp starts FFmpeg
for stream cuts; when yt-dlp is killed, that FFmpeg keeps running and keeps the output
pipes open, so ``subprocess.run`` then waits for it (seen on Windows: a 5-minute cut
limit that never ended, and an app that couldn't quit). Here a timeout kills the whole
tree of processes the tool started, then stops waiting on the pipes.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Any

PIPE_GRACE_S = 10  # after killing, how long to wait for the output pipes to close


def kill_tree(process: subprocess.Popen) -> None:
    """Kill a process and every process it started."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                       capture_output=True, check=False)  # fmt: skip
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)  # started in its own session
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.kill()
    except OSError:
        pass


def run(
    command: Any,
    *,
    timeout: float | None = None,
    capture_output: bool = False,
    input: Any = None,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Like ``subprocess.run``, but a timeout also ends everything the program started."""
    if capture_output:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    if input is not None:
        kwargs.setdefault("stdin", subprocess.PIPE)
    if sys.platform == "win32":
        new_group = subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | new_group
    else:
        kwargs.setdefault("start_new_session", True)
    with subprocess.Popen(command, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            kill_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=PIPE_GRACE_S)
            except subprocess.TimeoutExpired:
                for pipe in (process.stdout, process.stderr):
                    if pipe:
                        pipe.close()
                stdout = stderr = None
            timed_out = subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
            raise timed_out from expired
        except BaseException:
            kill_tree(process)
            raise
        returncode = process.wait()
    if check and returncode:
        raise subprocess.CalledProcessError(returncode, command, stdout, stderr)
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)
