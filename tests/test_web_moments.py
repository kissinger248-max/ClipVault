"""ClipVault Web: finding the best moments in an upload (clipvault_web/moments.py)."""

from __future__ import annotations

import random

from clipvault.process.transcribe import Transcript, Word
from clipvault_web import moments as m


def talk_with_laughs(minutes: float, laughs: list[float], seed: int = 1) -> list[float]:
    """Loudness of steady talk (-30 dBFS) with a 4-second burst (+12 dB) at each time."""
    rng = random.Random(seed)
    levels = [-30 + rng.gauss(0, 2) for _ in range(int(minutes * 60 / m.WINDOW_S))]
    for at in laughs:
        for i in range(int(at / m.WINDOW_S), int((at + 4) / m.WINDOW_S)):
            levels[i] += 12
    return levels


def speech(start: float, end: float, every: int = 8) -> list[Word]:
    """Words every 0.33 s, a sentence ending every ``every`` words (with a short pause)."""
    words, at, k = [], start, 0
    while at < end:
        k += 1
        words.append(Word("word." if k % every == 0 else "word", round(at, 2), round(at + 0.25, 2)))
        at += 0.9 if k % every == 0 else 0.33
    return words


def test_loudness_is_measured_per_half_second() -> None:
    import numpy as np

    rate = m.SAMPLE_RATE
    quiet = np.full(rate, 0.01, dtype=np.float32)  # 1 s at -40 dBFS
    loud = np.full(rate // 2, 0.5, dtype=np.float32)  # 0.5 s at -6 dBFS
    levels = m.levels_db(np.concatenate([quiet, loud]))
    assert [round(v) for v in levels] == [-40, -40, -6]
    assert m.levels_db(np.zeros(10, dtype=np.float32)) == []


def test_sudden_loud_moments_are_found_best_first_and_counted() -> None:
    laughs = [100, 250, 400, 610, 800, 1000]
    levels = talk_with_laughs(20, laughs)
    peaks, strong = m.find_peaks(levels, 6)
    assert strong == 6
    assert sorted(round(p.at / 10) * 10 for p in peaks) == [100, 250, 400, 610, 800, 1000]
    assert all(isinstance(p.at, float) and p.surge > 8 for p in peaks)
    assert all(abs(a.at - b.at) >= m.PEAK_GAP_S for a in peaks for b in peaks if a is not b)


def test_steady_music_and_the_videos_edges_are_not_moments() -> None:
    levels = talk_with_laughs(10, [300])
    levels[-int(120 / m.WINDOW_S) :] = [-12.0] * int(120 / m.WINDOW_S)  # a loud song at the end
    peaks, strong = m.find_peaks(levels, 3)
    assert peaks[0].at > 290 and peaks[0].at < 310  # the laugh, not the song or the start
    assert all(p.at > 5 for p in peaks)
    silent = [-80.0] * 600
    assert m.find_peaks(silent, 3) == ([], 0)
    assert m.find_peaks([], 3) == ([], 0)


def test_a_clip_is_cut_on_sentences_around_its_peak() -> None:
    words = speech(60, 140)
    start, end = m.cut_points(101.5, words, 1200)
    assert m.MIN_S <= end - start <= m.MAX_S
    assert start <= 101.5 - m.START_BEFORE_PEAK[0] and end >= 101.5 + m.END_AFTER_PEAK[0] - 0.5
    first = min((w for w in words if w.start >= start), key=lambda w: w.start)
    before = max((w for w in words if w.end <= first.start), key=lambda w: w.end)
    assert before.text.endswith(".")  # it starts at the beginning of a sentence
    last = max((w for w in words if w.end <= end), key=lambda w: w.end)
    assert last.text.endswith(".")  # and ends at the end of one


def test_cuts_always_stay_inside_the_video_and_15_to_59_seconds() -> None:
    for peak, duration in ((5, 40), (1195, 1200), (30, 60), (500, 1000)):
        start, end = m.cut_points(peak, [], duration)
        assert 0 <= start < end <= duration
        assert m.MIN_S <= end - start <= m.MAX_S
    assert m.cut_points(5, [], 12) == (0.0, 12)  # too short for a Short: the whole video


def test_reactions_and_speech_raise_the_score() -> None:
    peak = m.Peak(100.0, 8.0, -20.0)
    plain, _ = m.score_moment(peak, speech(80, 110), 30, "en")
    shouting = [*speech(80, 100), Word("no", 101, 101.2), Word("way!", 101.2, 101.5),
                Word("let's", 102, 102.2), Word("go!", 102.2, 102.5)]  # fmt: skip
    excited, reasons = m.score_moment(peak, shouting, 30, "en")
    silent, why = m.score_moment(peak, [], 30, "en")
    assert excited > plain > silent
    assert "shock x1" in reasons and "hype x1" in reasons and "hardly any speech" in why


def test_the_best_moments_that_dont_overlap_are_kept() -> None:
    peak = m.Peak(0, 5, -20)

    def moment(start: float, score: float) -> m.Moment:
        return m.Moment(start, start + 30, score, peak)

    chosen = m.pick([moment(0, 50), moment(10, 90), moment(45, 70), moment(100, 60)], 3)
    assert [c.score for c in chosen] == [90, 70, 60]  # 0-30 overlaps the best (10-40)


def test_split_words_are_joined() -> None:
    words = (Word("I", 0, 0.1), Word("love", 0.1, 0.3), Word("y", 0.3, 0.4), Word("'all", 0.4, 0.6))
    assert [w.text for w in m.join_split_words(words)] == ["I", "love", "y'all"]
    assert [w.text for w in m.join_split_words((Word("Uh", 0, 0.2), Word("-huh.", 0.2, 0.4)))] == [
        "Uh-huh."
    ]


def test_finding_moments_listens_only_to_the_candidates(monkeypatch) -> None:
    levels = talk_with_laughs(20, [100, 250, 400, 610, 800, 1000])
    heard = []

    def listen(video, start, end, providers):
        heard.append((start, end))
        words = tuple(speech(start, end))
        if start < 100 < end:  # the first laugh is the funniest
            words += (
                Word("hahaha", 101, 101.5),
                Word("I'm", 102, 102.2),
                Word("dying", 102.2, 102.6),
            )
        return Transcript(" ".join(w.text for w in words), words, "en", "fake")

    monkeypatch.setattr(m, "listen", listen)
    steps = []
    chosen, more = m.find_moments(None, 1200.0, 3, [], lambda s, d, t: steps.append(s),
                                  levels=levels)  # fmt: skip
    assert len(heard) == 6  # every candidate, never the whole video
    assert all(end - start <= m.LISTEN_BEFORE_S + m.LISTEN_AFTER_S for start, end in heard)
    assert len(chosen) == 3 and more == 3
    assert chosen[0].start < 100 < chosen[0].end and "funny" in " ".join(chosen[0].reasons)
    for moment in chosen:
        assert m.MIN_S <= moment.duration <= m.MAX_S
        assert moment.words and moment.words[0].start >= 0  # timed from the clip's start
        assert moment.words[-1].end <= moment.duration + 0.1
    assert steps[0] == "Listening for the big moments" and "Reading what is said" in steps


def test_a_video_without_sound_gets_evenly_spread_moments(monkeypatch) -> None:
    monkeypatch.setattr(m, "listen", lambda *a: (_ for _ in ()).throw(AssertionError("no sound")))
    chosen, more = m.find_moments(None, 300.0, 5, [], levels=[])
    assert chosen and more == 0
    assert all(c.text == "" for c in chosen)
