from __future__ import annotations

from pathlib import Path

import pytest

from clipvault import config as cfg
from clipvault import db, twitch
from clipvault.__main__ import main
from tests.twitch_fake import FakeTwitch, make_clip


@pytest.fixture
def fake_twitch(memory_keyring, monkeypatch: pytest.MonkeyPatch) -> FakeTwitch:
    """Stored keys plus a fake Twitch behind ``twitch.build_client``."""
    memory_keyring.set_password("clipvault", "twitch_client_id", "cid")
    memory_keyring.set_password("clipvault", "twitch_client_secret", "secret")
    fake = FakeTwitch()
    real_build = twitch.build_client
    monkeypatch.setattr(twitch, "build_client", lambda: real_build(http=fake.client()))
    return fake


def import_list(home: Path, capsys, text: str) -> None:
    (home / "streamers.txt").write_text(text, encoding="utf-8")
    assert main(["streamers", "import"]) == 0
    capsys.readouterr()


def test_secrets_set_list_delete(monkeypatch: pytest.MonkeyPatch, capsys, memory_keyring) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt: "  abc123  ")
    assert main(["secrets", "set", "twitch_client_id"]) == 0
    assert memory_keyring.store[("clipvault", "twitch_client_id")] == "abc123"

    assert main(["secrets", "list"]) == 0
    out = capsys.readouterr().out
    assert "abc123" not in out  # values are never shown
    assert "twitch_client_id       stored" in out
    assert "twitch_client_secret   not set" in out

    assert main(["secrets", "delete", "twitch_client_id"]) == 0
    assert cfg.get_secret("twitch_client_id") is None


def test_secrets_set_refuses_empty_and_app_managed_names(monkeypatch, capsys) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt: "   ")
    assert main(["secrets", "set", "groq_api_key"]) == 1
    with pytest.raises(SystemExit):
        main(["secrets", "set", "twitch_app_token"])


def test_streamers_import_and_list(home: Path, capsys) -> None:
    assert main(["streamers", "import"]) == 1
    assert "File not found" in capsys.readouterr().out

    # Notepad-style file: UTF-8 marker at the start and Windows line endings.
    (home / "streamers.txt").write_bytes(b"\xef\xbb\xbfAlpha top_n=3\r\nbeta min_views=50\r\n")
    assert main(["streamers", "import"]) == 0
    assert "2 added, 0 updated, 0 unchanged" in capsys.readouterr().out
    assert main(["streamers", "import"]) == 0
    assert "0 added, 0 updated, 2 unchanged" in capsys.readouterr().out

    assert main(["streamers", "list"]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "top_n=3" in out and "min_views=50" in out and "post=off" in out


def test_streamers_import_with_errors_changes_nothing(home: Path, capsys) -> None:
    other = home / "list.txt"
    other.write_text("good\nbad login\nx post=on\n", encoding="utf-8")
    assert main(["streamers", "import", str(other)]) == 1
    out = capsys.readouterr().out
    assert "line 2" in out and "line 3" in out
    conn = db.open_db(cfg.db_path())
    assert conn.execute("SELECT count(*) FROM streamers").fetchone()[0] == 0
    conn.close()


def test_discover_explains_what_is_missing(home: Path, capsys) -> None:
    assert main(["discover"]) == 1
    assert "streamer list is empty" in capsys.readouterr().out

    import_list(home, capsys, "alpha\n")
    assert main(["discover"]) == 1
    assert "dev.twitch.tv/console" in capsys.readouterr().out
    assert main(["discover", "--streamer", "nobody"]) == 1
    assert "not on the streamer list" in capsys.readouterr().out
    assert main(["discover", "--since", "soon"]) == 2


def test_discover_lists_new_clips_then_nothing(home: Path, capsys, fake_twitch: FakeTwitch) -> None:
    fake_twitch.add_user("1001", "alpha")
    recent = _hours_ago(3)
    fake_twitch.clips = [
        make_clip("c1", "1001", recent, 1234, title="café \U0001f525 moment"),
        make_clip("c2", "1001", recent, 7),
    ]
    import_list(home, capsys, "alpha\n")

    assert main(["discover"]) == 0
    out = capsys.readouterr().out
    assert "alpha: 2 new clip(s)" in out
    assert "1,234 views" in out and "https://clips.twitch.tv/c1" in out
    assert "2 new clip(s) across 1 streamer(s); 0 failed." in out

    assert main(["discover"]) == 0
    assert "alpha: 0 new clip(s)" in capsys.readouterr().out
    assert main(["discover", "--streamer", "ALPHA", "--since", "2d"]) == 0
    assert "alpha: 0 new clip(s)" in capsys.readouterr().out


def test_discover_reports_bad_keys(
    home: Path, capsys, fake_twitch: FakeTwitch, memory_keyring
) -> None:
    memory_keyring.set_password("clipvault", "twitch_client_secret", "wrong-secret")
    import_list(home, capsys, "alpha\n")
    assert main(["discover"]) == 1
    out = capsys.readouterr().out
    assert "Twitch problem" in out and "wrong-secret" not in out


def test_bad_config_is_explained_not_a_traceback(
    home: Path, capsys, fake_twitch: FakeTwitch
) -> None:
    (home / "config.yaml").write_text("quality: 4k\n", encoding="utf-8")
    assert main(["discover"]) == 1
    assert "Problem in config.yaml" in capsys.readouterr().out


def _hours_ago(hours: int) -> str:
    from datetime import timedelta

    from clipvault.timeutil import to_rfc3339, utc_now

    return to_rfc3339(utc_now() - timedelta(hours=hours))
