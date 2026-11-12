"""The streamer list: text-file import and Twitch profile caching.

``streamers.txt`` format, one streamer per line::

    # comments and blank lines are ignored
    somelogin
    otherlogin top_n=10
    https://www.twitch.tv/thirdlogin min_views=500 vod_moments=5 save=off

Posting permission is deliberately NOT settable here (hard rule 1): it is
recorded through the control panel, one streamer at a time.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from clipvault.logging_setup import ctx
from clipvault.timeutil import to_rfc3339, utc_now
from clipvault.twitch.api import TwitchClient

PROFILE_MAX_AGE = timedelta(days=7)

_LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,24}$")
_URL_PREFIX_RE = re.compile(r"^(https?://)?(www\.|m\.)?twitch\.tv/", re.IGNORECASE)
_TRUE = {"on", "true", "yes", "1"}
_FALSE = {"off", "false", "no", "0"}

log = logging.getLogger(__name__)


class StreamersFileError(ValueError):
    """The streamers file has problems; the message lists every bad line."""


@dataclass(frozen=True)
class StreamerEntry:
    """One line of the streamers file: a login and the settings given for it."""

    login: str
    settings: dict[str, int | None] = field(default_factory=dict)


@dataclass(frozen=True)
class ImportResult:
    """What an import changed."""

    added: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]


def normalize_login(text: str) -> str:
    """Turn ``Login``, ``@login`` or a twitch.tv URL into a lowercase login.

    Raises ``ValueError`` when the result is not a possible Twitch login.
    """
    login = _URL_PREFIX_RE.sub("", text.strip()).strip("/").lstrip("@").lower()
    if not _LOGIN_RE.match(login):
        raise ValueError(f"{text!r} is not a Twitch login (letters, digits, _; up to 25)")
    return login


def parse_streamers_text(text: str) -> list[StreamerEntry]:
    """Parse the contents of a streamers file.

    Raises ``StreamersFileError`` naming every bad line, so they can all be fixed in one go.
    """
    entries: dict[str, StreamerEntry] = {}
    problems: list[str] = []
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        first, *rest = line.split()
        try:
            login = normalize_login(first)
            settings = dict(_parse_setting(token) for token in rest)
        except ValueError as exc:
            problems.append(f"line {number}: {exc}")
            continue
        if login in entries:
            problems.append(f"line {number}: {login} is listed twice")
            continue
        entries[login] = StreamerEntry(login, settings)
    if problems:
        raise StreamersFileError("\n".join(problems))
    return list(entries.values())


def _parse_setting(token: str) -> tuple[str, int | None]:
    key, sep, value = token.partition("=")
    key, value = key.strip().lower(), value.strip().lower()
    if not sep or not value:
        raise ValueError(f"expected key=value, got {token!r}")
    if key == "save":
        if value in _TRUE | _FALSE:
            return "save_enabled", int(value in _TRUE)
        raise ValueError(f"save must be on or off, got {value!r}")
    if key == "min_views" and value in ("none", "off"):
        return "min_views", None
    limits = {"top_n": (1, 100), "min_views": (0, None), "vod_moments": (0, 50)}
    if key not in limits:
        if key.startswith(("post", "permission")):
            raise ValueError(
                f"{key} can't be set from a file; posting permission is recorded "
                "per streamer in the control panel"
            )
        raise ValueError(f"unknown setting {key!r} (allowed: top_n, min_views, vod_moments, save)")
    low, high = limits[key]
    if not value.isdigit() or int(value) < low or (high is not None and int(value) > high):
        span = f"{low}-{high}" if high is not None else f"{low} or more"
        raise ValueError(f"{key} must be a whole number ({span}), got {value!r}")
    return key, int(value)


def import_streamers(conn: sqlite3.Connection, entries: list[StreamerEntry]) -> ImportResult:
    """Add new streamers and apply the settings given in the file.

    Only settings written on a streamer's line are changed; streamers missing
    from the file are left alone (nothing is ever removed by an import).
    Call inside ``db.transaction`` to make the whole import all-or-nothing.
    """
    added, updated, unchanged = [], [], []
    for entry in entries:
        row = conn.execute("SELECT * FROM streamers WHERE login = ?", (entry.login,)).fetchone()
        if row is None:
            columns = ["login", "created_at", *entry.settings]
            values = [entry.login, to_rfc3339(utc_now()), *entry.settings.values()]
            conn.execute(
                f"INSERT INTO streamers ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' * len(columns))})",
                values,
            )
            added.append(entry.login)
            continue
        changes = {k: v for k, v in entry.settings.items() if row[k] != v}
        if changes:
            assignments = ", ".join(f"{column} = ?" for column in changes)
            conn.execute(
                f"UPDATE streamers SET {assignments} WHERE id = ?", [*changes.values(), row["id"]]
            )
            updated.append(entry.login)
        else:
            unchanged.append(entry.login)
    return ImportResult(tuple(added), tuple(updated), tuple(unchanged))


def refresh_profiles(
    conn: sqlite3.Connection, client: TwitchClient, now: datetime | None = None
) -> list[str]:
    """Fill in or refresh Twitch id, display name and avatar; return logins Twitch doesn't know.

    New streamers are looked up by login. Known ones are re-checked by id once a
    week, which also follows a streamer who changed their login.
    """
    now = now or utc_now()
    stale_before = to_rfc3339(now - PROFILE_MAX_AGE)
    rows = conn.execute("SELECT id, login, twitch_user_id, profile_checked_at FROM streamers")
    new = []
    stale = []
    for row in rows.fetchall():
        if not row["twitch_user_id"]:
            new.append(row)
        elif (row["profile_checked_at"] or "") < stale_before:
            stale.append(row)
    if not new and not stale:
        return []

    users = client.get_users(
        logins=[r["login"] for r in new], ids=[r["twitch_user_id"] for r in stale]
    )
    by_login = {u["login"].lower(): u for u in users}
    by_id = {u["id"]: u for u in users}
    checked_at = to_rfc3339(now)
    missing: list[str] = []
    for row in new + stale:
        user = by_id.get(row["twitch_user_id"]) if row["twitch_user_id"] else None
        user = user or by_login.get(row["login"])
        if user is None:
            if not row["twitch_user_id"]:
                missing.append(row["login"])
                log.warning(
                    "Twitch has no user with this login (typo, renamed, suspended or deleted)",
                    extra=ctx(streamer=row["login"], job="discover"),
                )
            continue
        try:
            conn.execute(
                "UPDATE streamers SET twitch_user_id = ?, login = ?, display_name = ?, "
                "avatar_url = ?, profile_checked_at = ? WHERE id = ?",
                (
                    user["id"],
                    user["login"].lower(),
                    user.get("display_name"),
                    user.get("profile_image_url"),
                    checked_at,
                    row["id"],
                ),
            )
        except sqlite3.IntegrityError:
            # The streamer's new login is already on the list as a separate row.
            log.warning(
                "streamer changed login to one already on the list; remove the duplicate",
                extra=ctx(streamer=row["login"], job="discover", new_login=user["login"]),
            )
    return missing
