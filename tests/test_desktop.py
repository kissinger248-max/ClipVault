"""The installed app: its data folder, start at login, first run, and quiet tools."""

from __future__ import annotations

import plistlib
import subprocess
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipvault import alerts, autostart, desktop, selftest
from clipvault import config as cfg
from tests.test_web import csrf, login


@pytest.fixture
def packaged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Pretend to be the PyInstaller build, unpacked in ``tmp_path/app``."""
    bundle = tmp_path / "app"
    bundle.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(bundle / "ClipVault.exe"))
    return bundle


def test_the_installed_app_keeps_its_data_in_the_user_data_folder(
    packaged: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CLIPVAULT_HOME")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert cfg.home_dir() == tmp_path / "local" / "ClipVault"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert cfg.home_dir() == Path.home() / "Library" / "Application Support" / "ClipVault"
    assert cfg.resource_dir() == packaged


def test_clipvault_home_still_wins_for_the_installed_app(packaged: Path, home: Path) -> None:
    assert cfg.home_dir() == home


def test_a_source_checkout_never_sets_start_at_login(conn) -> None:
    assert not autostart.supported()
    autostart.first_run(conn)
    assert alerts.get_setting(conn, autostart.FIRST_RUN_SETTING) is None


class FakeWinreg(types.SimpleNamespace):
    def __init__(self) -> None:
        values: dict[str, str] = {}
        key = types.SimpleNamespace(__enter__=None)

        class Key:
            def __enter__(self):
                return key

            def __exit__(self, *exc):
                return False

        def query(_key, name):
            if name not in values:
                raise FileNotFoundError(name)
            return values[name], 1

        def delete(_key, name):
            if name not in values:
                raise FileNotFoundError(name)
            del values[name]

        super().__init__(
            HKEY_CURRENT_USER=0, KEY_SET_VALUE=2, REG_SZ=1, values=values,
            OpenKey=lambda *args: Key(), QueryValueEx=query,
            SetValueEx=lambda _k, name, _r, _t, value: values.__setitem__(name, value),
            DeleteValue=delete,
        )  # fmt: skip


def test_start_at_login_on_windows_uses_the_run_key(
    packaged: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    assert autostart.supported() and not autostart.is_enabled()
    autostart.set_enabled(True)
    assert fake.values["ClipVault"] == f'"{Path(sys.executable).resolve()}" run --no-browser'
    assert autostart.is_enabled()
    autostart.set_enabled(False)
    autostart.set_enabled(False)  # already off: no error
    assert not autostart.is_enabled()


def test_start_at_login_on_mac_uses_a_launch_agent(
    packaged: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    autostart.set_enabled(True)
    agent = Path.home() / "Library" / "LaunchAgents" / f"{autostart.AGENT_LABEL}.plist"
    plist = plistlib.loads(agent.read_bytes())
    assert plist["RunAtLoad"] is True
    assert plist["ProgramArguments"] == [str(Path(sys.executable).resolve()), "run", "--no-browser"]
    assert autostart.is_enabled()
    autostart.set_enabled(False)
    assert not agent.exists()


def test_first_run_switches_things_on_once(
    packaged: Path, monkeypatch: pytest.MonkeyPatch, conn, home: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    calls: list[bool] = []
    monkeypatch.setattr(autostart, "set_enabled", calls.append)
    monkeypatch.setattr(autostart, "is_enabled", lambda: bool(calls) and calls[-1])
    monkeypatch.setattr(desktop, "local_timezone", lambda: "Europe/Berlin")
    desktop.first_run(conn)
    assert calls == [True]
    assert cfg.load_config(home / "config.yaml").timezone == "Europe/Berlin"
    cfg.save_settings({"timezone": "UTC"}, home / "config.yaml")
    desktop.first_run(conn)  # still on: the entry is refreshed; the time zone is kept
    assert calls == [True, True]
    calls.append(False)  # the user switched it off in Settings
    desktop.first_run(conn)
    assert calls == [True, True, False]
    assert cfg.load_config(home / "config.yaml").timezone == "UTC"


def test_tools_started_by_the_windows_app_get_no_window(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(subprocess.Popen, "__init__", lambda self, *a, **kw: seen.append(kw))
    desktop._hide_child_console_windows()
    subprocess.Popen(["ffmpeg", "-version"])
    subprocess.Popen(["ffmpeg"], stdin=subprocess.PIPE, creationflags=0x200)
    assert seen[0]["creationflags"] & 0x08000000 and seen[0]["stdin"] == subprocess.DEVNULL
    assert seen[1]["creationflags"] == 0x200 | 0x08000000 and seen[1]["stdin"] == subprocess.PIPE


def test_bundled_tools_come_first_on_path(packaged: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (packaged / "tools").mkdir()
    monkeypatch.setenv("PATH", "elsewhere")
    desktop._add_bundled_tools()
    assert sys.modules["os"].environ["PATH"].split(sys.modules["os"].pathsep)[0] == str(
        packaged / "tools"
    )


def test_settings_offer_start_at_login_only_where_it_works(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    assert "Open when I log in" not in client.get("/settings").text
    state = {"on": False}
    monkeypatch.setattr(autostart, "supported", lambda: True)
    monkeypatch.setattr(autostart, "is_enabled", lambda: state["on"])
    monkeypatch.setattr(autostart, "set_enabled", lambda on: state.update(on=on))
    assert "Open when I log in" in client.get("/settings").text
    client.post("/settings/schedule", data={"csrf": csrf(client), "interval": "30",
                                            "timezone": "UTC", "autostart_on": "1"})  # fmt: skip
    assert state["on"] is True
    client.post("/settings/schedule", data={"csrf": csrf(client), "interval": "30",
                                            "timezone": "UTC"})  # fmt: skip
    assert state["on"] is False


def test_selftest_fails_when_a_check_fails(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(
        selftest,
        "CHECKS",
        [
            ("Good", lambda: "fine"),
            ("Bad", lambda: (_ for _ in ()).throw(RuntimeError("no ffmpeg"))),
        ],
    )
    assert selftest.main() == 1
    out = capsys.readouterr().out
    assert "[ OK ] Good: fine" in out and "[FAIL] Bad: no ffmpeg" in out


def test_a_test_copy_can_use_its_own_keychain_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPVAULT_KEYRING_SERVICE", "clipvault-smoke")
    cfg.set_secret("ntfy_topic", "t")
    import keyring

    assert keyring.get_password("clipvault-smoke", "ntfy_topic") == "t"
    assert keyring.get_password("clipvault", "ntfy_topic") is None


def test_test_copies_never_register_to_start_at_login(
    packaged: Path, monkeypatch: pytest.MonkeyPatch, conn
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("CLIPVAULT_NO_AUTOSTART", "1")
    calls: list[bool] = []
    monkeypatch.setattr(autostart, "set_enabled", calls.append)
    assert not autostart.supported()
    autostart.first_run(conn)
    assert calls == []


def test_the_console_copy_registers_the_windowless_app(
    packaged: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(packaged / "ClipVault-console.exe"))
    autostart.set_enabled(True)
    assert (
        fake.values["ClipVault"] == f'"{(packaged / "ClipVault.exe").resolve()}" run --no-browser'
    )
