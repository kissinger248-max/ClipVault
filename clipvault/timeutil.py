"""Time helpers. Every timestamp the app stores is UTC, formatted ``YYYY-MM-DDTHH:MM:SSZ``.

Storing one format everywhere means timestamps sort correctly as plain text in SQLite.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_DURATION_RE = re.compile(r"^(\d+)\s*([mhdw])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def to_rfc3339(moment: datetime) -> str:
    """Format an aware datetime as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC, whole seconds)."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime: every time in ClipVault must carry a time zone")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rfc3339(text: str) -> datetime:
    """Parse an RFC3339 timestamp (as Twitch sends them) into an aware UTC datetime."""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"timestamp has no time zone: {text!r}")
    return moment.astimezone(UTC)


def parse_duration(text: str) -> timedelta:
    """Parse a short duration such as ``30m``, ``12h``, ``7d`` or ``2w``."""
    match = _DURATION_RE.match(text.strip().lower())
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"not a duration: {text!r} (examples: 12h, 7d, 2w)")
    return timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})
