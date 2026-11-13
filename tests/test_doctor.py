import sys
from collections import namedtuple
from pathlib import Path

import keyring
import pytest
from keyring.backends import fail as fail_backend

from clipvault import doctor
from clipvault.__main__ import main

CHECK_NAMES = [
    "Python",
    "FFmpeg",
    "yt-dlp",
    "TwitchDownloaderCLI",
    "Config file",
    "Keyring",
    "Twitch keys",
    "Disk space",
    "Database",
]
Usage = namedtuple("Usage", "total used free")


def test_doctor_reports_every_check(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["doctor"])
    out = capsys.readouterr().out
    for name in CHECK_NAMES:
        assert name in out
    assert code in (0, 1)
    assert [r.name for r in doctor.run_checks(home)] == CHECK_NAMES
    assert str(home) in out  # the home folder is shown, so it is clear which data is in use
    assert len(list((home / "logs").glob("clipvault-*.jsonl"))) == 1


def test_python_check_passes_here() -> None:
    assert doctor.check_python().status == doctor.OK


def test_ffmpeg_missing_gives_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    result = doctor.check_ffmpeg()
    assert result.status == doctor.FAIL
    assert "winget install" in result.fix


def test_twitch_downloader_missing_is_a_warning(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    result = doctor.check_twitch_downloader(home)
    assert result.status == doctor.WARN
    assert "github.com/lay295/TwitchDownloader" in result.fix


def test_twitch_downloader_found_in_home_bin(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    (home / "bin").mkdir()
    name = "TwitchDownloaderCLI" + (".exe" if sys.platform == "win32" else "")
    (home / "bin" / name).write_bytes(b"")
    assert doctor.check_twitch_downloader(home).status == doctor.OK


def test_config_check_states(home: Path) -> None:
    path = home / "config.yaml"
    result, loaded = doctor.check_config(path)
    assert result.status == doctor.WARN and loaded.quality == "720p"

    path.write_text("quality: 1080p\n", encoding="utf-8")
    result, loaded = doctor.check_config(path)
    assert result.status == doctor.OK and loaded.quality == "1080p"

    path.write_text("qualty: 1080p\n", encoding="utf-8")
    result, _ = doctor.check_config(path)
    assert result.status == doctor.WARN and "qualty" in result.detail

    path.write_text("quality: 4k\n", encoding="utf-8")
    result, _ = doctor.check_config(path)
    assert result.status == doctor.FAIL and "quality" in result.detail


def test_keyring_check_ok_and_leaves_nothing_behind(memory_keyring) -> None:
    assert doctor.check_keyring().status == doctor.OK
    assert memory_keyring.store == {}


def test_keyring_check_fails_without_backend() -> None:
    keyring.set_keyring(fail_backend.Keyring())
    result = doctor.check_keyring()
    assert result.status == doctor.FAIL
    assert "keyrings.alt" in result.fix


def test_disk_check_thresholds(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    library = home / "not" / "created" / "yet"
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: Usage(1000, 500, 500))
    assert doctor.check_disk(library, 15).status == doctor.OK
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: Usage(1000, 900, 100))
    result = doctor.check_disk(library, 15)
    assert result.status == doctor.FAIL and "15%" in result.fix


def test_database_check_creates_and_reports(home: Path) -> None:
    result = doctor.check_database(home / "clipvault.db")
    assert result.status == doctor.OK
    assert "WAL" in result.detail


def test_report_shows_fixes_and_summary() -> None:
    results = [
        doctor.CheckResult("A", doctor.OK, "fine", "unused"),
        doctor.CheckResult("B", doctor.FAIL, "broken", "do this"),
    ]
    report = doctor.format_report(results)
    assert "[FAIL] B  broken" in report
    assert "Fix: do this" in report
    assert "unused" not in report
    assert "1 problem(s) to fix" in report
    assert report.isascii()


def test_twitch_keys_check(memory_keyring) -> None:
    result = doctor.check_twitch_keys()
    assert result.status == doctor.WARN
    assert "secrets set twitch_client_id" in result.fix
    memory_keyring.set_password("clipvault", "twitch_client_id", "a")
    memory_keyring.set_password("clipvault", "twitch_client_secret", "b")
    assert doctor.check_twitch_keys().status == doctor.OK


def test_planned_commands_say_when_they_arrive(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["publish"]) == 2
    assert "M9" in capsys.readouterr().out
