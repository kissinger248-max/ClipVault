"""The fetch and cleanup commands, end to end with a stand-in for the yt-dlp process."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from clipvault import config as cfg
from clipvault import db
from clipvault.__main__ import main
from clipvault.fetch import ytdlp
from tests.helpers import add_clip, add_streamer


class FakeProcess:
    """Answers both ``pip install`` and ``yt_dlp`` command lines."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.stderr = ""

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if "--output" in command and not self.stderr:
            template = command[command.index("--output") + 1]
            target = Path(template.replace(".%(ext)s", ".mp4").replace("%%", "%"))
            target.write_bytes(b"v" * 3000)
        return SimpleNamespace(returncode=1 if self.stderr else 0, stderr=self.stderr, stdout="")

    @property
    def downloads(self) -> int:
        return sum(1 for c in self.commands if "yt_dlp" in c)


@pytest.fixture
def process(monkeypatch: pytest.MonkeyPatch) -> FakeProcess:
    fake = FakeProcess()
    monkeypatch.setattr(ytdlp, "_run", fake)
    return fake


@pytest.fixture
def library(home: Path) -> Path:
    root = home / "My Library"
    (home / "config.yaml").write_text(f"library_root: '{root.as_posix()}'\n", encoding="utf-8")
    return root


def seed(clips: dict[str, dict]) -> None:
    conn = db.open_db(cfg.db_path())
    add_streamer(conn, "alpha")
    for clip_id, columns in clips.items():
        add_clip(conn, clip_id, **columns)
    conn.close()


def test_fetch_fills_the_library_then_downloads_nothing_twice(
    library: Path, process: FakeProcess, capsys
) -> None:
    seed({"c1": {"view_count": 50, "title": "First"}, "c2": {"view_count": 5}})
    assert main(["fetch"]) == 0
    out = capsys.readouterr().out
    assert "Checked for a newer downloader" in out
    assert "[1/2] alpha" in out and "First" in out
    assert "2 clip(s) saved to" in out
    assert len(list(library.rglob("*.mp4"))) == 2 and len(list(library.rglob("*.json"))) == 2
    assert process.downloads == 2

    assert main(["fetch"]) == 0
    out = capsys.readouterr().out
    assert "0 clip(s) saved" in out and "Checked for a newer downloader" not in out
    assert process.downloads == 2


def test_fetch_reports_failures_with_exit_code_1(
    library: Path, process: FakeProcess, capsys
) -> None:
    seed({"c1": {}})
    process.stderr = "ERROR: Unable to download JSON metadata: timed out"
    assert main(["fetch"]) == 1
    out = capsys.readouterr().out
    assert "FAILED - Unable to download JSON metadata: timed out" in out
    assert "1 failed and will be retried" in out


def test_fetch_limit_and_bad_streamer_name(library: Path, process: FakeProcess, capsys) -> None:
    seed({f"c{i}": {"view_count": i} for i in range(4)})
    assert main(["fetch", "--limit", "1"]) == 0
    assert "3 still to download" in capsys.readouterr().out
    assert main(["fetch", "--streamer", "not a login"]) == 2


def test_deleting_a_file_by_hand_does_not_break_anything(
    library: Path, process: FakeProcess, capsys
) -> None:
    seed({f"c{i}": {"view_count": i} for i in range(12)})
    main(["fetch"])
    victim = sorted(library.rglob("*.mp4"))[0]
    victim.unlink()
    capsys.readouterr()

    assert main(["cleanup"]) == 0
    out = capsys.readouterr().out
    assert "1 clip(s) were deleted by hand" in out
    assert "Retention: nothing to remove" in out and "Disk:" in out
    assert main(["fetch"]) == 0  # the deleted clip is not downloaded again
    assert process.downloads == 12
    assert not victim.exists()


def test_cleanup_dry_run_shows_the_plan_and_deletes_nothing(
    library: Path, process: FakeProcess, capsys
) -> None:
    old = "2026-01-01T12:00:00Z"
    seed(
        {
            "low": {"created_at": old, "view_count": 1},
            "mid": {"created_at": old, "view_count": 50},
            "high": {"created_at": old, "view_count": 900},
        }
    )
    main(["fetch"])
    capsys.readouterr()
    assert main(["cleanup", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Retention would remove 1 clip(s)" in out and "Nothing was deleted" in out
    assert len(list(library.rglob("*.mp4"))) == 3

    assert main(["cleanup"]) == 0
    assert "Retention removed 1 clip(s)" in capsys.readouterr().out
    assert len(list(library.rglob("*.mp4"))) == 2


def test_disk_guard_with_a_high_threshold_pauses_downloads(
    home: Path, library: Path, process: FakeProcess, capsys
) -> None:
    # No real drive is 90% empty... unless it is; use the highest allowed threshold
    # and skip when this machine happens to have that much free space.
    from clipvault.library.retention import free_space_pct

    if free_space_pct(home) >= 90:
        pytest.skip("this drive is more than 90% free")
    config = (home / "config.yaml").read_text(encoding="utf-8")
    (home / "config.yaml").write_text(
        config + "retention:\n  min_free_space_pct: 90\n", encoding="utf-8"
    )
    seed({"c1": {}})
    assert main(["fetch"]) == 1
    out = capsys.readouterr().out
    assert "STOPPED: the disk is too full" in out and "ALERT: Downloads are paused" in out
    assert process.downloads == 0
    assert main(["cleanup"]) == 1
    assert "TOO FULL" in capsys.readouterr().out
