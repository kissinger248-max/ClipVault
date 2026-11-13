"""Timeouts end everything a tool started, and quitting stops the job loops promptly."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from clipvault import lifecycle, procs

# A "tool" that starts a long-running child holding its output open (like yt-dlp -> FFmpeg).
PARENT = (
    "import subprocess, sys, time;"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
    "time.sleep(60)"
)


def test_a_timeout_ends_the_tool_and_what_it_started() -> None:
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        procs.run([sys.executable, "-c", PARENT], capture_output=True, text=True, timeout=2)
    assert time.monotonic() - started < 20  # not the 60 s the grandchild would hold the pipes


def test_normal_runs_behave_like_subprocess_run() -> None:
    done = procs.run([sys.executable, "-c", "print('hi')"], capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.strip() == "hi"
    with pytest.raises(subprocess.CalledProcessError):
        procs.run([sys.executable, "-c", "raise SystemExit(3)"], check=True)


def test_a_step_does_not_start_while_the_app_is_quitting(home) -> None:
    from clipvault import config as cfg
    from clipvault import scheduler

    ran: list[str] = []
    lifecycle.stopping.set()
    note = scheduler.run_step("discover", lambda conn, config, now: ran.append("x") or "did",
                              cfg.Config())  # fmt: skip
    assert note is None and ran == []
