"""JSON-lines logging to ``logs/clipvault.jsonl`` (one file per day, 14 days kept).

Every line carries ``clip``, ``streamer`` and ``job`` keys so an error can always
be traced to what it concerns. Pass them with :func:`ctx`::

    log.error("download failed", extra=ctx(clip=clip_id, streamer=login, job="fetch"))
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

LOG_FILENAME = "clipvault.jsonl"
_HANDLER_TAG = "_clipvault_handler"
_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def ctx(
    clip: str | None = None, streamer: str | None = None, job: str | None = None, **more: Any
) -> dict[str, Any]:
    """Build the ``extra=`` dict for a log call."""
    return {"clip": clip, "streamer": streamer, "job": job, **more}


class JsonFormatter(logging.Formatter):
    """Formats each record as one JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "clip": None,
            "streamer": None,
            "job": None,
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging(log_dir: Path, level: int = logging.INFO) -> Path:
    """Send all logging to a JSON-lines file in ``log_dir``; return the file path.

    Safe to call more than once: earlier ClipVault handlers are replaced.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / LOG_FILENAME
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
            handler.close()

    file_handler = TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=14, encoding="utf-8", delay=True
    )
    file_handler.setFormatter(JsonFormatter())
    setattr(file_handler, _HANDLER_TAG, True)

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    setattr(console, _HANDLER_TAG, True)

    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console)
    return log_file
