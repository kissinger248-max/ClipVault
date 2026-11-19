"""Theo's pre-upload check: caption is grounded in the clip's facts, hashtags match streamer + real category,
no blocked tags, platform length limits met, and the video fits the platform. Any problem blocks the upload."""

from __future__ import annotations

import re
from pathlib import Path

from clipvault.posting import captiongen
from clipvault.posting.captiongen import LIMITS
from clipvault.posting.uploaders.base import ClipFile

MAX_SECONDS = {"tiktok": 600, "youtube": 180, "instagram": 900}
CODECS = {"h264", "hevc"}
CAPTION_KEY = {"tiktok": ["tiktok"], "youtube": ["youtube_title", "youtube_description"], "instagram": ["instagram"]}


def check_video(clip: ClipFile, platform: str, codec: str | None = None) -> list[str]:
    out = []
    if not clip.path.exists():
        return ["The video file is missing."]
    if abs(clip.width / max(clip.height, 1) - 9 / 16) > 0.02:
        out.append(f"Not vertical 9:16 ({clip.width}x{clip.height}).")
    if clip.duration > MAX_SECONDS[platform]:
        out.append(f"Too long for {platform} ({clip.duration:.0f}s, max {MAX_SECONDS[platform]}s).")
    if codec and codec not in CODECS:
        out.append(f"Unsupported video codec {codec}.")
    if clip.path.suffix.lower() != ".mp4":
        out.append("Not an .mp4 file.")
    return out


def check_caption(package: dict, facts: dict, platform: str, rules: dict | None = None) -> list[str]:
    rules = rules or captiongen.load_rules()
    out = []
    per = package.get("per_platform") or {}
    texts = [per.get(k, "") for k in CAPTION_KEY[platform]]
    if not all(texts):
        return ["No caption was generated for this platform."]
    block = {b.lower() for b in rules.get("blocklist", [])}
    for key, text in zip(CAPTION_KEY[platform], texts):
        limit = LIMITS.get(key, 5000)
        if len(text) > limit:
            out.append(f"Caption is {len(text)} characters; {key} allows {limit}.")
        tags = re.findall(r"#\w+", text)
        for t in tags:
            if t.lower() in block:
                out.append(f"Blocked hashtag {t}.")
    body = texts[-1]
    tags = {t.lower() for t in re.findall(r"#\w+", body)}
    login = captiongen.slug(facts.get("login") or "")
    if login and "#" + login not in tags:
        out.append(f"Missing the streamer tag #{login}.")
    if "#twitch" not in tags:
        out.append("Missing #twitch.")
    game = facts.get("game")
    allowed_game = set()
    if game:
        mapped = {k.lower(): v for k, v in (rules.get("games") or {}).items()}.get(game.lower())
        allowed_game = set(mapped) if mapped else {"#" + captiongen.slug(game)}
    every_game_tag = {t for v in (rules.get("games") or {}).values() for t in v}
    for t in tags & every_game_tag:
        if t not in allowed_game:
            out.append(f"{t} doesn't match the real category ({game or 'unknown'}).")
    if f"twitch.tv/{facts.get('login')}" not in body:
        out.append("Missing the streamer credit line.")
    bad = captiongen.unsupported_terms(package.get("title", "") + " " + package.get("caption", ""), facts)
    if bad:
        out.append("Caption mentions things not in the clip: " + ", ".join(bad[:5]) + ".")
    return out


def check_all(clip: ClipFile, package: dict, facts: dict, platform: str, *, codec: str | None = None) -> list[str]:
    return check_video(clip, platform, codec) + check_caption(package, facts, platform)
