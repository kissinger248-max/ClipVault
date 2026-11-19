"""Twitch for the website: reading pasted links, finding the clips to make, downloading them.

Only the website's own Twitch app is used (``CLIPVAULT_WEB_TWITCH_CLIENT_ID`` /
``_SECRET``), never the desktop app's keys or sign-in, so if Twitch ever blocks the
website the app keeps working (plan section 5, safeguard 1). Discovery uses the official
Helix API; what is downloaded, and how, is decided in :mod:`clipvault_web.making`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from clipvault.discover.vod_spikes import parse_duration
from clipvault.timeutil import parse_rfc3339, utc_now
from clipvault.twitch.api import TwitchAPIError, TwitchClient
from clipvault.twitch.auth import AppTokenProvider, TwitchAuthError

from .settings import WebSettings

MIN_CLIP_S = 15  # Shorts are 15-59 s (hard rule 4): shorter clips are left out
LOOK_BACK = timedelta(days=7)
LOOK_FURTHER = timedelta(days=30)
AROUND_A_CLIP = timedelta(days=3, hours=12)
SAME_MOMENT_S = 20  # two clips of the same VOD starting this close are the same moment
HTTP_TIMEOUT_S = 20
_LOGIN = re.compile(r"^[A-Za-z0-9_]{3,25}$")
_SLUG = re.compile(r"^[A-Za-z0-9_-]{6,120}$")
_NOT_CHANNELS = frozenset(
    "videos directory settings search downloads p jobs login signup subscriptions "
    "inventory wallet drops turbo prime messages friends u moderator".split()
)


class LinkProblem(Exception):
    """The link or name can't be used; the message is shown to the visitor."""


@dataclass(frozen=True)
class Link:
    kind: str  # clip | vod | channel | name
    value: str  # clip slug, VOD id, login or the name typed


@dataclass
class Item:
    """One moment to make a clip from."""

    source_kind: str  # clip | section
    url: str
    title: str = ""
    views: int | None = None
    slug: str | None = None  # Twitch clip id
    vod_id: str | None = None
    start: float = 0.0  # a VOD section: seconds into the VOD
    end: float = 0.0
    duration: float = 0.0
    embed_url: str | None = None
    median: float | None = None  # the channel's usual clip views (headlines)


@dataclass
class Plan:
    """What a Twitch run will make."""

    kind: str  # twitch_clip | twitch_vod | twitch_channel | twitch_mine
    user_id: str
    login: str
    name: str
    title: str  # shown on the run page
    is_live: bool = False
    items: list[Item] = field(default_factory=list)
    more_found: int = 0
    vod_url: str | None = None
    vod_length: float | None = None
    median_views: float | None = None  # the channel's usual clip, for the headlines


# --- links ----------------------------------------------------------------------------------


def parse_link(text: str) -> Link:
    """Work out what was pasted: a clip, a VOD, a channel link, or a streamer's name."""
    raw = (text or "").strip()
    if not raw:
        raise LinkProblem("Paste a Twitch link or type a streamer's name.")
    if _LOGIN.match(raw):
        return Link("name", raw.lower())
    url = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    if host == "clips.twitch.tv" and parts and _SLUG.match(parts[0]) and parts[0] != "embed":
        return Link("clip", parts[0])
    if host.endswith("twitch.tv") and host != "clips.twitch.tv":
        if len(parts) >= 2 and parts[0] == "videos" and parts[1].isdigit():
            return Link("vod", parts[1])
        if len(parts) >= 3 and parts[1] == "clip" and _SLUG.match(parts[2]):
            return Link("clip", parts[2])
        if len(parts) >= 2 and parts[0] == "clip" and _SLUG.match(parts[1]):
            return Link("clip", parts[1])
        if len(parts) >= 1 and _LOGIN.match(parts[0]) and parts[0].lower() not in _NOT_CHANNELS:
            return Link("channel", parts[0].lower())
    raise LinkProblem("That isn't a Twitch clip, video or channel link.")


# --- clients ---------------------------------------------------------------------------------


class _UserToken:
    """A signed-in streamer's token, for their own channel (it is not renewed)."""

    def __init__(self, token: str) -> None:
        self._token = token

    def get(self) -> str:
        return self._token

    def invalidate(self) -> None:
        raise TwitchAuthError("the Twitch sign-in has expired; please sign in again")


def build_client(settings: WebSettings, http: httpx.Client | None = None) -> TwitchClient:
    """A Helix client on the website's own Twitch app (app token kept in memory only)."""
    if not (settings.twitch_client_id and settings.twitch_client_secret):
        raise LinkProblem("Twitch links aren't switched on here yet. You can upload a video.")
    http = http or httpx.Client(timeout=HTTP_TIMEOUT_S)
    store: dict[str, str | None] = {"token": None}
    tokens = AppTokenProvider(
        settings.twitch_client_id, settings.twitch_client_secret, http,
        load=lambda: store["token"], save=lambda value: store.update(token=value),
    )  # fmt: skip
    return TwitchClient(settings.twitch_client_id, tokens, http, max_attempts=3)


def user_client(
    settings: WebSettings, access_token: str, http: httpx.Client | None = None
) -> TwitchClient:
    """A Helix client acting as a signed-in streamer (their own clips, official downloads)."""
    if not settings.twitch_client_id:
        raise LinkProblem("Twitch sign-in isn't switched on here yet.")
    http = http or httpx.Client(timeout=HTTP_TIMEOUT_S)
    return TwitchClient(settings.twitch_client_id, _UserToken(access_token), http, max_attempts=2)


# --- choosing the clips ----------------------------------------------------------------------


def _usable(clip: dict[str, Any]) -> bool:
    return float(clip.get("duration") or 0) >= MIN_CLIP_S


def _item(clip: dict[str, Any]) -> Item:
    return Item(
        source_kind="clip", url=clip["url"], title=clip.get("title") or "",
        views=int(clip.get("view_count") or 0), slug=clip["id"],
        vod_id=clip.get("video_id") or None,
        start=float(clip.get("vod_offset") or 0), duration=float(clip.get("duration") or 0),
        embed_url=clip.get("embed_url"),
    )  # fmt: skip


def best_clips(clips: list[dict[str, Any]], count: int, skip: set[str] = frozenset()) -> list[Item]:
    """The most-watched usable clips, one per moment (two clips of the same moment of a VOD
    count once), at most ``count``."""
    chosen: list[dict[str, Any]] = []
    for clip in sorted(clips, key=lambda c: -int(c.get("view_count") or 0)):
        if len(chosen) >= count:
            break
        if clip["id"] in skip or not _usable(clip):
            continue
        same = any(
            other.get("video_id") and other.get("video_id") == clip.get("video_id")
            and other.get("vod_offset") is not None and clip.get("vod_offset") is not None
            and abs(float(other["vod_offset"]) - float(clip["vod_offset"])) < SAME_MOMENT_S
            for other in chosen
        )  # fmt: skip
        if not same:
            chosen.append(clip)
    return [_item(c) for c in chosen]


def _user(client: TwitchClient, *, login: str | None = None, user_id: str | None = None) -> dict:
    users = client.get_users(logins=[login] if login else [], ids=[user_id] if user_id else [])
    if not users:
        raise LinkProblem(f"There's no Twitch channel called {login}." if login
                          else "That Twitch channel no longer exists.")  # fmt: skip
    return users[0]


def _is_live(client: TwitchClient, user_id: str) -> bool:
    try:
        return user_id in client.get_live_stream_ids([user_id])
    except TwitchAPIError:
        return False


def median_views(clips: list[dict[str, Any]]) -> float | None:
    """The channel's usual clip: the median views of every clip looked at."""
    views = sorted(int(c.get("view_count") or 0) for c in clips)
    if not views:
        return None
    middle = len(views) // 2
    return float(views[middle] if len(views) % 2 else (views[middle - 1] + views[middle]) / 2)


def channel_clips(
    client: TwitchClient, user_id: str, count: int, now: datetime | None = None
) -> tuple[list[Item], int]:
    """A channel's best clips of the last 7 days (30 if the week was quiet), and how many
    more usable ones there are."""
    now = now or utc_now()
    for back in (LOOK_BACK, LOOK_FURTHER):
        clips = list(client.iter_clips(user_id, now - back, now, max_pages=3))
        chosen = best_clips(clips, count)
        if len(chosen) >= count or back == LOOK_FURTHER:
            moments = len(best_clips(clips, len(clips)))  # distinct moments, not duplicate clips
            for item in chosen:
                item.median = median_views(clips)
            return chosen, max(moments - len(chosen), 0)
    return [], 0


def search(client: TwitchClient, query: str, first: int = 8) -> list[dict[str, Any]]:
    """Streamers matching what was typed, for the suggestions under the box."""
    query = (query or "").strip()
    if len(query) < 2:
        return []
    return [
        {"login": c["broadcaster_login"], "name": c.get("display_name") or c["broadcaster_login"],
         "avatar": c.get("thumbnail_url") or "", "live": bool(c.get("is_live")),
         "id": c.get("id")}
        for c in client.search_channels(query, first)
    ]  # fmt: skip


def plan_run(client: TwitchClient, link: Link, count: int, now: datetime | None = None) -> Plan:
    """Find what to make for a pasted link (official API only). Raises ``LinkProblem``."""
    now = now or utc_now()
    try:
        if link.kind == "clip":
            return _plan_clip(client, link.value, count)
        if link.kind == "vod":
            return _plan_vod(client, link.value, count)
        if link.kind == "name":
            found = search(client, link.value)
            exact = [c for c in found if c["login"] == link.value]
            if not (exact or found):
                raise LinkProblem(f"No streamer called {link.value} was found.")
            login = (exact or found)[0]["login"]
        else:
            login = link.value
        user = _user(client, login=login)
        items, more = channel_clips(client, user["id"], count, now)
        if not items:
            raise LinkProblem(f"{user['display_name']} has no clips of at least {MIN_CLIP_S} "
                              "seconds from the last month to make into Shorts.")  # fmt: skip
        return Plan("twitch_channel", user["id"], user["login"], user["display_name"],
                    f"{user['display_name']}: top clips", _is_live(client, user["id"]), items,
                    more)  # fmt: skip
    except TwitchAuthError as exc:
        raise LinkProblem("Twitch isn't answering right now. Please try again shortly.") from exc
    except TwitchAPIError as exc:
        if exc.status and 400 <= exc.status < 500:
            raise LinkProblem("Twitch didn't recognise that link.") from exc
        raise LinkProblem("Twitch isn't answering right now. Please try again shortly.") from exc


def _plan_clip(client: TwitchClient, slug: str, count: int) -> Plan:
    found = client.get_clips([slug])
    if not found:
        raise LinkProblem("That clip doesn't exist any more (or is private).")
    clip = found[0]
    user = _user(client, user_id=clip["broadcaster_id"])
    created = parse_rfc3339(clip["created_at"])
    around = list(client.iter_clips(user["id"], created - AROUND_A_CLIP, created + AROUND_A_CLIP,
                                    max_pages=2))  # fmt: skip
    first = [_item(clip)] if _usable(clip) else []
    others = best_clips([clip, *around] if first else around, count, skip=set())
    others = [i for i in others if i.slug != clip["id"]][: count - len(first)]
    if not first and not others:
        raise LinkProblem(f"That clip is shorter than {MIN_CLIP_S} seconds, the shortest a "
                          "Short can be, and there are no others from that week.")  # fmt: skip
    usable = len(best_clips([clip, *around], len(around) + 1)) - len(first)
    return Plan("twitch_clip", user["id"], user["login"], user["display_name"],
                clip.get("title") or f"A clip of {user['display_name']}",
                _is_live(client, user["id"]), first + others,
                max(usable - len(others), 0),
                median_views=median_views([clip, *around]))  # fmt: skip


def _plan_vod(client: TwitchClient, vod_id: str, count: int) -> Plan:
    found = client.get_videos([vod_id])
    if not found:
        raise LinkProblem("That video doesn't exist any more (or is for subscribers only).")
    video = found[0]
    created = parse_rfc3339(video["created_at"])
    length = parse_duration(video.get("duration") or "0s")
    clips = list(client.iter_clips(video["user_id"], created - timedelta(minutes=5),
                                   created + timedelta(seconds=length) + timedelta(hours=1),
                                   max_pages=5))  # fmt: skip
    mine = [c for c in clips if c.get("video_id") == vod_id]
    items = best_clips(mine, count)
    usable = len(best_clips(mine, len(mine)))
    return Plan("twitch_vod", video["user_id"], video["user_login"], video["user_name"],
                video.get("title") or "A past stream", _is_live(client, video["user_id"]), items,
                max(usable - len(items), 0), vod_url=video.get("url"),
                vod_length=float(length), median_views=median_views(clips))  # fmt: skip
