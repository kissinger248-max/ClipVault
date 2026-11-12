"""``python -m clipvault doctor``: checks that everything the app needs is in place."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from clipvault import config as cfg
from clipvault import db

OK, WARN, FAIL = "ok", "warn", "fail"
MIN_PYTHON = (3, 12)
_LABELS = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one doctor check."""

    name: str
    status: str  # ok | warn | fail
    detail: str
    fix: str = ""


def check_python() -> CheckResult:
    """Python 3.12 or newer."""
    version = ".".join(str(n) for n in sys.version_info[:3])
    if tuple(sys.version_info[:2]) >= MIN_PYTHON:
        return CheckResult("Python", OK, version)
    return CheckResult(
        "Python",
        FAIL,
        f"{version} is too old",
        "Install Python 3.12 or newer from https://www.python.org/downloads/ "
        "and recreate the virtual environment.",
    )


def check_ffmpeg() -> CheckResult:
    """FFmpeg on PATH and able to run."""
    fix = (
        "Install FFmpeg and make sure it is on PATH. Windows: 'winget install Gyan.FFmpeg' "
        "or 'scoop install ffmpeg'. macOS: 'brew install ffmpeg'. "
        "Linux: 'sudo apt install ffmpeg'. Then open a new terminal."
    )
    path = shutil.which("ffmpeg")
    if not path:
        return CheckResult("FFmpeg", FAIL, "not found on PATH", fix)
    try:
        out = subprocess.run(
            [path, "-version"], capture_output=True, text=True, timeout=15, check=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("FFmpeg", FAIL, f"found at {path} but would not run: {exc}", fix)
    first_line = (out.stdout.splitlines() or ["unknown version"])[0]
    return CheckResult("FFmpeg", OK, first_line.split(" Copyright")[0])


def check_ytdlp() -> CheckResult:
    """yt-dlp Python module installed in this environment."""
    try:
        version = metadata.version("yt-dlp")
    except metadata.PackageNotFoundError:
        return CheckResult(
            "yt-dlp",
            FAIL,
            "Python module not installed in this environment",
            'Run: pip install -U yt-dlp   (or reinstall with: pip install -e ".[dev]")',
        )
    return CheckResult("yt-dlp", OK, f"version {version}")


def find_twitch_downloader(home: Path) -> str | None:
    """Return the path to TwitchDownloaderCLI (PATH first, then ``<home>/bin``)."""
    found = shutil.which("TwitchDownloaderCLI")
    if found:
        return found
    for name in ("TwitchDownloaderCLI.exe", "TwitchDownloaderCLI"):
        candidate = home / "bin" / name
        if candidate.is_file():
            return str(candidate)
    return None


def check_twitch_downloader(home: Path) -> CheckResult:
    """TwitchDownloaderCLI binary; only needed for VOD chat moments, so a warning."""
    path = find_twitch_downloader(home)
    if path:
        return CheckResult("TwitchDownloaderCLI", OK, path)
    return CheckResult(
        "TwitchDownloaderCLI",
        WARN,
        "not found (only needed for VOD chat-spike moments)",
        "Download the CLI zip for your system from "
        "https://github.com/lay295/TwitchDownloader/releases and put "
        f"TwitchDownloaderCLI in {home / 'bin'} or anywhere on PATH.",
    )


def check_config(path: Path) -> tuple[CheckResult, cfg.Config]:
    """config.yaml readable and valid. Returns defaults alongside a failure."""
    if not path.exists():
        return (
            CheckResult(
                "Config file",
                WARN,
                f"{path} not found; using defaults",
                "Copy config.example.yaml to config.yaml and edit it (optional).",
            ),
            cfg.Config(),
        )
    try:
        loaded = cfg.load_config(path)
    except cfg.ConfigError as exc:
        return (
            CheckResult("Config file", FAIL, str(exc), f"Fix the setting in {path}."),
            cfg.Config(),
        )
    if loaded.unknown_keys:
        return (
            CheckResult(
                "Config file",
                WARN,
                f"{path} has settings the app doesn't know: {', '.join(loaded.unknown_keys)}",
                "Check for typos against config.example.yaml.",
            ),
            loaded,
        )
    return CheckResult("Config file", OK, str(path)), loaded


def check_keyring() -> CheckResult:
    """OS keychain usable: store, read back and remove a throwaway value."""
    fix = (
        "ClipVault stores keys in the OS keychain. Windows and macOS work out of the box. "
        "On a Linux server install a backend: 'pip install keyrings.alt' "
        "(or set up Secret Service / gnome-keyring)."
    )
    try:
        import keyring
        from keyring.backends import fail as fail_backend

        backend = keyring.get_keyring()
        if isinstance(backend, fail_backend.Keyring):
            return CheckResult("Keyring", FAIL, "no keychain backend available", fix)
        probe_user, probe_value = "doctor_probe", "ok"
        keyring.set_password(cfg.KEYRING_SERVICE, probe_user, probe_value)
        try:
            read_back = keyring.get_password(cfg.KEYRING_SERVICE, probe_user)
        finally:
            keyring.delete_password(cfg.KEYRING_SERVICE, probe_user)
    except Exception as exc:  # any backend error means secrets can't be stored
        return CheckResult("Keyring", FAIL, f"keychain error: {exc}", fix)
    if read_back != probe_value:
        return CheckResult("Keyring", FAIL, "keychain did not return the stored value", fix)
    return CheckResult("Keyring", OK, f"{type(backend).__module__}.{type(backend).__name__}")


def check_disk(library_root: Path, min_free_pct: int) -> CheckResult:
    """Free space on the drive that will hold the library."""
    probe = library_root
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        return CheckResult(
            "Disk space",
            FAIL,
            f"cannot read the drive for {library_root}: {exc}",
            "Check that the drive is connected, or change library_root in config.yaml.",
        )
    free_gb = usage.free / 1024**3
    free_pct = 100 * usage.free / usage.total
    detail = f"{free_gb:.1f} GB free ({free_pct:.0f}%) for library at {library_root}"
    if free_pct < min_free_pct:
        return CheckResult(
            "Disk space",
            FAIL,
            detail,
            f"Downloads pause below {min_free_pct}% free. Free up space, or point "
            "library_root in config.yaml at a bigger drive.",
        )
    return CheckResult("Disk space", OK, detail)


def check_database(path: Path) -> CheckResult:
    """Database opens in WAL mode and the schema is current."""
    try:
        conn = db.open_db(path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            count = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:
        return CheckResult(
            "Database",
            FAIL,
            f"cannot open {path}: {exc}",
            "Check that the folder is writable and no other copy of ClipVault is running.",
        )
    return CheckResult("Database", OK, f"{path} ({mode.upper()} mode, {count} migration(s))")


def run_checks(home: Path | None = None) -> list[CheckResult]:
    """Run every check and return the results in display order."""
    home = home or cfg.home_dir()
    config_result, loaded = check_config(home / cfg.CONFIG_FILENAME)
    return [
        check_python(),
        check_ffmpeg(),
        check_ytdlp(),
        check_twitch_downloader(home),
        config_result,
        check_keyring(),
        check_disk(loaded.library_root, loaded.retention.min_free_space_pct),
        check_database(home / cfg.DB_FILENAME),
    ]


def format_report(results: list[CheckResult]) -> str:
    """Render results as plain text (ASCII only, safe for any console)."""
    width = max(len(r.name) for r in results)
    lines = ["ClipVault doctor", ""]
    for r in results:
        lines.append(f"{_LABELS[r.status]} {r.name.ljust(width)}  {r.detail}")
        if r.status != OK and r.fix:
            lines.append(f"       {' ' * width}  Fix: {r.fix}")
    fails = sum(r.status == FAIL for r in results)
    warns = sum(r.status == WARN for r in results)
    lines.append("")
    if fails:
        lines.append(f"{fails} problem(s) to fix, {warns} warning(s).")
    elif warns:
        lines.append(f"Ready, with {warns} warning(s).")
    else:
        lines.append("All checks passed.")
    return "\n".join(lines)


def main() -> int:
    """Run the doctor; exit code 1 when any check failed."""
    results = run_checks()
    print(format_report(results))
    return 1 if any(r.status == FAIL for r in results) else 0
