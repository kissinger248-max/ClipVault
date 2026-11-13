"""JSON-lines logging to ``logs/clipvault-YYYY-MM-DD.jsonl`` (one file per local day).

Every line carries ``clip``, ``streamer`` and ``job`` keys so an error can always
be traced to what it concerns. Pass them with :func:`ctx`::

    log.error("download failed", extra=ctx(clip=clip_id, streamer=login, job="fetch"))

The file for each day is opened by name and appended to; nothing is ever renamed.
That keeps logging safe when two ClipVault processes run at once (the background
app plus a CLI command), which rename-based rotation is not on Windows.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

_HANDLER_TAG = "_clipvault_handler"
_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
# Libraries that log request URLs at INFO; keep them quiet so logs stay small and clean.
_NOISY_LOGGERS = ("httpx", "httpcore", "apscheduler", "urllib3")


def ctx(
    clip: str | None = None, streamer: str | None = None, job: str | None = None, **more: Any
) -> dict[str, Any]:
    """Build the ``extra=`` dict for a log call."""
    return {"clip": clip, "streamer": streamer, "job": job, **more}


def log_file_for(log_dir: Path, day: str) -> Path:
    """Return the log file path for a ``YYYY-MM-DD`` day."""
    return log_dir / f"clipvault-{day}.jsonl"


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


class DailyFileHandler(logging.Handler):
    """Appends each record to the file named after the record's local date."""

    def __init__(self, log_dir: Path) -> None:
        super().__init__()
        self._log_dir = log_dir
        self._day: str | None = None
        self._stream: IO[str] | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            day = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d")
            if day != self._day or self._stream is None:
                self._close_stream()
                self._log_dir.mkdir(parents=True, exist_ok=True)
                self._stream = log_file_for(self._log_dir, day).open("a", encoding="utf-8")
                self._day = day
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def close(self) -> None:
        self.acquire()
        try:
            self._close_stream()
        finally:
            self.release()
        super().close()


def setup_logging(log_dir: Path, level: int = logging.INFO) -> Path:
    """Send all logging to JSON-lines files in ``log_dir``; return today's file path.

    Safe to call more than once: earlier ClipVault handlers are replaced.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
            handler.close()

    file_handler = DailyFileHandler(log_dir)
    file_handler.setFormatter(JsonFormatter())
    setattr(file_handler, _HANDLER_TAG, True)

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    setattr(console, _HANDLER_TAG, True)

    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # "Skipped: maximum number of running instances" just means a long round is
    # still going (say, editing a backlog); that is expected, not a warning.
    logging.getLogger("apscheduler.scheduler").setLevel(logging.ERROR)
    logging.getLogger("apscheduler.executors").setLevel(logging.ERROR)
    return log_file_for(log_dir, datetime.now().strftime("%Y-%m-%d"))
