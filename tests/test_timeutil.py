from datetime import UTC, datetime, timedelta, timezone

import pytest

from clipvault import runs
from clipvault.timeutil import parse_duration, parse_rfc3339, to_rfc3339


def test_rfc3339_roundtrip_is_always_utc_z() -> None:
    plus_two = timezone(timedelta(hours=2))
    moment = datetime(2026, 11, 12, 14, 30, 5, 999, tzinfo=plus_two)
    assert to_rfc3339(moment) == "2026-11-12T12:30:05Z"
    assert parse_rfc3339("2026-11-12T12:30:05Z") == datetime(2026, 11, 12, 12, 30, 5, tzinfo=UTC)
    assert parse_rfc3339("2026-11-12T14:30:05.25+02:00").hour == 12


def test_naive_times_are_refused() -> None:
    with pytest.raises(ValueError):
        to_rfc3339(datetime(2026, 11, 12))
    with pytest.raises(ValueError):
        parse_rfc3339("2026-11-12T12:30:05")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30m", timedelta(minutes=30)),
        ("12h", timedelta(hours=12)),
        ("7d", timedelta(days=7)),
        (" 2W ", timedelta(weeks=2)),
    ],
)
def test_parse_duration(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "7", "d", "0d", "-1d", "1.5h", "7 days"])
def test_parse_duration_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)


def test_runs_bookkeeping(conn) -> None:
    assert runs.get_run(conn, "job") is None and runs.get_cursor(conn, "job") is None
    runs.mark_started(conn, "job")
    runs.mark_error(conn, "job", "boom")
    row = runs.get_run(conn, "job")
    assert row["last_started"] and row["last_error"] == "boom" and row["last_success"] is None

    runs.mark_success(conn, "job", cursor="2026-11-12T00:00:00Z")
    row = runs.get_run(conn, "job")
    assert row["last_error"] is None and row["cursor"] == "2026-11-12T00:00:00Z"

    runs.mark_error(conn, "job", "later failure")
    runs.mark_success(conn, "job")  # no cursor given: the old one is kept
    assert runs.get_cursor(conn, "job") == "2026-11-12T00:00:00Z"
