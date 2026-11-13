"""Searching what was said in saved clips (full-text index over transcripts)."""

from __future__ import annotations

import re
import sqlite3

_SELECT = (
    "SELECT c.id, s.login, c.title, c.created_at, c.view_count, c.duration, c.file_path, "
    "snippet(transcripts, 1, '[', ']', ' ... ', 14) AS snippet "
    "FROM transcripts JOIN clips c ON c.id = transcripts.clip_id "
    "JOIN streamers s ON s.id = c.streamer_id "
    "WHERE transcripts MATCH ? AND c.removed_at IS NULL "
    "ORDER BY rank, c.view_count DESC LIMIT ?"
)


def search_transcripts(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[sqlite3.Row]:
    """Find clips where ``query`` was said; best matches first.

    The words are first looked for as an exact phrase; if no clip says exactly
    that, clips containing all the words anywhere are returned instead. The last
    word may be incomplete ("gam" finds "game"). Punctuation and case are ignored.
    """
    tokens = re.findall(r"\w+", query.lower())
    if not tokens:
        return []
    phrase = '"' + " ".join(tokens) + '"'
    rows = conn.execute(_SELECT, (phrase, limit)).fetchall()
    if not rows:
        parts = [f'"{t}"' for t in tokens[:-1]] + [f'"{tokens[-1]}"*']
        rows = conn.execute(_SELECT, (" AND ".join(parts), limit)).fetchall()
    return rows
