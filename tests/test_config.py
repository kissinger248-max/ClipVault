from pathlib import Path

import pytest

from clipvault import config as cfg

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


def write(home: Path, text: str) -> Path:
    path = home / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_file_gives_defaults(home: Path) -> None:
    loaded = cfg.load_config()
    assert loaded == cfg.Config()
    assert loaded.mode == "archive"
    assert loaded.quality == "720p"
    assert loaded.retention.min_free_space_pct == 15
    assert loaded.posting.review_mode is True
    assert loaded.posting.spacing_min == (90, 120)


def test_example_file_matches_defaults() -> None:
    assert cfg.load_config(EXAMPLE) == cfg.Config()


def test_partial_file_overrides_only_what_it_sets(home: Path) -> None:
    write(home, "quality: 1080p\nretention:\n  min_free_space_pct: 20\n")
    loaded = cfg.load_config()
    assert loaded.quality == "1080p"
    assert loaded.retention.min_free_space_pct == 20
    assert loaded.retention.keep_days_below_median == 60
    assert loaded.unknown_keys == ()


def test_library_root_expands_home(home: Path) -> None:
    write(home, "library_root: ~/clips\n")
    assert cfg.load_config().library_root == Path("~/clips").expanduser()


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("quality: 4k\n", "quality"),
        ("mode: post\n", "mode"),
        ("timezone: Mars/Olympus\n", "time zone"),
        ("discover_interval_min: soon\n", "whole number"),
        ("posting:\n  active_hours: [08:00, 23:00]\n", "quotes"),
        ("posting:\n  spacing_min: [120, 90]\n", "spacing_min"),
        ("posting:\n  max_per_account_per_day: 40\n", "at most 15"),
        ("look:\n  accent: yellow\n", "color"),
        ("retention: 60\n", "section"),
        ("- just\n- a list\n", "top level"),
        ("mode: [unclosed\n", "not valid YAML"),
    ],
)
def test_invalid_values_raise_clear_errors(home: Path, text: str, needle: str) -> None:
    write(home, text)
    with pytest.raises(cfg.ConfigError, match=needle):
        cfg.load_config()


def test_unknown_keys_are_reported_not_fatal(home: Path) -> None:
    write(home, "qualty: 1080p\nlook:\n  acent: '#FFFFFF'\n")
    loaded = cfg.load_config()
    assert loaded.unknown_keys == ("look.acent", "qualty")
    assert loaded.quality == "720p"


def test_secret_roundtrip_uses_keyring(memory_keyring) -> None:
    assert cfg.get_secret("groq_api_key") is None
    cfg.set_secret("groq_api_key", "abc")
    assert cfg.get_secret("groq_api_key") == "abc"
    assert memory_keyring.store == {("clipvault", "groq_api_key"): "abc"}
    cfg.delete_secret("groq_api_key")
    cfg.delete_secret("groq_api_key")  # second delete is a no-op
    assert cfg.get_secret("groq_api_key") is None


def test_secret_env_override_and_unknown_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPVAULT_NTFY_TOPIC", "from-env")
    assert cfg.get_secret("ntfy_topic") == "from-env"
    with pytest.raises(KeyError):
        cfg.get_secret("not_a_secret")
