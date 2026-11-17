"""The live view's data: what ClipVault is doing right now (polled by static/activity.js).

``GET /api/activity`` joins the running app's in-memory activity (:mod:`clipvault.activity`)
with the database:

- **pipeline:** clips on their way to a phone version, each with its stage (found, saved,
  subtitled) and what it is doing or waiting for (being cut, waiting for the stream's
  recording to catch up, waiting for the charger, ...);
- **done:** clips whose phone version appeared since the page last asked, as ready-made
  card HTML (the same macro the pages use), so a loading card can turn into the real one;
- **live:** the channels whose chat is being read, with names and avatars;
- **counts:** the numbers on Home's tiles.

The page sends ``since`` (the last event it saw) and ``after`` (the newest phone version
it knows) so each answer carries only what is new.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Form

from clipvault import activity, alerts, power
from clipvault import config as cfg
from clipvault.timeutil import parse_rfc3339, to_rfc3339, utc_now

PIPELINE_SHOWN = 8
DONE_SHOWN = 8
WAITING_STATES = ("found", "fetched", "filtered", "transcribed", "written")
# How far along a clip is: steps done out of Found, Saved, Subtitles, Phone version.
STEPS_DONE = {"found": 1, "fetched": 2, "filtered": 2, "transcribed": 3, "written": 3}
WORK_STAGE = {"downloading": 1, "cutting": 1, "subtitles": 2, "editing": 3}


def register(
    app: FastAPI,
    *,
    back: Callable[..., Any],
    conn: Callable[[], sqlite3.Connection],
    config: Callable[[], cfg.Config],
    card: Callable[[sqlite3.Row], str],
    clip_rows: Callable[..., list[sqlite3.Row]],
    local_midnight: Callable[[cfg.Config], str],
) -> None:
    """Add the live-view routes to the panel."""

    @app.get("/api/activity")
    def api_activity(since: int = 0, after: str = "") -> dict[str, Any]:
        current = config()
        c = conn()
        try:
            return payload(c, current, since=since, after=after, card=card,
                           clip_rows=clip_rows, today=local_midnight(current))  # fmt: skip
        finally:
            c.close()

    @app.post("/settings/live")
    def save_live(on: str = Form(""), next: str = Form("/settings#twitch")):
        from clipvault.live import LIVE_SETTING

        c = conn()
        try:
            alerts.set_setting(c, LIVE_SETTING, "1" if on else "0")
        finally:
            c.close()
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        return back(target, "Live moments are on." if on else "Live moments are off.")

    @app.post("/settings/heavy-on-battery")
    def heavy_on_battery(next: str = Form("/")):
        try:
            cfg.save_settings({"heavy_jobs_on_battery": True})
        except cfg.ConfigError as exc:
            return back("/settings#schedule", f"Not saved: {exc}")
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        return back(target, "Subtitles and phone versions are now made on battery too.")


def payload(
    c: sqlite3.Connection,
    config: cfg.Config,
    *,
    since: int,
    after: str,
    card: Callable[[sqlite3.Row], str],
    clip_rows: Callable[..., list[sqlite3.Row]],
    today: str,
) -> dict[str, Any]:
    """Everything the live view shows, as plain data."""
    from clipvault import jobs
    from clipvault.live import LIVE_MIN_SURGE, LIVE_SETTING

    snap = activity.snapshot(since)
    paused = alerts.is_paused(c)
    battery = power.on_battery()
    heavy_ok = power.heavy_allowed(config)
    people = {
        row["login"]: {"name": row["display_name"] or row["login"], "avatar": row["avatar_url"]}
        for row in c.execute("SELECT login, display_name, avatar_url FROM streamers")
    }
    for channel in snap["live"]["channels"]:
        channel.update(people.get(channel["login"], {"name": channel["login"], "avatar": None}))
    for item in snap["work"]:
        item["name"] = people.get(item["login"], {}).get("name", item["login"])
    unnamed = [e["clip_id"] for e in snap["events"] if e.get("clip_id") and not e.get("login")]
    if unnamed:  # e.g. "phone version ready" only knows the clip
        logins = dict(c.execute(
            "SELECT c.id, s.login FROM clips c JOIN streamers s ON s.id = c.streamer_id "
            f"WHERE c.id IN ({', '.join('?' * len(unnamed))})", unnamed))  # fmt: skip
        for item in snap["events"]:
            if item.get("clip_id") in logins and not item.get("login"):
                item["login"] = logins[item["clip_id"]]
    for item in snap["events"]:
        if item.get("login"):
            item["name"] = people.get(item["login"], {}).get("name", item["login"])

    pipeline, waiting = _pipeline(c, config, snap, paused=paused, heavy_ok=heavy_ok)
    done, newest = _done(c, after, card, clip_rows)
    snap["live"].update(on=alerts.get_setting(c, LIVE_SETTING) != "0", min_surge=LIVE_MIN_SURGE)
    return {
        **snap,
        "paused": paused,
        "power": {"battery": battery, "heavy_ok": heavy_ok},
        "subtitles": config.transcription.enabled,
        "pipeline": pipeline,
        "waiting": waiting,
        "done": done,
        "after": newest or after,
        "counts": {
            "saved": c.execute(
                "SELECT count(*) FROM clips WHERE fetched_at >= ?", (today,)
            ).fetchone()[0],
            "editing": len(jobs.clips_to_render(c, config)),
            "live": c.execute(
                "SELECT count(*) FROM clips WHERE source = 'live_spike' AND created_at >= ?",
                (today,),
            ).fetchone()[0],
        },
    }


def _pipeline(
    c: sqlite3.Connection, config: cfg.Config, snap: dict, *, paused: bool, heavy_ok: bool
) -> tuple[list[dict], int]:
    """Clips on their way to a phone version, the most interesting first; and how many."""
    marks = ", ".join("?" * len(WAITING_STATES))
    where = (
        f"c.state IN ({marks}) AND c.removed_at IS NULL AND c.render_error IS NULL "
        "AND s.save_enabled = 1 AND (c.reject_reason IS NULL OR c.reject_reason = 'muted') "
        "AND NOT EXISTS (SELECT 1 FROM renders r WHERE r.clip_id = c.id AND r.path IS NOT NULL)"
    )
    total = c.execute(
        f"SELECT count(*) FROM clips c JOIN streamers s ON s.id = c.streamer_id WHERE {where}",
        WAITING_STATES,
    ).fetchone()[0]
    busy = {w["clip_id"]: w for w in snap["work"]}
    rows = c.execute(
        "SELECT c.id, c.state, c.source, c.title, c.created_at, c.view_count, c.duration, "
        "c.retry_at, c.fail_count, c.surge, c.file_path, s.login, s.display_name "
        f"FROM clips c JOIN streamers s ON s.id = c.streamer_id WHERE {where} "
        "ORDER BY c.id IN (SELECT value FROM json_each(?)) DESC, "
        "(c.source = 'live_spike') DESC, c.state IN ('transcribed', 'written') DESC, "
        "c.created_at DESC LIMIT ?",
        (*WAITING_STATES, _json_list(busy), PIPELINE_SHOWN),
    ).fetchall()
    now = utc_now()
    items = []
    for row in rows:
        steps = STEPS_DONE[row["state"]]
        work = busy.get(row["id"])
        wait_until = None
        if work is not None:
            status = work["stage"]
            steps = WORK_STAGE[work["stage"]]
        elif paused:
            status = "paused"
        elif row["state"] == "found":
            due = parse_rfc3339(row["retry_at"]) if row["retry_at"] else None
            if due is not None and due > now:
                wait_until = due.timestamp()
                cut = row["source"] in ("live_spike", "vod_spike")
                status = "waiting_stream" if cut and not row["fail_count"] else "retry"
            else:
                status = "queued"
        elif not heavy_ok:
            status = "power"
        else:
            status = "queued"
        items.append({
            "id": row["id"],
            "login": row["login"],
            "name": row["display_name"] or row["login"],
            "title": row["title"] or "",
            "source": row["source"],
            "steps": steps,
            "subtitles": config.transcription.enabled,
            "status": status,
            "started": work["started"] if work else None,
            "expected_s": work["expected_s"] if work else None,
            "wait_until": wait_until,
            "surge": row["surge"],
            "views": row["view_count"],
            "duration": row["duration"],
            "has_file": row["state"] != "found",
            "created_at": row["created_at"],
        })  # fmt: skip
    return items, total


def _done(
    c: sqlite3.Connection,
    after: str,
    card: Callable[[sqlite3.Row], str],
    clip_rows: Callable[..., list[sqlite3.Row]],
) -> tuple[list[dict], str]:
    """Phone versions made after ``after`` (newest first), as card HTML."""
    if not after:
        return [], to_rfc3339(utc_now())
    rows = clip_rows(
        c,
        "c.removed_at IS NULL AND EXISTS (SELECT 1 FROM renders r WHERE r.clip_id = c.id "
        "AND r.path IS NOT NULL AND r.created_at > ?)",
        [after],
        "render_at DESC",
        DONE_SHOWN,
    )
    done = [{"id": row["id"], "at": row["render_at"], "html": str(card(row))} for row in rows]
    newest = max((item["at"] for item in done), default=after)
    return done, newest


def _json_list(values: Any) -> str:
    import json

    return json.dumps(list(values))
