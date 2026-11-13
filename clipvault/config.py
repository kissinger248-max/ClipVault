"""Settings loader: non-secret settings from ``config.yaml``, secrets from the OS keychain.

The app's home folder holds ``config.yaml``, the database and ``logs/``. It is the
``CLIPVAULT_HOME`` environment variable when set; otherwise the installed app uses
its own data folder (``app_data_dir()``) and a source checkout uses the current
working directory.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

CONFIG_FILENAME = "config.yaml"
DB_FILENAME = "clipvault.db"
KEYRING_SERVICE = "clipvault"


def keyring_service() -> str:
    """The keychain entry name; ``CLIPVAULT_KEYRING_SERVICE`` keeps test copies apart."""
    return os.environ.get("CLIPVAULT_KEYRING_SERVICE") or KEYRING_SERVICE


# Secrets the user types in (SPEC §17); settable with ``clipvault secrets set``.
USER_SECRET_NAMES = (
    "twitch_client_id",
    "twitch_client_secret",
    "groq_api_key",
    "llm_api_key",
    "uploadpost_api_key",
)
# Every secret the app keeps in the keychain, including ones it creates itself.
SECRET_NAMES = (
    *USER_SECRET_NAMES,
    "twitch_app_token",
    "ntfy_topic",
    "panel_password_hash",
    "panel_session_key",
    "twitch_user_token",
)

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class ConfigError(ValueError):
    """Raised when ``config.yaml`` cannot be read or holds an invalid value."""


@dataclass(frozen=True)
class RetentionConfig:
    keep_days_below_median: int = 60
    min_free_space_pct: int = 15


@dataclass(frozen=True)
class PostingConfig:
    review_mode: bool = True
    max_per_account_per_day: int = 10
    spacing_min: tuple[int, int] = (90, 120)
    jitter_min: int = 10
    active_hours: tuple[str, str] = ("08:00", "23:00")


@dataclass(frozen=True)
class LookConfig:
    template: str = "default"
    accent: str = "#FFD400"
    headline_style: str = "box"
    caption_size: int = 72
    show_credit: bool = False  # "@login on Twitch" under the headline


@dataclass(frozen=True)
class NotificationsConfig:
    quiet_hours: tuple[str, str] = ("23:00", "08:00")
    summary_time: str = "09:00"
    server: str = "https://ntfy.sh"


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "groq"  # a name for logs; any OpenAI-style chat endpoint works
    model: str = "openai/gpt-oss-20b"
    base_url: str = "https://api.groq.com/openai/v1"


@dataclass(frozen=True)
class PanelConfig:
    host: str = "127.0.0.1"  # 0.0.0.0 or a Tailscale address makes it reachable from a phone
    port: int = 8765


@dataclass(frozen=True)
class TranscriptionConfig:
    enabled: bool = True
    provider: str = "auto"  # auto | groq | local
    language: str | None = None  # ISO-639-1 code such as "en"; None = detect


@dataclass(frozen=True)
class Config:
    """All non-secret settings. Defaults match SPEC §17."""

    mode: str = "archive"
    timezone: str = "America/Chicago"
    library_root: Path = field(default_factory=lambda: Path("~/Twitch Archive").expanduser())
    quality: str = "720p"
    discover_interval_min: int = 30
    heavy_jobs_on_battery: bool = False
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    posting: PostingConfig = field(default_factory=PostingConfig)
    look: LookConfig = field(default_factory=LookConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    panel: PanelConfig = field(default_factory=PanelConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    # Keys found in the file that the app does not know (likely typos). Not an error.
    unknown_keys: tuple[str, ...] = ()


def is_packaged() -> bool:
    """True when running from the installed app (a PyInstaller build), not from source."""
    return bool(getattr(sys, "frozen", False))


def app_data_dir() -> Path:
    """Where the installed app keeps its settings, database and logs.

    Windows: ``%LOCALAPPDATA%\\ClipVault``; macOS: ``~/Library/Application Support/ClipVault``;
    elsewhere ``~/.local/share/clipvault``. The clip library itself is chosen separately
    (``library_root``, by default ``~/Twitch Archive``).
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ClipVault"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ClipVault"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "clipvault"


def home_dir() -> Path:
    """Return the app's home folder.

    ``CLIPVAULT_HOME`` wins; the installed app uses ``app_data_dir()``; running from
    source uses the current directory.
    """
    if os.environ.get("CLIPVAULT_HOME"):
        return Path(os.environ["CLIPVAULT_HOME"]).expanduser().resolve()
    if is_packaged():
        return app_data_dir()
    return Path.cwd().resolve()


def resource_dir() -> Path:
    """Folder holding files shipped next to the code (``assets/``, bundled ``tools/``)."""
    if is_packaged():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def bundled_twitch_client_id() -> str | None:
    """The public Twitch app ID shipped with ClipVault, used for "Sign in with Twitch".

    Twitch client IDs are public by design. The app is registered as a *public*
    client, so it has no secret: each person signs in with their own Twitch account
    (device code at twitch.tv/activate) and ClipVault uses that sign-in for everything.
    """
    from importlib import resources

    try:
        text = resources.files("clipvault.data").joinpath("twitch_public_client_id.txt")
        return first_setting_line(text.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError):
        return None


def first_setting_line(text: str) -> str | None:
    """The first line that is neither blank nor a ``#`` comment, stripped."""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def own_twitch_keys() -> tuple[str, str] | None:
    """The user's own developer keys (client ID and secret), when both are stored."""
    client_id = get_secret("twitch_client_id")
    secret = get_secret("twitch_client_secret")
    return (client_id, secret) if client_id and secret else None


def twitch_client_id() -> str | None:
    """The Twitch app ID in use: the user's own if they stored one, else the built-in one."""
    return get_secret("twitch_client_id") or bundled_twitch_client_id()


def config_path() -> Path:
    """Return where ``config.yaml`` is expected."""
    return home_dir() / CONFIG_FILENAME


def db_path() -> Path:
    """Return where the SQLite database lives."""
    return home_dir() / DB_FILENAME


def logs_dir() -> Path:
    """Return the folder for JSON-lines logs."""
    return home_dir() / "logs"


def load_config(path: Path | None = None) -> Config:
    """Load settings from ``path`` (default: ``config.yaml`` in the home folder).

    A missing file is fine: every setting has a default. Raises ``ConfigError``
    with a plain-language message when the file is unreadable or a value is invalid.
    """
    path = path or config_path()
    if not path.exists():
        return Config()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if raw is None:
        return Config()
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain 'key: value' settings at the top level")
    return _build(raw)


def save_settings(updates: dict[str, Any], path: Path | None = None) -> Config:
    """Change settings in ``config.yaml`` (dotted keys) and return the new config.

    The values are checked first: nothing is written when one is invalid
    (``ConfigError``). Keys not mentioned keep their values. Note: the file is
    rewritten, so comments in it are lost.
    """
    path = path or config_path()
    raw: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    for key, value in updates.items():
        node = raw
        *parents, last = key.split(".")
        for part in parents:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        if value is None:
            node.pop(last, None)
        else:
            node[last] = list(value) if isinstance(value, tuple) else value
    config = _build(raw)
    text = "# ClipVault settings. Also changed from the control panel's Settings screen.\n"
    text += yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)
    return config


def _build(raw: dict[str, Any]) -> Config:
    reader = _Reader(raw)
    cfg = Config(
        mode=reader.choice("mode", ("archive", "archive_post"), "archive"),
        timezone=reader.timezone("timezone", "America/Chicago"),
        library_root=Path(reader.text("library_root", "~/Twitch Archive")).expanduser(),
        quality=reader.choice("quality", ("720p", "1080p"), "720p"),
        discover_interval_min=reader.integer("discover_interval_min", 30, minimum=1),
        heavy_jobs_on_battery=reader.boolean("heavy_jobs_on_battery", False),
        retention=RetentionConfig(
            keep_days_below_median=reader.integer(
                "retention.keep_days_below_median", 60, minimum=1
            ),
            min_free_space_pct=reader.integer(
                "retention.min_free_space_pct", 15, minimum=0, maximum=90
            ),
        ),
        posting=PostingConfig(
            review_mode=reader.boolean("posting.review_mode", True),
            max_per_account_per_day=reader.integer(
                "posting.max_per_account_per_day", 10, minimum=0, maximum=15
            ),
            spacing_min=reader.int_range("posting.spacing_min", (90, 120)),
            jitter_min=reader.integer("posting.jitter_min", 10, minimum=0),
            active_hours=reader.time_pair("posting.active_hours", ("08:00", "23:00")),
        ),
        look=LookConfig(
            template=reader.text("look.template", "default"),
            accent=reader.color("look.accent", "#FFD400"),
            headline_style=reader.choice("look.headline_style", ("box", "outline"), "box"),
            caption_size=reader.integer("look.caption_size", 72, minimum=56, maximum=96),
            show_credit=reader.boolean("look.show_credit", False),
        ),
        notifications=NotificationsConfig(
            quiet_hours=reader.time_pair("notifications.quiet_hours", ("23:00", "08:00")),
            summary_time=reader.time_of_day("notifications.summary_time", "09:00"),
            server=reader.url("notifications.server", "https://ntfy.sh"),
        ),
        llm=LLMConfig(
            provider=reader.text("llm.provider", "groq"),
            model=reader.text("llm.model", "openai/gpt-oss-20b"),
            base_url=reader.url("llm.base_url", "https://api.groq.com/openai/v1"),
        ),
        panel=PanelConfig(
            host=reader.text("panel.host", "127.0.0.1"),
            port=reader.integer("panel.port", 8765, minimum=1024, maximum=65535),
        ),
        transcription=TranscriptionConfig(
            enabled=reader.boolean("transcription.enabled", True),
            provider=reader.choice("transcription.provider", ("auto", "groq", "local"), "auto"),
            language=reader.optional_text("transcription.language"),
        ),
    )
    return Config(**{**cfg.__dict__, "unknown_keys": reader.unknown_keys()})


class _Reader:
    """Reads dotted keys from the parsed YAML, validating types as it goes."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw
        self._seen: set[str] = set()

    def _get(self, key: str) -> Any:
        self._seen.add(key)
        node: Any = self._raw
        parts = key.split(".")
        for i, part in enumerate(parts):
            if node is None:
                return None
            if not isinstance(node, dict):
                section = ".".join(parts[:i])
                raise ConfigError(f"'{section}' must be a section with indented settings")
            node = node.get(part)
        return node

    def text(self, key: str, default: str) -> str:
        value = self._get(key)
        if value is None:
            return default
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"'{key}' must be text, got {value!r}")
        return value.strip()

    def optional_text(self, key: str) -> str | None:
        value = self._get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"'{key}' must be text, got {value!r}")
        return value.strip()

    def choice(self, key: str, options: tuple[str, ...], default: str) -> str:
        value = self._get(key)
        if value is None:
            return default
        if value not in options:
            raise ConfigError(f"'{key}' must be one of {', '.join(options)}; got {value!r}")
        return value

    def boolean(self, key: str, default: bool) -> bool:
        value = self._get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise ConfigError(f"'{key}' must be true or false, got {value!r}")
        return value

    def integer(
        self, key: str, default: int, minimum: int | None = None, maximum: int | None = None
    ) -> int:
        value = self._get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"'{key}' must be a whole number, got {value!r}")
        if minimum is not None and value < minimum:
            raise ConfigError(f"'{key}' must be at least {minimum}, got {value}")
        if maximum is not None and value > maximum:
            raise ConfigError(f"'{key}' must be at most {maximum}, got {value}")
        return value

    def int_range(self, key: str, default: tuple[int, int]) -> tuple[int, int]:
        value = self._get(key)
        if value is None:
            return default
        ok = (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
        )
        if not ok or value[0] < 0 or value[0] > value[1]:
            raise ConfigError(f"'{key}' must be [low, high] whole numbers, got {value!r}")
        return (value[0], value[1])

    def time_pair(self, key: str, default: tuple[str, str]) -> tuple[str, str]:
        value = self._get(key)
        if value is None:
            return default
        ok = (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(v, str) and _TIME_RE.match(v) for v in value)
        )
        if not ok:
            raise ConfigError(
                f'\'{key}\' must be two quoted 24-hour times like ["08:00", "23:00"], '
                f"got {value!r} (times need quotes in YAML)"
            )
        return (value[0], value[1])

    def time_of_day(self, key: str, default: str) -> str:
        value = self._get(key)
        if value is None:
            return default
        if not isinstance(value, str) or not _TIME_RE.match(value):
            raise ConfigError(
                f"'{key}' must be a quoted 24-hour time like \"09:00\", got {value!r}"
            )
        return value

    def url(self, key: str, default: str) -> str:
        value = self._get(key)
        if value is None:
            return default
        if not isinstance(value, str) or not value.startswith(("https://", "http://")):
            raise ConfigError(
                f"'{key}' must be a web address starting with https://, got {value!r}"
            )
        return value.rstrip("/")

    def color(self, key: str, default: str) -> str:
        value = self._get(key)
        if value is None:
            return default
        if not isinstance(value, str) or not _COLOR_RE.match(value):
            raise ConfigError(f"'{key}' must be a quoted color like \"#FFD400\", got {value!r}")
        return value.upper()

    def timezone(self, key: str, default: str) -> str:
        value = self.text(key, default)
        try:
            ZoneInfo(value)
        except Exception as exc:  # ZoneInfoNotFoundError, ValueError on odd keys
            raise ConfigError(
                f"'{key}' is not a known time zone: {value!r} (example: America/Chicago)"
            ) from exc
        return value

    def unknown_keys(self) -> tuple[str, ...]:
        """Return dotted keys present in the file that nothing asked for."""
        sections = {k.split(".")[0] for k in self._seen if "." in k}
        found: list[str] = []
        for key, value in self._raw.items():
            if key in sections and isinstance(value, dict):
                found += [f"{key}.{sub}" for sub in value if f"{key}.{sub}" not in self._seen]
            elif key not in self._seen:
                found.append(str(key))
        return tuple(sorted(found))


# --- Secrets (OS keychain) -------------------------------------------------


def get_secret(name: str) -> str | None:
    """Return a secret from the OS keychain, or ``None`` when it is not set.

    For local development an environment variable ``CLIPVAULT_<NAME>`` wins
    (for example ``CLIPVAULT_TWITCH_CLIENT_ID``).
    """
    _check_secret_name(name)
    env_value = os.environ.get(f"CLIPVAULT_{name.upper()}")
    if env_value:
        return env_value
    import keyring

    return keyring.get_password(keyring_service(), name)


def set_secret(name: str, value: str) -> None:
    """Store a secret in the OS keychain."""
    _check_secret_name(name)
    import keyring

    keyring.set_password(keyring_service(), name, value)


def delete_secret(name: str) -> None:
    """Remove a secret from the OS keychain; does nothing when it is not set."""
    _check_secret_name(name)
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(keyring_service(), name)
    except PasswordDeleteError:
        pass


def _check_secret_name(name: str) -> None:
    if name not in SECRET_NAMES:
        raise KeyError(f"unknown secret name: {name!r}")
