"""Finding the best moments in a video that has no chat and no view counts (uploads).

Two passes, so even an hour-long video costs little:

1. **Listen** to the whole video, only its loudness (two numbers a second), and find
   where it suddenly gets louder than the minute and a half around it: laughter,
   shouting, cheering, a punchline landing. Music and steady talk don't count, because
   they are as loud as their surroundings.
2. **Read** only the strongest candidates (about a minute each, transcribed) and score
   them on the surge and on what is said: reactions ("no way", "let's go", laughing),
   how much is said, exclamations. Each moment is then cut on sentence boundaries to
   15-59 s (hard rule 4), with the build-up before the peak and the reaction after it.

Everything that decides is a pure function of numbers and words (tested without
FFmpeg); only :func:`measure_levels` and :func:`listen` touch the video.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from clipvault import procs
from clipvault.process import headlines, transcribe
from clipvault.process.transcribe import Transcriber, Transcript, Word

WINDOW_S = 0.5  # one loudness number per half second
SAMPLE_RATE = 8000
SMOOTH_S = 2.0
BASELINE_S = 90.0  # "louder than around it" means than this stretch around it
BASELINE_STEP_S = 5.0
SILENCE_DB = -50.0  # quieter than this is never a moment
PEAK_GAP_S = 45.0  # two candidates are at least this far apart
MIN_SURGE_DB = 5.0  # a jump this big counts as a moment (for "N more moments found")
LISTEN_BEFORE_S = 40.0  # transcribed around a candidate's peak
LISTEN_AFTER_S = 20.0
START_BEFORE_PEAK = (8.0, 35.0)  # a clip starts this long before its peak (the build-up)
END_AFTER_PEAK = (3.0, 16.0)  # and ends this long after it (the reaction)
TARGET_S = 32.0
MIN_S = 15.0  # Shorts are 15-59 s (hard rule 4)
MAX_S = 59.0
PAUSE_S = 0.45  # a gap between words this long is a natural place to cut
SENTENCE_END = (".", "?", "!")
OVERLAP_S = 2.0  # clips may share at most this much
REACTION_WEIGHT = {"clip": 10, "funny": 9, "shock": 9, "hype": 8, "fear": 7, "rage": 6, "sad": 5}
LOUDNESS_TIMEOUT_S = 900

log = logging.getLogger(__name__)
Progress = Callable[[str, int, int], None]


@dataclass(frozen=True)
class Peak:
    """A point where the video gets suddenly louder than around it."""

    at: float  # seconds into the video
    surge: float  # dB louder than its surroundings
    level: float  # dBFS


@dataclass
class Moment:
    """A stretch of the video worth a clip."""

    start: float
    end: float
    score: float
    peak: Peak
    text: str = ""
    words: tuple[Word, ...] = ()  # timed from ``start``
    language: str | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


# --- pass 1: loudness ------------------------------------------------------------------------


def measure_levels(video: Path) -> list[float]:
    """The video's loudness in dBFS, one number per ``WINDOW_S`` (empty: no audio)."""
    import numpy as np

    command = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(video), "-map", "0:a:0",
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
    ]  # fmt: skip
    result = procs.run(command, capture_output=True, timeout=LOUDNESS_TIMEOUT_S)
    if result.returncode != 0 or not result.stdout:
        return []
    samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return levels_db(samples, SAMPLE_RATE)


def levels_db(samples, rate: int = SAMPLE_RATE) -> list[float]:
    """RMS loudness in dBFS per ``WINDOW_S`` of mono ``samples`` (floats, -1..1)."""
    import numpy as np

    size = int(rate * WINDOW_S)
    count = len(samples) // size
    if count == 0:
        return []
    blocks = np.asarray(samples[: count * size], dtype=np.float64).reshape(count, size)
    rms = np.sqrt(np.mean(blocks * blocks, axis=1))
    return [float(v) for v in 20 * np.log10(np.maximum(rms, 1e-5))]


def find_peaks(levels: list[float], want: int) -> tuple[list[Peak], int]:
    """The ``want`` best peaks (best first, at least ``PEAK_GAP_S`` apart) and how many
    peaks in the whole video are real surges (``MIN_SURGE_DB`` or more)."""
    import numpy as np

    n = len(levels)
    if n == 0 or want <= 0:
        return [], 0
    level = np.asarray(levels, dtype=np.float64)
    width = max(int(SMOOTH_S / WINDOW_S), 1)
    padded = np.pad(level, (width // 2, width - 1 - width // 2), mode="edge")  # not silence
    smooth = np.convolve(padded, np.ones(width) / width, mode="valid")
    baseline = _rolling_median(smooth, int(BASELINE_S / WINDOW_S), int(BASELINE_STEP_S / WINDOW_S))
    surge = smooth - baseline
    loudness = smooth - float(np.median(smooth))
    score = surge + 0.25 * loudness
    gap = int(PEAK_GAP_S / WINDOW_S)
    blocked = np.zeros(n, dtype=bool)
    peaks: list[Peak] = []
    for i in np.argsort(-score, kind="stable"):
        if blocked[i] or smooth[i] < SILENCE_DB:
            continue
        peaks.append(Peak(round((int(i) + 0.5) * WINDOW_S, 2), round(float(surge[i]), 2),
                          round(float(smooth[i]), 1)))  # fmt: skip
        blocked[max(i - gap, 0) : i + gap + 1] = True
    strong = sum(1 for p in peaks if p.surge >= MIN_SURGE_DB)
    return peaks[:want], strong


def _rolling_median(values, window: int, step: int):
    """Median of the ``window`` values centred on each point, computed every ``step`` points
    and joined with straight lines (fast enough for hours of audio)."""
    import numpy as np

    n = len(values)
    half = max(window // 2, 1)
    anchors = list(range(0, n, max(step, 1)))
    if anchors[-1] != n - 1:
        anchors.append(n - 1)
    medians = [float(np.median(values[max(a - half, 0) : a + half + 1])) for a in anchors]
    return np.interp(np.arange(n), anchors, medians)


def even_peaks(duration: float, want: int) -> list[Peak]:
    """Evenly spread candidates, for a video with no sound to listen to."""
    if duration <= 0 or want <= 0:
        return []
    count = max(min(want, int(duration // PEAK_GAP_S)), 1)
    return [Peak(round(duration * (i + 1) / (count + 1), 2), 0.0, 0.0) for i in range(count)]


# --- pass 2: what is said --------------------------------------------------------------------


def listen(video: Path, start: float, end: float, providers: list[Transcriber]) -> Transcript:
    """Transcribe ``start``..``end`` of the video; word times are from the video's start."""
    with tempfile.TemporaryDirectory(prefix="clipvault-web-listen-") as folder:
        audio = Path(folder) / "part.flac"
        command = [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{start:.3f}",
            "-i", str(video), "-t", f"{end - start:.3f}", "-map", "0:a:0",
            "-ar", "16000", "-ac", "1", "-c:a", "flac", str(audio),
        ]  # fmt: skip
        result = procs.run(command, capture_output=True, timeout=transcribe.AUDIO_TIMEOUT_S)
        if result.returncode != 0 or not audio.is_file():
            raise transcribe.NoAudio("no sound in this part of the video")
        heard = transcribe.transcribe_audio(audio, providers)
    words = join_split_words(tuple(
        Word(w.text, round(w.start + start, 3), round(w.end + start, 3)) for w in heard.words
    ))  # fmt: skip
    return Transcript(heard.text, words, heard.language, heard.provider)


def join_split_words(words: tuple[Word, ...]) -> tuple[Word, ...]:
    """Whisper sometimes splits "y'all" into "y" + "'all" and "uh-huh" into "uh" + "-huh"
    (captions read "Y 'ALL", "UH -HUH"); join such pieces back onto the word before."""
    joined: list[Word] = []
    for word in words:
        if joined and word.text[:1] in ("'", "’", "-") and word.start - joined[-1].end < 0.15:
            before = joined[-1]
            joined[-1] = Word(before.text + word.text, before.start, word.end)
        else:
            joined.append(word)
    return tuple(joined)


def score_moment(
    peak: Peak, words: list[Word], span: float, language: str | None
) -> tuple[float, list[str]]:
    """How good a moment is, and why (for tuning)."""
    text = " ".join(w.text for w in words)
    score = 10.0 + 4.0 * max(peak.surge, -2.0)
    reasons = [f"{peak.surge:+.1f} dB"]
    if (language or "en").startswith("en"):
        for kind, count in headlines.reactions(text).items():
            score += REACTION_WEIGHT.get(kind, 5) * min(count, 3)
            reasons.append(f"{kind} x{count}")
    per_second = len(words) / span if span > 0 else 0.0
    if per_second < 0.4:
        score -= 12
        reasons.append("hardly any speech")
    elif 1.2 <= per_second <= 4.0:
        score += 4
    exclaims = min(text.count("!"), 4)
    if exclaims:
        score += 1.5 * exclaims
        reasons.append(f"! x{exclaims}")
    return round(score, 2), reasons


def cut_points(peak: float, words: list[Word], duration: float) -> tuple[float, float]:
    """Where to start and end a clip around ``peak``: on sentence starts and pauses, with
    the build-up before it and the reaction after it, 15-59 s long (inside the video)."""
    duration = max(duration, 0.0)
    if duration <= MIN_S:
        return 0.0, duration
    ordered = sorted(words, key=lambda w: w.start)
    lo_start = max(peak - START_BEFORE_PEAK[1], 0.0)
    hi_start = max(peak - START_BEFORE_PEAK[0], 0.0)
    lo_end = min(peak + END_AFTER_PEAK[0], duration)
    hi_end = min(peak + END_AFTER_PEAK[1], duration)

    starts: list[tuple[float, float]] = []  # (time, penalty)
    ends: list[tuple[float, float]] = []
    for i, word in enumerate(ordered):
        before = ordered[i - 1] if i else None
        if lo_start <= word.start <= hi_start:
            if before is None or before.text.endswith(SENTENCE_END):
                starts.append((word.start, 0.0))
            elif word.start - before.end >= PAUSE_S:
                starts.append((word.start, 2.0))
        after = ordered[i + 1] if i + 1 < len(ordered) else None
        if lo_end <= word.end <= hi_end:
            if word.text.endswith(SENTENCE_END):
                ends.append((word.end, 0.0))
            elif after is None or after.start - word.end >= PAUSE_S:
                ends.append((word.end, 2.0))
    if lo_start == 0.0:
        starts.append((0.0, 1.0))  # the video's own start
    if hi_end == duration:
        ends.append((duration, 1.0))
    for offset in (12.0, 20.0, 28.0):  # anywhere, if nothing better: costs more
        starts.append((max(peak - offset, 0.0), 6.0))
    for offset in (6.0, 10.0):
        ends.append((min(peak + offset, duration), 6.0))

    best: tuple[float, float, float] | None = None
    for start, start_cost in starts:
        for end, end_cost in ends:
            length = end - start
            if not MIN_S <= length <= MAX_S:
                continue
            cost = start_cost + end_cost + abs(length - TARGET_S) / 4
            if best is None or cost < best[0]:
                best = (cost, start, end)
    if best is None:
        start = max(min(peak - 20.0, duration - MIN_S), 0.0)
        end = min(start + max(MIN_S, min(TARGET_S, duration - start)), duration)
    else:
        _, start, end = best
    # A breath either side, so no word is clipped (still inside the video and 15-59 s).
    start = max(start - 0.15, 0.0)
    end = min(end + 0.35, duration, start + MAX_S)
    if end - start < MIN_S:
        end = min(start + MIN_S, duration)
        start = max(end - MIN_S, 0.0)
    return round(start, 3), round(end, 3)


def pick(moments: list[Moment], count: int) -> list[Moment]:
    """The best ``count`` moments that don't overlap, best first."""
    chosen: list[Moment] = []
    for moment in sorted(moments, key=lambda m: -m.score):
        if len(chosen) >= count:
            break
        clash = any(
            min(moment.end, other.end) - max(moment.start, other.start) > OVERLAP_S
            for other in chosen
        )
        if not clash:
            chosen.append(moment)
    return chosen


# --- both passes -------------------------------------------------------------------------------


def find_moments(
    video: Path,
    duration: float,
    count: int,
    providers: list[Transcriber],
    progress: Progress | None = None,
    levels: list[float] | None = None,
) -> tuple[list[Moment], int]:
    """The best ``count`` moments of ``video`` (best first), and how many more were found.

    ``levels`` can be given to skip measuring (tests).
    """
    report = progress or (lambda step, done, total: None)
    report("Listening for the big moments", 0, 0)
    if levels is None:
        levels = measure_levels(video)
    candidates_wanted = max(count * 2, count + 3)
    peaks, strong = find_peaks(levels, candidates_wanted)
    if not peaks:
        peaks = even_peaks(duration, candidates_wanted)
    moments: list[Moment] = []
    for i, peak in enumerate(peaks):
        report("Reading what is said", i, len(peaks))
        start = max(peak.at - LISTEN_BEFORE_S, 0.0)
        end = min(peak.at + LISTEN_AFTER_S, duration)
        try:
            heard = listen(video, start, end, providers) if levels else None
        except transcribe.TranscribeError as exc:
            log.warning("could not transcribe a moment: %s", exc)
            heard = None
        words = list(heard.words) if heard else []
        clip_start, clip_end = cut_points(peak.at, words, duration)
        inside = [w for w in words if w.start >= clip_start - 0.05 and w.end <= clip_end + 0.05]
        score, reasons = score_moment(peak, inside, clip_end - clip_start,
                                      heard.language if heard else None)  # fmt: skip
        moments.append(Moment(
            start=clip_start, end=clip_end, score=score, peak=peak,
            text=" ".join(w.text for w in inside),
            words=tuple(Word(w.text, round(w.start - clip_start, 3), round(w.end - clip_start, 3))
                        for w in inside),
            language=heard.language if heard else None, reasons=reasons,
        ))  # fmt: skip
    report("Reading what is said", len(peaks), len(peaks))
    chosen = pick(moments, count)
    more = max(strong - sum(1 for m in chosen if m.peak.surge >= MIN_SURGE_DB), 0)
    return chosen, more
