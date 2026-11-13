"""Ranking saved clips as posting candidates (SPEC §7), using the rules in ``filter``.

The ranking also decides which clips ``render`` picks on its own. Clips with a
lot of music or under 15 s are left out; clips with hardly any speech go last.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from clipvault.process import filter as rules
from clipvault.timeutil import parse_rfc3339, utc_now


@dataclass(frozen=True)
class Candidate:
    """A clip that may become a Short, with its score."""

    clip_id: str
    login: str
    score: float
    verdict: rules.Verdict


def recent_posted_texts(conn: sqlite3.Connection, limit: int = rules.NOVELTY_WINDOW) -> list[str]:
    """Transcripts of the most recently posted clips, oldest first."""
    rows = conn.execute(
        "SELECT t.text FROM posts p JOIN renders r ON r.id = p.render_id "
        "JOIN transcripts t ON t.clip_id = r.clip_id WHERE p.status = 'posted' "
        "GROUP BY r.clip_id ORDER BY max(p.posted_at) DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [row["text"] for row in reversed(rows)]


def rank_candidates(
    conn: sqlite3.Connection,
    *,
    login: str | None = None,
    unrendered_only: bool = True,
    now: datetime | None = None,
    store: bool = True,
) -> list[Candidate]:
    """Score every saved clip that passes the posting rules, best first.

    Clips with hardly any speech come after all the others whatever their
    score. With ``store``, each clip's score is saved in ``clips.score``.
    """
    now = now or utc_now()
    query = (
        "SELECT c.id, s.login, c.duration, c.music_score, c.speech_wps, c.view_count, "
        "c.created_at, t.text FROM clips c JOIN streamers s ON s.id = c.streamer_id "
        "LEFT JOIN transcripts t ON t.clip_id = c.id "
        "WHERE c.state NOT IN ('found', 'rejected', 'dead') AND c.removed_at IS NULL "
        "AND c.file_path IS NOT NULL AND coalesce(c.reject_reason, '') != 'muted'"
    )
    params: list[str] = []
    if unrendered_only:
        query += " AND NOT EXISTS (SELECT 1 FROM renders r WHERE r.clip_id = c.id)"
    if login:
        query += " AND s.login = ?"
        params.append(login)
    recent = recent_posted_texts(conn)
    ranked: list[Candidate] = []
    scores: list[tuple[float, str]] = []
    for row in conn.execute(query, params).fetchall():
        verdict = rules.posting_verdict(
            row["duration"] or 0.0, row["music_score"], row["speech_wps"]
        )
        if not verdict.ok:
            continue
        created = parse_rfc3339(row["created_at"]) if row["created_at"] else now
        velocity = rules.view_velocity(row["view_count"] or 0, created, now)
        novelty = rules.novelty(row["text"] or "", recent)
        score = rules.clip_score(velocity, row["speech_wps"], novelty)
        ranked.append(Candidate(row["id"], row["login"], score, verdict))
        scores.append((score, row["id"]))
    if store and scores:
        conn.executemany("UPDATE clips SET score = ? WHERE id = ?", scores)
    ranked.sort(key=lambda c: (c.verdict.low_priority, -c.score, c.clip_id))
    return ranked


def spread_across_streamers(candidates: list[Candidate], limit: int) -> list[Candidate]:
    """Take the best of each streamer in turn, so one busy channel doesn't fill the list."""
    queues: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        queues.setdefault(candidate.login, []).append(candidate)
    picked: list[Candidate] = []
    while len(picked) < limit and any(queues.values()):
        for queue in queues.values():
            if queue and len(picked) < limit:
                picked.append(queue.pop(0))
    return picked
