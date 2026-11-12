import json
import logging
from pathlib import Path

from clipvault.logging_setup import ctx, setup_logging


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_logs_are_json_lines_with_context(home: Path) -> None:
    log_file = setup_logging(home / "logs")
    log = logging.getLogger("clipvault.test")
    log.info("plain message")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception("download failed", extra=ctx(clip="Abc123", streamer="somebody", job="fetch"))

    first, second = read_lines(log_file)
    assert first["msg"] == "plain message"
    assert first["level"] == "INFO"
    assert (first["clip"], first["streamer"], first["job"]) == (None, None, None)
    assert (second["clip"], second["streamer"], second["job"]) == ("Abc123", "somebody", "fetch")
    assert second["level"] == "ERROR"
    assert "RuntimeError: boom" in second["exc"]
    assert second["ts"].endswith("+00:00")


def test_setup_twice_does_not_duplicate_lines(home: Path) -> None:
    setup_logging(home / "logs")
    log_file = setup_logging(home / "logs")
    logging.getLogger("clipvault.test").info("once")
    assert len(read_lines(log_file)) == 1


def test_one_file_per_day_and_two_writers_can_share_it(home: Path) -> None:
    from clipvault.logging_setup import DailyFileHandler, JsonFormatter

    log_file = setup_logging(home / "logs")
    assert log_file.name.startswith("clipvault-") and log_file.suffix == ".jsonl"
    # A second process (a CLI command while the app runs) appends to the same file.
    second = DailyFileHandler(home / "logs")
    second.setFormatter(JsonFormatter())
    record = logging.makeLogRecord({"msg": "from the other process", "levelname": "INFO"})
    logging.getLogger("clipvault.test").info("from this process")
    second.emit(record)
    logging.getLogger("clipvault.test").info("again from this process")
    second.close()
    assert [e["msg"] for e in read_lines(log_file)] == [
        "from this process",
        "from the other process",
        "again from this process",
    ]

    old = logging.makeLogRecord({"msg": "long ago", "levelname": "INFO", "created": 86400.0 * 365})
    handler = DailyFileHandler(home / "logs")
    handler.setFormatter(JsonFormatter())
    handler.emit(old)
    handler.close()
    assert len(list((home / "logs").glob("clipvault-*.jsonl"))) == 2


def test_http_libraries_are_quiet(home: Path) -> None:
    setup_logging(home / "logs")
    assert logging.getLogger("httpx").level == logging.WARNING
