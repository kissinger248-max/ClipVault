from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from clipvault.process import candidates
from tests.helpers import add_clip, add_streamer

NOW = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)  # 24 h after the helper's created_at


def saved(conn: sqlite3.Connection, clip_id: str, streamer_id: int = 1, text: str = "", **kw):
    columns = {"state": "transcribed", "file_path": f"alpha/{clip_id}.mp4", "speech_wps": 2.0}
    add_clip(conn, clip_id, streamer_id, **{**columns, **kw})
    if text:
        conn.execute("INSERT INTO transcripts (clip_id, text) VALUES (?, ?)", (clip_id, text))


def test_rules_leave_out_short_and_music_clips_and_put_quiet_ones_last(conn) -> None:
    add_streamer(conn, "alpha")
    saved(conn, "short", duration=12.0, view_count=10_000)
    saved(conn, "music", music_score=0.9, view_count=10_000)
    saved(conn, "quiet", speech_wps=0.2, view_count=50_000)
    saved(conn, "talky", view_count=2_400)
    saved(conn, "long", duration=90.0, view_count=4_800)  # trimmed to 59 s later, still a candidate
    ranked = candidates.rank_candidates(conn, now=NOW)
    assert [c.clip_id for c in ranked] == ["long", "talky", "quiet"]
    assert ranked[-1].verdict.low_priority and ranked[0].verdict.music_unchecked
    # 2400 views over 24 h = 100/h; x (0.5 + 2/3) x novelty 1
    talky = conn.execute("SELECT score FROM clips WHERE id = 'talky'").fetchone()[0]
    assert abs(talky - 100 * (0.5 + 2 / 3)) < 1e-6


def test_rendered_and_removed_clips_are_skipped(conn) -> None:
    add_streamer(conn, "alpha")
    saved(conn, "done")
    saved(conn, "gone", removed_at="2026-11-11T00:00:00Z", removed_reason="retention")
    saved(conn, "new")
    conn.execute("INSERT INTO renders (clip_id, layout, path) VALUES ('done', 'letterbox', 'x')")
    assert [c.clip_id for c in candidates.rank_candidates(conn, now=NOW)] == ["new"]
    assert {
        c.clip_id for c in candidates.rank_candidates(conn, unrendered_only=False, now=NOW)
    } == {
        "done",
        "new",
    }


def test_a_repeat_of_something_already_posted_scores_lower(conn) -> None:
    add_streamer(conn, "alpha")
    saved(conn, "posted", text="he jumped off the bridge and survived")
    conn.execute("INSERT INTO renders (id, clip_id, layout, path) VALUES (1, 'posted', 'l', 'x')")
    conn.execute(
        "INSERT INTO posts (render_id, platform, account, status, posted_at) "
        "VALUES (1, 'tiktok', 'main', 'posted', '2026-11-11T10:00:00Z')"
    )
    saved(conn, "repeat", text="he jumped off the bridge and survived")
    saved(conn, "fresh", text="chat cannot believe this ending")
    assert candidates.recent_posted_texts(conn) == ["he jumped off the bridge and survived"]
    ranked = candidates.rank_candidates(conn, now=NOW)
    assert [c.clip_id for c in ranked] == ["fresh", "repeat"]
    assert ranked[1].score == 0.0


def test_picks_take_turns_between_streamers(conn) -> None:
    add_streamer(conn, "alpha")
    add_streamer(conn, "beta")
    for i, views in enumerate((9000, 8000, 7000)):
        saved(conn, f"a{i}", 1, view_count=views)
    saved(conn, "b0", 2, view_count=100)
    ranked = candidates.rank_candidates(conn, now=NOW)
    picked = candidates.spread_across_streamers(ranked, 3)
    assert [c.clip_id for c in picked] == ["a0", "b0", "a1"]
    only_beta = candidates.rank_candidates(conn, login="beta", now=NOW)
    assert [c.clip_id for c in only_beta] == ["b0"]
