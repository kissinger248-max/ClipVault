from __future__ import annotations

import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipvault import runs
from clipvault.fetch import ytdlp
from clipvault.fetch.ytdlp import DownloadError, download_clip, format_selector, update_if_due

URL = "https://www.twitch.tv/alpha/clip/Some-Clip"
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


class Runner:
    """Records the command and behaves like ``subprocess.run``."""

    def __init__(self, returncode: int = 0, stderr: str = "", write: bool = True) -> None:
        self.returncode, self.stderr, self.write = returncode, stderr, write
        self.commands: list[list[str]] = []
        self.raises: Exception | None = None

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if self.raises:
            raise self.raises
        if self.write and self.returncode == 0 and "--output" in command:
            template = command[command.index("--output") + 1]
            Path(template.replace(".%(ext)s", ".mp4").replace("%%", "%")).write_bytes(b"video")
        return SimpleNamespace(returncode=self.returncode, stderr=self.stderr, stdout="")


def test_format_selector_caps_quality_and_avoids_portrait() -> None:
    assert format_selector("720p").startswith("best[height<=720][format_id!^=portrait]")
    assert format_selector("1080p").startswith("best[height<=1080][format_id!^=portrait]")


def test_download_builds_the_command_and_returns_the_file(home: Path) -> None:
    runner = Runner()
    dest = home / "lib" / "alpha" / "100% real_Some-Clip.mp4"
    assert download_clip(URL, dest, "720p", runner=runner) == dest
    command = runner.commands[0]
    assert command[1:3] == ["-m", "yt_dlp"]
    assert command[-1] == URL
    assert format_selector("720p") in command
    template = command[command.index("--output") + 1]
    assert template.endswith("100%% real_Some-Clip.%(ext)s")  # a % in the path is escaped
    assert "--no-playlist" in command and "--no-overwrites" in command


@pytest.mark.parametrize(
    ("stderr", "permanent"),
    [
        ("WARNING: x\nERROR: [twitch:clips] Abc: This clip is no longer available\n", True),
        ("ERROR: unable to download video data: HTTP Error 404: Not Found", True),
        ("ERROR: Unable to download JSON metadata: timed out", False),
        ("", False),
    ],
)
def test_failures_are_classified(home: Path, stderr: str, permanent: bool) -> None:
    runner = Runner(returncode=1, stderr=stderr)
    with pytest.raises(DownloadError) as info:
        download_clip(URL, home / "clip.mp4", "720p", runner=runner)
    assert info.value.permanent is permanent
    assert "ERROR:" not in str(info.value)


def test_timeout_missing_file_and_missing_program_are_download_errors(home: Path) -> None:
    runner = Runner()
    runner.raises = subprocess.TimeoutExpired("yt-dlp", 300)
    with pytest.raises(DownloadError, match="longer than"):
        download_clip(URL, home / "a.mp4", "720p", runner=runner)
    runner.raises = FileNotFoundError("python")
    with pytest.raises(DownloadError, match="could not start"):
        download_clip(URL, home / "b.mp4", "720p", runner=runner)
    with pytest.raises(DownloadError, match="missing or empty"):
        download_clip(URL, home / "c.mp4", "720p", runner=Runner(write=False))


def test_real_processes_are_blocked_in_tests(home: Path) -> None:
    with pytest.raises(AssertionError, match="real process"):
        download_clip(URL, home / "clip.mp4", "720p")


def test_update_runs_at_most_once_a_day(conn: sqlite3.Connection) -> None:
    runner = Runner()
    assert update_if_due(conn, now=NOW, runner=runner) is True
    assert runner.commands[0][1:5] == ["-m", "pip", "install", "--upgrade"]
    assert update_if_due(conn, now=NOW + timedelta(hours=23), runner=runner) is False
    assert update_if_due(conn, now=NOW + timedelta(hours=25), runner=runner) is True
    assert len(runner.commands) == 2
    assert runs.get_run(conn, ytdlp.UPDATE_JOB)["last_error"] is None


def test_failed_update_is_recorded_and_not_retried_until_tomorrow(conn: sqlite3.Connection) -> None:
    runner = Runner(returncode=1, stderr="no internet")
    assert update_if_due(conn, now=NOW, runner=runner) is True
    assert "no internet" in runs.get_run(conn, ytdlp.UPDATE_JOB)["last_error"]
    assert update_if_due(conn, now=NOW + timedelta(hours=1), runner=runner) is False
    runner.raises = subprocess.TimeoutExpired("pip", 300)
    assert update_if_due(conn, now=NOW + timedelta(days=2), runner=runner) is True
