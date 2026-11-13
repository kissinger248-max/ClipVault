"""Filtering rules (SPEC §7). So far: removing clips that overlap on the same VOD."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

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
