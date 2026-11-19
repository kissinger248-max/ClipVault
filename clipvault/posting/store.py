"""Posting settings and helpers. The tables are created by migration 0010_posting.sql."""

from __future__ import annotations

import json
import sqlite3
import time

DEFAULTS = {
    "dry_run": "1",                 # starts ON: the whole flow runs, nothing is posted
    "auto_approve": "0",
    "tiktok_mode": "drafts",        # drafts | direct
    "youtube_privacy": "private",   # private | unlisted | public
    "enabled_tiktok": "1", "enabled_youtube": "1", "enabled_instagram": "1",
    "min_gap_minutes": "120", "jitter_minutes": "20", "daily_max": "4", "cross_account_gap_minutes": "10",
    "youtube_daily_quota": "10000",  # no longer used: uploads have their own bucket of 100 a day
}


def get_setting(c: sqlite3.Connection, key: str) -> str:
    r = c.execute("SELECT value FROM posting_settings WHERE key=?", (key,)).fetchone()
    if r is None and key == "min_gap_minutes":        # versions before 0.5.6 stored this in hours
        old = c.execute("SELECT value FROM posting_settings WHERE key='min_gap_hours'").fetchone()
        if old:
            try:
                return f"{float(old['value']) * 60:g}"
            except ValueError:
                pass
    return r["value"] if r else DEFAULTS.get(key, "")


def set_setting(c: sqlite3.Connection, key: str, value: str) -> None:
    if key not in DEFAULTS:
        raise ValueError(f"unknown setting {key}")
    c.execute("INSERT OR REPLACE INTO posting_settings VALUES (?,?)", (key, str(value)))
    c.commit()


def all_settings(c: sqlite3.Connection) -> dict[str, str]:
    return {k: get_setting(c, k) for k in DEFAULTS}


def log_attempt(c: sqlite3.Connection, platform: str, account_id: int | None, clip_id: str, result: str,
                url: str = "", detail: str = "") -> None:
    c.execute("INSERT INTO upload_log (at, platform, account_id, clip_id, result, url, detail) VALUES (?,?,?,?,?,?,?)",
              (time.time(), platform, account_id, clip_id, result, url, detail[:500]))
    c.commit()


def add_account(c: sqlite3.Connection, platform: str, label: str, handle: str, external_id: str,
                account_type: str | None, scopes: list[str], expires_at: float | None) -> int:
    row = c.execute("SELECT id FROM social_accounts WHERE platform=? AND external_id=?", (platform, external_id)).fetchone()
    if row:
        c.execute("UPDATE social_accounts SET status='connected', error=NULL, handle=?, account_type=?, scopes=?, "
                  "token_expires_at=? WHERE id=?", (handle, account_type, json.dumps(scopes), expires_at, row["id"]))
        c.commit()
        return row["id"]
    cur = c.execute("INSERT INTO social_accounts (platform,label,handle,external_id,account_type,scopes,token_expires_at,"
                    "created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (platform, label or handle, handle, external_id, account_type, json.dumps(scopes), expires_at, time.time()))
    c.commit()
    return cur.lastrowid
