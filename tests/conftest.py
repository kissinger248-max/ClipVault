"""Shared fixtures: isolated home folder, in-memory keychain, and no real network."""

from __future__ import annotations

import logging
import socket
import sqlite3
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from clipvault import db

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class MemoryKeyring(KeyringBackend):
    """Keychain stand-in so tests never touch the real OS keychain."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise PasswordDeleteError("not found")
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def fresh_process_state():
    """In-memory state of the running app (panel key cache, live view) starts empty."""
    from clipvault import activity
    from clipvault.web import auth

    auth.forget_cache()
    activity.reset()
    yield
    auth.forget_cache()
    activity.reset()


@pytest.fixture(autouse=True)
def memory_keyring() -> MemoryKeyring:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard rule 9: unit tests never reach the internet. Loopback stays allowed."""
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host in _LOOPBACK:
            return real_connect(self, address, *args, **kwargs)
        raise RuntimeError(f"unit test tried to open a network connection to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def no_real_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """yt-dlp, pip and FFmpeg run as separate processes, which the socket guard can't see.

    Every module that starts a process does it through its own ``_run``; tests
    that need one replace it with a stand-in.
    """
    from clipvault.fetch import ytdlp
    from clipvault.process import transcribe

    def refuse(command, *args, **kwargs):
        raise AssertionError(f"unit test tried to start a real process: {command[:4]}")

    monkeypatch.setattr(ytdlp, "_run", refuse)
    monkeypatch.setattr(transcribe, "_run", refuse)
    from clipvault.render import ffmpeg

    monkeypatch.setattr(ffmpeg, "_run", refuse)


@pytest.fixture(autouse=True)
def no_bundled_twitch_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests start without the built-in "Sign in with Twitch" app ID; sign-in tests set one."""
    from clipvault import config

    monkeypatch.setattr(config, "bundled_twitch_client_id", lambda: None)


@pytest.fixture(autouse=True)
def fake_user_folder(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Make ``~`` point at a temp folder, so the default library root is never the real one."""
    folder = tmp_path_factory.mktemp("userhome")
    monkeypatch.setenv("USERPROFILE", str(folder))
    monkeypatch.setenv("HOME", str(folder))
    return folder


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CLIPVAULT_HOME at a temp folder and undo logging handlers afterwards."""
    monkeypatch.setenv("CLIPVAULT_HOME", str(tmp_path))
    for name in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TWITCH_APP_TOKEN"):
        monkeypatch.delenv(f"CLIPVAULT_{name}", raising=False)
    yield tmp_path
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_clipvault_handler", False):
            root.removeHandler(handler)
            handler.close()


@pytest.fixture
def conn(home: Path) -> sqlite3.Connection:
    """A migrated database in the temp home folder."""
    connection = db.open_db(home / "clipvault.db")
    yield connection
    connection.close()


# --- control panel ------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_lan_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The panel's phone address is made up, never looked up on the real network."""
    monkeypatch.setattr("clipvault.web.settings_routes.lan_address", lambda: "192.168.1.5")


@pytest.fixture
def library(home: Path) -> Path:
    """An empty clip library in the temp home folder, set in config.yaml."""
    from clipvault import config as cfg

    root = home / "library"
    root.mkdir()
    (root / ".clipvault-library").write_text("ClipVault library\n")
    cfg.save_settings({"library_root": str(root)}, home / "config.yaml")
    return root


@pytest.fixture
def client(home: Path, library: Path):
    """The control panel, served in-process (no password set yet)."""
    from fastapi.testclient import TestClient

    from clipvault.web.server import create_app

    db.open_db(home / "clipvault.db").close()
    return TestClient(create_app(home), follow_redirects=False)


@pytest.fixture(autouse=True)
def app_not_stopping():
    """The "app is quitting" signal is process-wide; every test starts without it."""
    from clipvault import lifecycle

    lifecycle.stopping.clear()
    yield
    lifecycle.stopping.clear()
    lifecycle.on_quit(None)
