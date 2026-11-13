from __future__ import annotations

import pytest

from clipvault import config as cfg
from clipvault import secret_prompt
from clipvault.__main__ import main


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("  abc123xyz \n", "abc123xyz"),
        ("", ""),
        (None, ""),
        ("\x16", ""),  # a Ctrl+V keystroke that was not turned into a paste
        ("abc\x16def", ""),
        ("two words", ""),
    ],
)
def test_clean_secret(raw: str | None, cleaned: str) -> None:
    assert secret_prompt.clean_secret(raw) == cleaned


def test_set_with_window_stores_the_value(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(secret_prompt, "ask_in_window", lambda name: " from-window ")
    assert main(["secrets", "set", "twitch_client_id", "--window"]) == 0
    assert cfg.get_secret("twitch_client_id") == "from-window"
    assert "from-window" not in capsys.readouterr().out


def test_set_with_window_cancelled(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(secret_prompt, "ask_in_window", lambda name: None)
    assert main(["secrets", "set", "twitch_client_id", "--window"]) == 1
    assert "Cancelled" in capsys.readouterr().out
    assert cfg.get_secret("twitch_client_id") is None


def test_failed_paste_is_not_stored(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt: "\x16")
    assert main(["secrets", "set", "twitch_client_secret"]) == 1
    assert "--window" in capsys.readouterr().out
    assert cfg.get_secret("twitch_client_secret") is None
