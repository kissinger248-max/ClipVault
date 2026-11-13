"""The library folder: where clip files live and what they are called (SPEC §13).

Layout::

    <library_root>/
      .clipvault-library            marker written when the app first uses the folder
      <streamer_login>/YYYY-MM/
        YYYY-MM-DD_<views>views_<slug-of-title>_<clipid>.mp4
        ...same name....json        sidecar with everything known about the clip
      Favorites/                    files the user moved here are never touched

The database stores paths relative to the library root (forward slashes), so the
library can move to another drive or be synced to another computer.

If the library root was set up before but can't be found now (an unplugged
external drive), new downloads go to a local cache folder and are moved into
the library when it is back.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from clipvault import config as cfg
from clipvault.alerts import get_setting, set_setting
from clipvault.logging_setup import ctx

MARKER = ".clipvault-library"
CACHE_DIRNAME = "library-cache"
FAVORITES = "Favorites"
SLUG_MAX = 40
# Windows refuses paths over 260 characters; leave room for "vertical/" and "_9x16" (M7).
MAX_PATH_CHARS = 240
_ROOT_SETTING = "library.initialised_root"
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{p}{n}" for p in ("com", "lpt") for n in "123456789"
}

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Library:
    """Where clip files are read from and written to right now."""

    root: Path
    cache: Path
    available: bool  # False when the library root can't be found (drive unplugged)

    @property
    def write_base(self) -> Path:
        """Folder new files go into: the library, or the local cache while it is away."""
        return self.root if self.available else self.cache


def open_library(conn: sqlite3.Connection, config: cfg.Config) -> Library:
    """Find the library root, setting it up on first use.

    The root counts as present only when its marker file is there. A root that
    was set up before but has no marker now is treated as away, not re-created:
    otherwise an unplugged drive would silently turn into a new empty library.
    """
    root = config.library_root
    cache = cfg.home_dir() / CACHE_DIRNAME
    marker = root / MARKER
    try:
        if marker.is_file():
            return Library(root, cache, True)
        if get_setting(conn, _ROOT_SETTING) == str(root):
            return Library(root, cache, False)
        root.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            "ClipVault library folder. Do not delete this file: it tells the app the library "
            "is connected.\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("library folder is not reachable: %s", exc, extra=ctx(job="library"))
        return Library(root, cache, False)
    set_setting(conn, _ROOT_SETTING, str(root))
    return Library(root, cache, True)


def slugify(title: str, max_len: int = SLUG_MAX) -> str:
    """Turn a clip title into a short file-name-safe slug (ASCII letters, digits, dashes)."""
    # Split accented letters into letter + accent and drop the accents ("é" -> "e").
    # Everything else that isn't a plain letter or digit (emoji included) becomes a dash.
    decomposed = unicodedata.normalize("NFKD", title)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    slug = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")
    return slug[:max_len].strip("-") or "clip"


def safe_id(clip_id: str) -> str:
    """Make a clip id usable in a file name (VOD cut ids contain colons)."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", clip_id)


def streamer_folder(login: str) -> str:
    """Folder name for a streamer; avoids names Windows reserves (con, com1, ...)."""
    return f"_{login}" if login.lower() in _WINDOWS_RESERVED else login


def clip_relpath(
    login: str,
    clip_id: str,
    title: str,
    view_count: int | None,
    created_at: datetime,
    tz: tzinfo,
    root_chars: int = 0,
) -> str:
    """Return the library-relative path for a clip's video file (SPEC §13 naming).

    The date is the clip's creation date in the user's time zone. ``root_chars``
    is the length of the library root path; the title slug is shortened so the
    full path stays under the Windows limit.
    """
    local = created_at.astimezone(tz)
    folder = f"{streamer_folder(login)}/{local:%Y-%m}"
    # Moments found from chat have no view count: they are labelled "chat" instead.
    views = "chat" if view_count is None else f"{max(view_count, 0)}views"
    fixed = f"{local:%Y-%m-%d}_{views}__{safe_id(clip_id)}.mp4"
    budget = MAX_PATH_CHARS - root_chars - len(folder) - len(fixed) - 2
    slug = slugify(title, max(min(SLUG_MAX, budget), 4))
    return f"{folder}/{local:%Y-%m-%d}_{views}_{slug}_{safe_id(clip_id)}.mp4"


def sidecar_relpath(video_relpath: str) -> str:
    """Return the sidecar path that goes with a video path."""
    return video_relpath.rsplit(".", 1)[0] + ".json"


def resolve(library: Library, relpath: str | None) -> Path | None:
    """Find a library file on disk; ``None`` when it isn't anywhere.

    Looks in the library, then the local cache, then ``Favorites/`` (the user
    may have moved the file there to keep it forever).
    """
    if not relpath:
        return None
    candidates = [library.cache / relpath]
    if library.available:
        name = relpath.rsplit("/", 1)[-1]
        candidates = [library.root / relpath, *candidates, library.root / FAVORITES / name]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def is_favorite(library: Library, path: Path) -> bool:
    """True when ``path`` is inside the library's Favorites folder."""
    return _is_inside(path, library.root / FAVORITES)


def is_inside_library(library: Library, path: Path) -> bool:
    """True when ``path`` really is inside the library root (after resolving links)."""
    return _is_inside(path, library.root)


def _is_inside(path: Path, folder: Path) -> bool:
    try:
        return path.resolve().is_relative_to(folder.resolve())
    except OSError:
        return False


def write_text_atomic(path: Path, text: str) -> None:
    """Write a text file so that it is either the old version or the new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def write_sidecar(path: Path, data: dict[str, Any]) -> None:
    """Write the JSON sidecar next to a clip, replacing any old one in one step."""
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False, default=str))


def relocate_cache(library: Library) -> int:
    """Move cached downloads into the library once it is back; return files moved."""
    if not library.available or not library.cache.is_dir():
        return 0
    moved = 0
    for source in sorted(p for p in library.cache.rglob("*") if p.is_file()):
        if source.suffix in (".part", ".tmp", ".ytdl"):
            continue  # an unfinished download; the fetch step finishes or restarts it
        target = library.root / source.relative_to(library.cache)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        moved += 1
    if moved:
        log.info("moved cached files into the library", extra=ctx(job="library", moved=moved))
    return moved
