"""Finding moments nobody clipped from a finished stream's chat replay (SPEC §5.2).

Everything here is pure: chat messages in, moments (start/end seconds in the
VOD) out. Downloading the chat and cutting the video live elsewhere, so the
detector can be tuned and tested on recorded chat logs.

How it works: messages are counted in 10-second windows, and messages with a
hype or laugh emote count three times (1 + 2 extra). A window is a spike when
its score is more than 3 standard deviations above the mean of the previous 10
minutes, and at least twice that mean. Chat reacts 5 to 15 seconds after the
event, so a moment runs from 30 s before the peak to 10 s after it.
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass

WINDOW_S = 10
HISTORY_WINDOWS = 60  # 10 minutes
MIN_HISTORY_WINDOWS = 6  # a minute of chat before anything can count as a spike
SIGMAS = 3.0
MIN_RATIO = 2.0
MIN_SCORE = 10.0  # a quiet chat's 0 -> 4 messages is not a moment
EMOTE_WEIGHT = 2
BEFORE_PEAK_S = 30
AFTER_PEAK_S = 10
MERGE_GAP_S = 20
MAX_MOMENT_S = 60

# Hype and laugh emotes (SPEC §5.2 list plus common variants). Matched as whole words.
DEFAULT_EMOTES = frozenset(
    {
        "KEKW", "KEKL", "LUL", "LULW", "OMEGALUL", "Pog", "PogU", "PogChamp", "POGGERS",
        "LMAO", "LOL", "W", "ICANT", "Clap", "monkaS", "HUH", "xdd",
    }
)  # fmt: skip
_TOKEN = re.compile(r"\S+")


@dataclass(frozen=True)
class ChatMessage:
    offset: float  # seconds into the VOD
    text: str


@dataclass(frozen=True)
class Spike:
    peak: float  # seconds into the VOD (middle of the peak window)
    score: float
    baseline: float  # mean score of the 10 minutes before


@dataclass(frozen=True)
class Moment:
    start: int  # seconds into the VOD
    end: int
    score: float
    peak: float
    surge: float = 0.0  # score divided by the normal level of the 10 minutes before

    @property
    def duration(self) -> int:
        return self.end - self.start


def has_emote(text: str, emotes: Iterable[str] = DEFAULT_EMOTES) -> bool:
    """True when the message contains one of the emotes as a whole word."""
    wanted = emotes if isinstance(emotes, (set, frozenset)) else set(emotes)
    return any(token.strip(".,!?") in wanted for token in _TOKEN.findall(text))


def window_scores(
    messages: Iterable[ChatMessage],
    length_s: float | None = None,
    emotes: Iterable[str] = DEFAULT_EMOTES,
) -> list[float]:
    """Score per 10-second window: messages, plus 2 for each one with an emote."""
    wanted = frozenset(emotes)
    scores: list[float] = []
    if length_s:
        scores = [0.0] * max(math.ceil(length_s / WINDOW_S), 1)
    for message in messages:
        if message.offset < 0:
            continue
        index = int(message.offset // WINDOW_S)
        if index >= len(scores):
            scores.extend([0.0] * (index + 1 - len(scores)))
        scores[index] += 1 + (EMOTE_WEIGHT if has_emote(message.text, wanted) else 0)
    return scores


def find_spikes(scores: list[float]) -> list[Spike]:
    """Windows far above the previous 10 minutes; a run of spiking windows gives one peak."""
    flagged: list[tuple[int, float, float]] = []
    for index, score in enumerate(scores):
        history = scores[max(0, index - HISTORY_WINDOWS) : index]
        if len(history) < MIN_HISTORY_WINDOWS:
            continue
        mean = statistics.fmean(history)
        spread = statistics.pstdev(history)
        if score >= MIN_SCORE and score > mean + SIGMAS * spread and score >= MIN_RATIO * mean:
            flagged.append((index, score, mean))

    spikes: list[Spike] = []
    run: list[tuple[int, float, float]] = []
    for item in flagged:
        if run and item[0] - run[-1][0] > 2:  # one quiet window may sit inside a burst
            spikes.append(_peak(run))
            run = []
        run.append(item)
    if run:
        spikes.append(_peak(run))
    return spikes


def _peak(run: list[tuple[int, float, float]]) -> Spike:
    index, score, mean = max(run, key=lambda item: item[1])
    return Spike(index * WINDOW_S + WINDOW_S / 2, score, mean)


def surge(spike: Spike) -> float:
    """How many times the normal level a spike is (normal counted as at least 1)."""
    return spike.score / max(spike.baseline, 1.0)


def moments_from_spikes(spikes: list[Spike], length_s: float | None = None) -> list[Moment]:
    """Turn peaks into cut ranges, merging peaks less than 20 s apart."""
    moments: list[Moment] = []
    for spike in sorted(spikes, key=lambda s: s.peak):
        start = max(int(spike.peak - BEFORE_PEAK_S), 0)
        end = int(spike.peak + AFTER_PEAK_S)
        if length_s is not None:
            end = min(end, int(length_s))
        if end - start < 5:
            continue
        previous = moments[-1] if moments else None
        if previous and spike.peak - previous.peak < MERGE_GAP_S:
            merged_start, merged_end = previous.start, max(previous.end, end)
            best = spike if spike.score > previous.score else None
            peak = best.peak if best else previous.peak
            if merged_end - merged_start > MAX_MOMENT_S:  # keep the range around the bigger peak
                merged_start = max(int(peak - BEFORE_PEAK_S), 0)
                merged_end = merged_start + BEFORE_PEAK_S + AFTER_PEAK_S
            moments[-1] = Moment(
                merged_start,
                merged_end,
                max(previous.score, spike.score),
                peak,
                max(previous.surge, surge(spike)),
            )
        else:
            moments.append(Moment(start, end, spike.score, spike.peak, surge(spike)))
    return moments


def overlaps(moment: Moment, start: float, duration: float) -> bool:
    """Does the moment share any time with the range ``start`` .. ``start + duration``?"""
    return moment.start < start + duration and start < moment.end


def pick_moments(
    messages: Iterable[ChatMessage],
    *,
    length_s: float | None,
    covered: Iterable[tuple[float, float]] = (),
    keep: int = 3,
    emotes: Iterable[str] = DEFAULT_EMOTES,
) -> list[Moment]:
    """The best ``keep`` moments of a VOD that no viewer clip covers, biggest surge first.

    ``covered`` holds (vod_offset, duration) of the clips already made from it.
    """
    scores = window_scores(messages, length_s, emotes)
    return pick_from_scores(scores, length_s=length_s, covered=covered, keep=keep)


def add_scores(total: list[float], more: list[float]) -> list[float]:
    """Add two window-score lists together (chat read in pieces)."""
    if len(more) > len(total):
        total = total + [0.0] * (len(more) - len(total))
    for index, value in enumerate(more):
        total[index] += value
    return total


def pick_from_scores(
    scores: list[float],
    *,
    length_s: float | None,
    covered: Iterable[tuple[float, float]] = (),
    keep: int = 3,
) -> list[Moment]:
    """:func:`pick_moments` for chat already turned into window scores."""
    spans = list(covered)
    moments = moments_from_spikes(find_spikes(scores), length_s)
    fresh = [m for m in moments if not any(overlaps(m, s, d) for s, d in spans)]
    # Ranked by surge, not raw score: chat is often several times busier early in a
    # stream, so raw counts would pick only the first hour (seen on a real 6.7 h VOD).
    return sorted(fresh, key=lambda m: (-m.surge, -m.score, m.start))[:keep]


def parse_duration(text: str) -> int:
    """Twitch video length such as ``"3h8m33s"`` or ``"45s"`` in seconds."""
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text.strip())
    if not match or not any(match.groups()):
        raise ValueError(f"not a Twitch duration: {text!r}")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def clock(seconds: float) -> str:
    """``5025`` -> ``"1:23:45"`` (for titles and logs)."""
    whole = int(seconds)
    return f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"
