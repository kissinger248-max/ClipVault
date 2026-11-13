"""Chat spikes found while a stream is live (SPEC §5.3), with the M4 rules.

Messages arrive one by one with their time. They are counted in the same
10-second windows and scored the same way as in :mod:`vod_spikes`; when a run
of spiking windows ends, its peak is reported once. Everything is in wall-clock
time (UTC seconds); the caller turns a peak into a position in the recording.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field

from clipvault.discover import vod_spikes as rules

QUIET_GAP_WINDOWS = 2  # a run of spikes ends after this many ordinary windows


@dataclass(frozen=True)
class LiveSpike:
    peak: float  # UTC seconds (middle of the peak window)
    score: float
    baseline: float

    @property
    def surge(self) -> float:
        return self.score / max(self.baseline, 1.0)


@dataclass
class RollingDetector:
    """Window scores for one channel, and the spike runs found in them."""

    emotes: frozenset[str] = rules.DEFAULT_EMOTES
    history: deque[float] = field(default_factory=lambda: deque(maxlen=rules.HISTORY_WINDOWS))
    window_start: float | None = None
    window_score: float = 0.0
    run: list[tuple[float, float, float]] = field(default_factory=list)  # (peak, score, mean)
    quiet: int = 0

    def add(self, when: float, text: str) -> list[LiveSpike]:
        """Count one message; returns the spikes whose run just ended (usually none)."""
        found = self.advance(when)
        self.window_score += 1 + (rules.EMOTE_WEIGHT if rules.has_emote(text, self.emotes) else 0)
        return found

    def advance(self, now: float) -> list[LiveSpike]:
        """Close every window that ended before ``now`` (call it on quiet chat too)."""
        if self.window_start is None:
            self.window_start = now - now % rules.WINDOW_S
            return []
        found: list[LiveSpike] = []
        while now >= self.window_start + rules.WINDOW_S:
            spike = self._close_window()
            if spike is not None:
                found.append(spike)
            self.window_start += rules.WINDOW_S
        return found

    def flush(self) -> list[LiveSpike]:
        """The stream ended: report a run that is still open."""
        if not self.run:
            return []
        spike = self._peak()
        self.run = []
        return [spike]

    def _close_window(self) -> LiveSpike | None:
        score, self.window_score = self.window_score, 0.0
        history = list(self.history)
        self.history.append(score)
        middle = (self.window_start or 0.0) + rules.WINDOW_S / 2
        spiking = False
        if len(history) >= rules.MIN_HISTORY_WINDOWS:
            mean = statistics.fmean(history)
            spread = statistics.pstdev(history)
            spiking = (
                score >= rules.MIN_SCORE
                and score > mean + rules.SIGMAS * spread
                and score >= rules.MIN_RATIO * mean
            )
            if spiking:
                self.run.append((middle, score, mean))
                self.quiet = 0
        if not spiking and self.run:
            self.quiet += 1
            if self.quiet > QUIET_GAP_WINDOWS:
                spike = self._peak()
                self.run, self.quiet = [], 0
                return spike
        return None

    def _peak(self) -> LiveSpike:
        peak, score, mean = max(self.run, key=lambda item: item[1])
        return LiveSpike(peak, score, mean)


@dataclass(frozen=True)
class Cut:
    start: int  # seconds into the recording
    end: int


def cut_for(spike: LiveSpike, recording_started: float) -> Cut:
    """The 40-second stretch of the recording around a live spike (peak - 30 s .. + 10 s)."""
    offset = spike.peak - recording_started
    start = max(int(offset - rules.BEFORE_PEAK_S), 0)
    return Cut(start, int(offset + rules.AFTER_PEAK_S))
