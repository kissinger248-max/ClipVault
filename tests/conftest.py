"""Shared fixtures: an isolated home folder and an in-memory keychain for every test."""

from __future__ import annotations

import logging
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


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
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CLIPVAULT_HOME at a temp folder and undo logging handlers afterwards."""
    monkeypatch.setenv("CLIPVAULT_HOME", str(tmp_path))
    yield tmp_path
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_clipvault_handler", False):
            root.removeHandler(handler)
            handler.close()
