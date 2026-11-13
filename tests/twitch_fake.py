"""An in-memory stand-in for Twitch, served through ``httpx.MockTransport``.

It mimics the documented behaviour the app depends on: bearer-token checks,
clips filtered by time window and sorted by view count, ``first``/``after``
pagination, and lookups limited to 100 ids.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from clipvault.timeutil import parse_rfc3339

FIXTURES = Path(__file__).parent / "fixtures" / "twitch"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_clip(
    clip_id: str,
    broadcaster_id: str,
    created_at: str,
    views: int,
    *,
    title: str | None = None,
    game_id: str = "33214",
    video_id: str = "v1",
    vod_offset: int | None = 100,
    duration: float = 30.0,
) -> dict:
    """Build a clip object shaped like Get Clips returns it."""
    return {
        "id": clip_id,
        "url": f"https://clips.twitch.tv/{clip_id}",
        "embed_url": f"https://clips.twitch.tv/embed?clip={clip_id}",
        "broadcaster_id": broadcaster_id,
        "broadcaster_name": "Fixture",
        "creator_id": "42",
        "creator_name": "SomeViewer",
        "video_id": video_id,
        "game_id": game_id,
        "language": "en",
        "title": title if title is not None else f"title of {clip_id}",
        "view_count": views,
        "created_at": created_at,
        "thumbnail_url": "https://example.test/thumb.jpg",
        "duration": duration,
        "vod_offset": vod_offset,
        "is_featured": False,
    }


class FakeTwitch:
    """Holds users, games and clips, and answers Helix requests about them."""

    def __init__(self) -> None:
        self.users: list[dict] = []
        self.games: dict[str, str] = {}
        self.clips: list[dict] = []
        self.valid_tokens: set[str] = set()
        self.tokens_issued = 0
        self.requests: list[httpx.Request] = []
        self.fail_clips_for: set[str] = set()  # broadcaster ids that answer HTTP 500
        self.videos: list[dict] = []
        self.live: dict[str, str] = {}  # user id -> stream id of a broadcast going on now

    def add_user(self, user_id: str, login: str, display_name: str | None = None) -> None:
        self.users.append(
            {
                "id": user_id,
                "login": login,
                "display_name": display_name or login.title(),
                "profile_image_url": f"https://example.test/{login}.png",
            }
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]

    # --- request handling --------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "id.twitch.tv":
            return self._token(request)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.valid_tokens or request.headers.get("Client-Id") != "cid":
            return httpx.Response(401, json={"status": 401, "message": "Invalid OAuth token"})
        params = parse_qs(request.url.query.decode())
        route = {
            "/helix/users": self._users,
            "/helix/games": self._games,
            "/helix/clips": self._clips,
            "/helix/videos": self._videos,
            "/helix/streams": self._streams,
        }
        return route[request.url.path](params)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        if form.get("client_secret") != ["secret"] or form.get("client_id") != ["cid"]:
            return httpx.Response(403, json={"status": 403, "message": "invalid client secret"})
        self.tokens_issued += 1
        token = f"token-{self.tokens_issued}"
        self.valid_tokens.add(token)
        return httpx.Response(
            200, json={"access_token": token, "expires_in": 5_000_000, "token_type": "bearer"}
        )

    def _users(self, params: dict) -> httpx.Response:
        logins, ids = params.get("login", []), params.get("id", [])
        if not logins and not ids or len(logins) + len(ids) > 100:
            return httpx.Response(400, json={"status": 400, "message": "bad id/login count"})
        data = [u for u in self.users if u["login"] in logins or u["id"] in ids]
        return httpx.Response(200, json={"data": data})

    def _games(self, params: dict) -> httpx.Response:
        data = [{"id": i, "name": self.games[i]} for i in params.get("id", []) if i in self.games]
        return httpx.Response(200, json={"data": data})

    def _clips(self, params: dict) -> httpx.Response:
        broadcaster = params["broadcaster_id"][0]
        if broadcaster in self.fail_clips_for:
            return httpx.Response(500, json={"status": 500, "message": "boom"})
        start, end = self._minute(params["started_at"][0]), self._minute(params["ended_at"][0])
        matching = [
            c
            for c in self.clips
            if c["broadcaster_id"] == broadcaster and start <= parse_rfc3339(c["created_at"]) <= end
        ]
        matching.sort(key=lambda c: -c["view_count"])
        first = int(params.get("first", ["20"])[0])
        offset = int(params.get("after", ["0"])[0])
        page = matching[offset : offset + first]
        more = offset + first < len(matching)
        pagination = {"cursor": str(offset + first)} if more else {}
        return httpx.Response(200, json={"data": page, "pagination": pagination})

    def add_video(
        self, video_id: str, user_id: str, created_at: str, duration: str, stream_id: str = ""
    ) -> None:
        self.videos.append(
            {
                "id": video_id,
                "stream_id": stream_id or f"s{video_id}",
                "user_id": user_id,
                "title": f"Stream {video_id}",
                "created_at": created_at,
                "url": f"https://www.twitch.tv/videos/{video_id}",
                "type": "archive",
                "duration": duration,
                "muted_segments": None,
            }
        )

    def _videos(self, params: dict) -> httpx.Response:
        user = params["user_id"][0]
        data = [v for v in self.videos if v["user_id"] == user]
        data.sort(key=lambda v: v["created_at"], reverse=True)
        return httpx.Response(200, json={"data": data[: int(params["first"][0])], "pagination": {}})

    def _streams(self, params: dict) -> httpx.Response:
        users = params.get("user_id", [])
        data = [{"id": sid, "user_id": uid, "type": "live"} for uid, sid in self.live.items()
                if uid in users]  # fmt: skip
        return httpx.Response(200, json={"data": data, "pagination": {}})

    @staticmethod
    def _minute(text: str) -> datetime:
        return parse_rfc3339(text).replace(second=0, microsecond=0)
