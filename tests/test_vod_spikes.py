from __future__ import annotations

from pathlib import Path

import pytest

from clipvault.discover import vod_spikes as vs
from clipvault.twitch import chat_replay

FIXTURE = Path(__file__).parent / "fixtures" / "chat" / "vod_chat.json"


@pytest.fixture(scope="module")
def chat() -> list[vs.ChatMessage]:
    return chat_replay.load_chat(FIXTURE)


def steady(seconds: int, per_window: int = 4) -> list[vs.ChatMessage]:
    return [
        vs.ChatMessage(w * 10 + i, "hello") for w in range(seconds // 10) for i in range(per_window)
    ]


def test_parse_reads_offsets_and_text_in_order(chat) -> None:
    assert len(chat) == 1119
    assert all(a.offset <= b.offset for a, b in zip(chat, chat[1:], strict=False))
    assert vs.parse_duration("3h8m33s") == 11313 and vs.parse_duration("45s") == 45
    with pytest.raises(ValueError):
        vs.parse_duration("")


def test_parse_falls_back_to_fragments_and_skips_junk() -> None:
    data = {
        "comments": [
            {
                "content_offset_seconds": 5,
                "message": {"fragments": [{"text": "KEK"}, {"text": "W"}]},
            },
            {"content_offset_seconds": "bad", "message": {"body": "x"}},
            "junk",
            {"content_offset_seconds": 1, "message": {"body": "first"}},
        ]
    }
    assert chat_replay.parse_chat(data) == [vs.ChatMessage(1, "first"), vs.ChatMessage(5, "KEKW")]


def test_emote_messages_count_three_times() -> None:
    assert vs.has_emote("that was so funny KEKW") and vs.has_emote("W!")
    assert not vs.has_emote("kekw lowercase is not the emote") and not vs.has_emote("Wow")
    scores = vs.window_scores([vs.ChatMessage(1, "hi"), vs.ChatMessage(2, "LUL")], length_s=30)
    assert scores == [4.0, 0.0, 0.0]


def test_steady_chat_has_no_spikes() -> None:
    assert vs.find_spikes(vs.window_scores(steady(1800))) == []


def test_quiet_chat_needs_a_real_burst() -> None:
    quiet = [vs.ChatMessage(w * 10, "hi") for w in range(0, 120, 3)]  # one message per 30 s
    small = quiet + [vs.ChatMessage(605 + i, "hi") for i in range(4)]
    assert vs.find_spikes(vs.window_scores(small)) == []  # below the minimum score


def test_no_spike_before_a_minute_of_history() -> None:
    burst = [vs.ChatMessage(5 + i * 0.1, "KEKW") for i in range(50)] + steady(600)
    assert vs.find_spikes(vs.window_scores(burst)) == []


def test_fixture_finds_the_three_bursts(chat) -> None:
    spikes = vs.find_spikes(vs.window_scores(chat, 2400))
    peaks = sorted(round(s.peak / 60) for s in spikes)
    assert peaks == [15, 25, 35]
    assert all(s.score >= 2 * s.baseline for s in spikes)


def test_moments_cut_30_before_and_10_after_and_merge_close_peaks() -> None:
    spikes = [vs.Spike(100.0, 50, 5), vs.Spike(112.0, 80, 5), vs.Spike(500.0, 40, 5)]
    first, second = vs.moments_from_spikes(spikes, length_s=505)
    assert (first.start, first.end, first.score, first.peak) == (70, 122, 80, 112.0)
    assert (second.start, second.end) == (470, 505)  # stops at the end of the VOD
    early = vs.moments_from_spikes([vs.Spike(12.0, 50, 5)])[0]
    assert (early.start, early.end) == (0, 22)


def test_pick_skips_clipped_moments_and_keeps_the_best(chat) -> None:
    # A viewer clip covers 34:40 to 35:10.
    picked = vs.pick_moments(chat, length_s=2400, covered=[(2080, 30)], keep=3)
    assert len(picked) == 2 and all(m.duration == 40 for m in picked)
    assert all(not vs.overlaps(m, 2080, 30) for m in picked)
    assert {round(m.peak / 60) for m in picked} == {15, 25}
    assert picked[0].surge >= picked[1].surge > 2
    assert len(vs.pick_moments(chat, length_s=2400, keep=1)) == 1


def test_clock() -> None:
    assert vs.clock(5025) == "1:23:45" and vs.clock(59) == "0:00:59"


def test_a_surge_in_quiet_chat_beats_a_bump_in_busy_chat() -> None:
    busy = [vs.ChatMessage(w * 10 + i * 0.3, "hi") for w in range(120) for i in range(30)]
    busy += [vs.ChatMessage(605 + i * 0.1, "hi") for i in range(70)]  # 30 -> 100 a window
    quiet = [vs.ChatMessage(1200 + w * 10 + i, "hi") for w in range(120) for i in range(3)]
    quiet += [vs.ChatMessage(1805 + i * 0.1, "KEKW") for i in range(25)]  # 3 -> 75 a window
    picked = vs.pick_moments(busy + quiet, length_s=2400, keep=2)
    assert [round(m.peak / 60) for m in picked] == [30, 10]  # the quiet-chat surge first
    assert picked[0].score < picked[1].score and picked[0].surge > picked[1].surge
