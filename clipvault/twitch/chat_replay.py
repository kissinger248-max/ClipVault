"""Chat replays of finished streams, downloaded with TwitchDownloaderCLI (SPEC §5.2).

TwitchDownloaderCLI (MIT licence, github.com/lay295/TwitchDownloader) is a
separate program. ClipVault looks for it on the PATH and in ``bin/`` inside the
ClipVault folder. Its ``chatdownload`` verb writes the whole chat of a VOD as
JSON: ``comments[]`` with ``content_offset_seconds`` and ``message.body``.

Chat logs are kept, gzipped, in the library next to the streamer's clips
(``<login>/chat/<date>_<vod id>[_partNN].json.gz``), so the detector can be
re-run. Long streams (some run for two days) are downloaded in pieces of a few
hours with ``--beginning``/``--ending``, so no single file or download is huge.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipvault.discover.vod_spikes import ChatMessage

TOOL = "TwitchDownloaderCLI"
DOWNLOAD_TIMEOUT_S = 45 * 60  # a long, busy stream has hundreds of thousands of messages
# Error texts that mean the chat can never be downloaded.
_PERMANENT_MARKERS = (
    "subscriber",
    "sub-only",
    "not found",
    "does not exist",
    "deleted",
    "unauthorized",
    "forbidden",
    "404",
    "403",
)

Runner = Callable[..., Any]


def _run(*args: Any, **kwargs: Any) -> Any:
    """The one place this module starts a process (tests replace it)."""
    return subprocess.run(*args, **kwargs)


class ChatUnavailable(Exception):
    """The chat could not be downloaded. ``permanent``: it never will be (sub-only, gone)."""

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


def find_tool(home: Path) -> Path | None:
    """Path to TwitchDownloaderCLI, or None when it is not installed."""
    found = shutil.which(TOOL)
    if found:
        return Path(found)
    name = TOOL + (".exe" if sys.platform == "win32" else "")
    local = home / "bin" / name
    return local if local.is_file() else None


def download_chat(
    tool: Path,
    vod_id: str,
    dest: Path,
    begin: int | None = None,
    end: int | None = None,
    *,
    runner: Runner | None = None,
    timeout: float = DOWNLOAD_TIMEOUT_S,
) -> Path:
    """Download a VOD's chat (or ``begin``..``end`` seconds of it) to ``dest`` (``.json.gz``).

    Raises ``ChatUnavailable``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = dest.with_name(dest.name.removesuffix(".gz").removesuffix(".json") + ".part.json")
    command = [
        str(tool), "chatdownload", "--id", vod_id, "--output", str(raw),
        "--timestamp-format", "Relative", "--collision", "Overwrite",
    ]  # fmt: skip
    if begin is not None:
        command += ["--beginning", f"{int(begin)}s"]
    if end is not None:
        command += ["--ending", f"{int(end)}s"]
    try:
        result = (runner or _run)(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raw.unlink(missing_ok=True)
        raise ChatUnavailable(f"chat download took over {timeout / 60:.0f} minutes") from exc
    except OSError as exc:
        raise ChatUnavailable(f"could not start {TOOL}: {exc}") from exc
    if result.returncode != 0 or not raw.is_file():
        raw.unlink(missing_ok=True)
        text = (result.stderr or result.stdout or "").strip().splitlines()
        message = text[-1][:300] if text else f"{TOOL} exited with code {result.returncode}"
        permanent = any(marker in message.lower() for marker in _PERMANENT_MARKERS)
        raise ChatUnavailable(message, permanent=permanent)
    temp = dest.with_name(dest.name + ".tmp")
    with raw.open("rb") as source, gzip.open(temp, "wb") as target:
        shutil.copyfileobj(source, target)
    os.replace(temp, dest)
    raw.unlink(missing_ok=True)  # our own temporary file, inside the library folder
    return dest


def load_chat(path: Path, begin: float = 0.0) -> list[ChatMessage]:
    """Read a chat file (TwitchDownloader JSON, plain or gzipped) into messages.

    ``begin`` is where a piece of chat starts in the VOD. Offsets are expected
    to count from the start of the VOD; if a piece's offsets clearly count from
    the start of the piece instead, they are moved to VOD time.
    """
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    messages = parse_chat(data)
    if begin > 60 and messages and messages[-1].offset < begin - 60:
        messages = [ChatMessage(m.offset + begin, m.text) for m in messages]
    return messages


def parse_chat(data: dict[str, Any]) -> list[ChatMessage]:
    """Messages from TwitchDownloader's JSON, oldest first. Unknown entries are skipped."""
    messages: list[ChatMessage] = []
    for comment in data.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        offset = comment.get("content_offset_seconds")
        message = comment.get("message") or {}
        text = message.get("body") if isinstance(message, dict) else None
        if text is None and isinstance(message, dict):
            text = "".join(f.get("text", "") for f in message.get("fragments") or [])
        if isinstance(offset, (int, float)) and isinstance(text, str):
            messages.append(ChatMessage(float(offset), text))
    messages.sort(key=lambda m: m.offset)
    return messages
