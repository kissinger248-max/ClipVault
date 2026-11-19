"""Folders of finished phone versions: one per streamer, and one with everyone's.

    <library>/Edited clips/Kai Cenat/2026-11-16 21.04 Kai Cenat LOSES IT.mp4
    <library>/Edited clips/All streamers/2026-11-16 21.04 Kai Cenat - Kai Cenat LOSES IT.mp4

Only finished phone versions appear here: never an original, never a half-made video.
Each file is a hard link to the phone version, so it takes no extra disk space; on a
drive that can't hold hard links (FAT/exFAT) it is a copy. Every one is recorded in the
database (``edited_files``): making the clip again replaces it, and deleting the clip
or retention removes it (hard rule 8: only files the app made and tracked). A file
removed by hand stays removed.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from clipvault import config as cfg
from clipvault.library import storage
from clipvault.library.storage import Library
from clipvault.logging_setup import ctx
from clipvault.timeutil import parse_rfc3339

EDITED = "Edited clips"
ALL = "All streamers"
NAME_MAX = 110  # characters of a file name, extension included
_DROP = re.compile(r'[<>"?*]')  # "What?!" -> "What!"
_SPACE = re.compile(r"[:/\\|\x00-\x1f]")  # "Kai: no/yes" -> "Kai no yes"
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{p}{n}" for p in ("com", "lpt") for n in "123456789"
}

log = logging.getLogger(__name__)


def safe_name(text: str, fallback: str = "clip") -> str:
    """A file or folder name that works on Windows and macOS."""
    name = _SPACE.sub(" ", _DROP.sub("", text or ""))
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = fallback
    if name.lower() in _WINDOWS_RESERVED:
        name = f"_{name}"
    return name


def streamer_folder_name(display_name: str | None, login: str) -> str:
    from clipvault.process.headlines import spoken_name

    return safe_name(spoken_name(display_name or login), login)


def folder(library: Library, streamer: sqlite3.Row | None = None) -> Path:
    """The edited-clips folder for one streamer, or the one with everyone's."""
    base = library.root / EDITED
    if streamer is None:
        return base / ALL
    return base / streamer_folder_name(streamer["display_name"], streamer["login"])


def _names(clip: sqlite3.Row, tz: ZoneInfo) -> tuple[str, str]:
    """File names for the streamer's folder and for the all-streamers folder."""
    when = parse_rfc3339(clip["created_at"]).astimezone(tz) if clip["created_at"] else None
    stamp = f"{when:%Y-%m-%d %H.%M} " if when else ""
    who = streamer_folder_name(clip["display_name"], clip["login"])
    headline = safe_name(clip["headline"] or clip["title"] or "clip")
    own = f"{stamp}{headline}"
    everyone = f"{stamp}{who} - {headline}"
    return _fit(own) + ".mp4", _fit(everyone) + ".mp4"


def _fit(stem: str) -> str:
    return stem[: NAME_MAX - 4].rstrip(" .")


def _relpath(library: Library, path: Path) -> str:
    return path.relative_to(library.root).as_posix()


def withdraw(conn: sqlite3.Connection, library: Library, clip_id: str) -> int:
    """Remove a clip's files from the edited folders; returns how many were deleted."""
    removed = 0
    for (relpath,) in conn.execute(
        "SELECT path FROM edited_files WHERE clip_id = ?", (clip_id,)
    ).fetchall():
        path = library.root / relpath
        if library.available and storage.is_inside_library(library, path) and path.is_file():
            try:
                path.unlink()
                removed += 1
            except OSError as exc:  # open in a player: it goes next time
                log.warning("could not remove %s: %s", relpath, exc,
                            extra=ctx(clip=clip_id, job="edited"))  # fmt: skip
                continue
        conn.execute("DELETE FROM edited_files WHERE path = ?", (relpath,))
    return removed


def publish(
    conn: sqlite3.Connection, config: cfg.Config, library: Library, clip_id: str
) -> list[Path]:
    """Put a clip's newest phone version in its streamer's folder and the all-streamers
    folder (replacing what was there for it). Returns the files made."""
    if not library.available:
        return []  # the drive is away; ``sync`` catches up when it is back
    clip = conn.execute(
        "SELECT c.id, c.created_at, c.headline, c.title, s.login, s.display_name, "
        "(SELECT r.path FROM renders r WHERE r.clip_id = c.id AND r.path IS NOT NULL "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS render "
        "FROM clips c JOIN streamers s ON s.id = c.streamer_id WHERE c.id = ?",
        (clip_id,),
    ).fetchone()
    if clip is None or not clip["render"]:
        return []
    source = storage.resolve(library, clip["render"])
    if source is None:
        return []
    withdraw(conn, library, clip_id)
    own_name, all_name = _names(clip, ZoneInfo(config.timezone))
    made = []
    for target in (folder(library, clip) / own_name, folder(library) / all_name):
        target = _free_name(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            _link_or_copy(source, target)
        except OSError as exc:
            log.warning("could not add to the edited folder: %s", exc,
                        extra=ctx(clip=clip_id, streamer=clip["login"], job="edited"))  # fmt: skip
            continue
        conn.execute("INSERT OR REPLACE INTO edited_files (path, clip_id) VALUES (?, ?)",
                     (_relpath(library, target), clip_id))  # fmt: skip
        made.append(target)
    return made


def _free_name(target: Path) -> Path:
    """``target``, or "name (2).mp4" ... when another clip already has that name."""
    if not target.exists():
        return target
    for n in range(2, 100):
        candidate = target.with_name(f"{target.stem} ({n}){target.suffix}")
        if not candidate.exists():
            return candidate
    return target.with_name(f"{target.stem} ({os.getpid()}){target.suffix}")


def _link_or_copy(source: Path, target: Path) -> None:
    """A hard link (no extra space); a copy where the drive can't hold hard links."""
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def sync(conn: sqlite3.Connection, config: cfg.Config, library: Library, limit: int = 200) -> int:
    """Add phone versions made before these folders existed (or while the drive was
    away). Clips already in the folders are left alone, so a file deleted by hand stays
    deleted. Returns how many clips were added."""
    if not library.available:
        return 0
    rows = conn.execute(
        "SELECT c.id FROM clips c WHERE c.removed_at IS NULL "
        "AND EXISTS (SELECT 1 FROM renders r WHERE r.clip_id = c.id AND r.path IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM edited_files e WHERE e.clip_id = c.id) "
        "ORDER BY c.created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    added = 0
    for row in rows:
        if publish(conn, config, library, row["id"]):
            added += 1
    if added:
        log.info("added older phone versions to the edited folders",
                 extra=ctx(job="edited", count=added))  # fmt: skip
    return added


def open_in_file_manager(path: Path) -> None:
    """Show a folder in Explorer or Finder (on the computer running ClipVault)."""
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
