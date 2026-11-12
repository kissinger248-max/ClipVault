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
    logging.getLogger().handlers[-2].flush()

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
