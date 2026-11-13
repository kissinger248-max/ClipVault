"""Small builders shared by the library and fetch tests."""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

from clipvault.config import Config, RetentionConfig
from clipvault.fetch.ytdlp import DownloadError


def make_config(tmp: Path, **retention) -> Config:
    """A config whose library lives in the test's temp folder."""
    config = Config(library_root=tmp / "library")
    if retention:
        config = dataclasses.replace(config, retention=RetentionConfig(**retention))
    return config


def add_streamer(conn: sqlite3.Connection, login: str = "alpha", **columns) -> int:
    names = ["login", *columns]
    marks = ", ".join("?" * len(names))
    cursor = conn.execute(
        f"INSERT INTO streamers ({', '.join(names)}) VALUES ({marks})", [login, *columns.values()]
    )
    return cursor.lastrowid


def add_clip(conn: sqlite3.Connection, clip_id: str, streamer_id: int = 1, **columns) -> None:
    """Insert a clip in state ``found`` with sensible defaults; override any column."""
    values = {
        "id": clip_id,
        "streamer_id": streamer_id,
        "source": "viewer",
        "title": f"Title of {clip_id}",
        "created_at": "2026-11-10T20:00:00Z",
        "view_count": 100,
        "duration": 30.0,
        "url": f"https://www.twitch.tv/alpha/clip/{clip_id}",
        "state": "found",
        **columns,
    }
    marks = ", ".join("?" * len(values))
    conn.execute(f"INSERT INTO clips ({', '.join(values)}) VALUES ({marks})", list(values.values()))


class FakeDownloader:
    """Stands in for yt-dlp: writes a small file, or fails as scripted."""

    def __init__(self, size: int = 2048) -> None:
        self.size = size
        self.calls: list[tuple[str, Path, str]] = []
        self.failures: dict[str, DownloadError] = {}  # url fragment -> error to raise
        self.fail_all: DownloadError | None = None

    def __call__(self, url: str, dest: Path, quality: str) -> Path:
        self.calls.append((url, dest, quality))
        for fragment, error in self.failures.items():
            if fragment in url:
                raise error
        if self.fail_all:
            raise self.fail_all
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * self.size)
        return dest


def clip_row(conn: sqlite3.Connection, clip_id: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
