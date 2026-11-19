"""Secrets for posting (developer keys, account tokens) in the OS keychain, under ClipVault's own keychain
entry. Values are never logged or returned by the web API.

Pass ``store`` (a dict) in tests; otherwise the ``keyring`` library is used, like the rest of ClipVault."""

from __future__ import annotations

import json

from clipvault import config as cfg

PREFIX = "posting."


class Secrets:
    def __init__(self, store: dict | None = None):
        self.store = store

    def get(self, name: str) -> str | None:
        if self.store is not None:
            return self.store.get(name)
        import keyring

        return keyring.get_password(cfg.keyring_service(), PREFIX + name)

    def set(self, name: str, value: str) -> None:
        if self.store is not None:
            self.store[name] = value
            return
        import keyring

        keyring.set_password(cfg.keyring_service(), PREFIX + name, value)

    def delete(self, name: str) -> None:
        if self.store is not None:
            self.store.pop(name, None)
            return
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(cfg.keyring_service(), PREFIX + name)
        except PasswordDeleteError:
            pass

    def get_json(self, name: str) -> dict | None:
        raw = self.get(name)
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def set_json(self, name: str, value: dict) -> None:
        self.set(name, json.dumps(value))

    def has(self, name: str) -> bool:
        return bool(self.get(name))
