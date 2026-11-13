"""Filtering rules (SPEC §7): repeated moments, posting candidates and the clip score."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

OVERLAP_THRESHOLD = 0.5


@dataclass(frozen=True)
class Span:
    """The stretch of a VOD that a clip covers."""

    id: str
    vod_id: str | None
    start: float | None  # seconds into the VOD
    duration: float
    view_count: int
    created_at: str
    settled: bool = False  # already downloaded: it stays, whatever its views


def overlap_fraction(a: Span, b: Span) -> float:
    """Share of the shorter clip that the two clips have in common (0.0 to 1.0)."""
    if a.start is None or b.start is None or a.duration <= 0 or b.duration <= 0:
        return 0.0
    shared = min(a.start + a.duration, b.start + b.duration) - max(a.start, b.start)
    return max(shared, 0.0) / min(a.duration, b.duration)


def find_duplicates(spans: list[Span]) -> dict[str, str]:
    """Return ``{loser_id: winner_id}`` for clips that repeat another clip.

    Two clips from the same VOD that overlap by more than half are the same
    moment; the one with more views is kept (ties: the older clip). A clip that
    is already downloaded always wins, so nothing in the library is thrown away
    for a late arrival. Clips with no known VOD position can't be compared.
    """
    by_vod: dict[str, list[Span]] = defaultdict(list)
    for span in spans:
        if span.vod_id and span.start is not None and span.duration > 0:
            by_vod[span.vod_id].append(span)

    losers: dict[str, str] = {}
    for group in by_vod.values():
        group.sort(key=lambda s: (not s.settled, -s.view_count, s.created_at, s.id))
        kept: list[Span] = []
        for span in group:
            winner = next((k for k in kept if overlap_fraction(span, k) > OVERLAP_THRESHOLD), None)
            if winner is None or span.settled:
                kept.append(span)
            else:
                losers[span.id] = winner.id
    return losers


# --- posting candidates and score ------------------------------------------------

MIN_SECONDS = 15  # Shorts are 15-59 s (hard rule 4); longer clips are trimmed, keeping the end
MUSIC_LIMIT = 0.5
LOW_SPEECH_WPS = 0.5
NOVELTY_WINDOW = 20


@dataclass(frozen=True)
class Verdict:
    """Whether a clip may become a Short, and why not if it may not."""

    ok: bool
    reason: str | None = None  # too_short | music
    low_priority: bool = False  # hardly any speech
    music_unchecked: bool = False  # no music score yet: a person should listen first


def posting_verdict(
    duration: float, music_score: float | None, speech_wps: float | None
) -> Verdict:
    """Apply the posting-candidate rules to one clip."""
    if duration < MIN_SECONDS:
        return Verdict(False, "too_short")
    if music_score is not None and music_score > MUSIC_LIMIT:
        return Verdict(False, "music")
    quiet = speech_wps is not None and speech_wps < LOW_SPEECH_WPS
    return Verdict(True, None, low_priority=quiet, music_unchecked=music_score is None)


def view_velocity(view_count: int, created_at: datetime, now: datetime) -> float:
    """Views per hour since the clip was made (clips under an hour old count as one hour)."""
    hours = max((now - created_at).total_seconds() / 3600, 1.0)
    return max(view_count, 0) / hours


def token_similarity(a: str, b: str) -> float:
    """Jaccard similarity of the words in two texts (0 = nothing shared, 1 = same words)."""
    first, second = set(re.findall(r"\w+", a.lower())), set(re.findall(r"\w+", b.lower()))
    return len(first & second) / len(first | second) if first and second else 0.0


def novelty(text: str, recent_posts: list[str]) -> float:
    """1 minus the closest match among the last 20 posted transcripts."""
    recent = recent_posts[-NOVELTY_WINDOW:]
    return 1.0 - max((token_similarity(text, other) for other in recent), default=0.0)


def clip_score(velocity: float, speech_wps: float | None, novelty_value: float) -> float:
    """``view_velocity x (0.5 + min(speech_wps, 3) / 3) x novelty`` (SPEC §7)."""
    return velocity * (0.5 + min(speech_wps or 0.0, 3.0) / 3.0) * novelty_value
