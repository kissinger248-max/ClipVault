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
    """yt-dlp and pip run as separate processes, which the socket guard can't see."""
    from clipvault.fetch import ytdlp

    def refuse(command, *args, **kwargs):
        raise AssertionError(f"unit test tried to start a real process: {command[:4]}")

    monkeypatch.setattr(ytdlp, "_run", refuse)


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
