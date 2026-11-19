"""The upload queue. Routes clips to accounts, spaces posts out, runs Theo's checks, calls the uploader with
retries, and puts anything that can't be posted in the "Post manually" list.

Hard rule 1 holds here, in the code path itself: a clip is only queued when its streamer has ``post_enabled``
with a recorded permission. Nothing is queued by itself unless the user switched auto-approve on.

Job states: queued -> posted | drafted | dry_run_ok | blocked (Theo said no) | manual (a person must post it)."""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from clipvault import config as cfg
from clipvault.library import storage
from clipvault.posting import captiongen, checks, oauth, store
from clipvault.posting import http as requests
from clipvault.posting.oauth import OAuthError
from clipvault.posting.secrets import Secrets
from clipvault.posting.uploaders.base import ClipFile, UploadError
from clipvault.posting.uploaders.instagram import Instagram
from clipvault.posting.uploaders.tiktok import TikTok
from clipvault.posting.uploaders.youtube import YouTube

log = logging.getLogger(__name__)
UPLOADERS = {"tiktok": TikTok(), "youtube": YouTube(), "instagram": Instagram()}
PLATFORM_NAMES = {"tiktok": "TikTok", "youtube": "YouTube Shorts", "instagram": "Instagram Reels"}
MAX_ATTEMPTS = 4
PERMITTED = ("campaign", "written")

CLIP_SQL = (
    "SELECT c.id, c.title, c.game_name, c.headline, c.transcript_path, s.login, s.display_name, "
    "s.post_enabled, s.permission_type, s.permission_ref, "
    "(SELECT r.path FROM renders r WHERE r.clip_id = c.id AND r.path IS NOT NULL "
    " ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS render_path, "
    "(SELECT r.qa_passed FROM renders r WHERE r.clip_id = c.id AND r.path IS NOT NULL "
    " ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS qa_passed, "
    "(SELECT r.hook_emoji FROM renders r WHERE r.clip_id = c.id AND r.path IS NOT NULL "
    " ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS hook_emoji "
    "FROM clips c JOIN streamers s ON s.id = c.streamer_id "
)


# --- helpers -----------------------------------------------------------------------------------------------------
def clip_info(c: sqlite3.Connection, clip_id: str) -> dict | None:
    row = c.execute(CLIP_SQL + "WHERE c.id = ?", (clip_id,)).fetchone()
    return dict(row) if row else None


def may_post(clip: dict) -> str:
    """Hard rule 1: empty when this clip's streamer is cleared for posting, else the reason it isn't."""
    if (clip["post_enabled"] and clip["permission_type"] in PERMITTED
            and (clip["permission_ref"] or "").strip()):
        return ""
    return (f"{clip['display_name'] or clip['login']} hasn't given permission for posting. "
            "Record their permission on the Streamers page first.")


def library_for(c: sqlite3.Connection, config: cfg.Config | None = None) -> storage.Library:
    return storage.open_library(c, config or cfg.load_config(cfg.home_dir() / cfg.CONFIG_FILENAME))


def clip_path(library: storage.Library, clip: dict) -> Path | None:
    path = storage.resolve(library, clip.get("render_path"))
    return path if path is not None and storage.is_inside_library(library, path) else None


def clip_file(clip_id: str, path: Path) -> tuple[ClipFile, str | None]:
    from clipvault.render import ffmpeg

    try:
        info = ffmpeg.probe(path)
    except ffmpeg.RenderError:
        return ClipFile(clip_id, path, 0.0, 0, 0), None
    return ClipFile(clip_id, path, info.duration, info.width, info.height), info.codec.lower()


def _transcript(library: storage.Library, clip: dict) -> str:
    path = storage.resolve(library, clip.get("transcript_path"))
    try:
        return captiongen.srt_text(path.read_text(encoding="utf-8")) if path else ""
    except OSError:
        return ""


def _tomorrow(now: float) -> float:
    d = datetime.fromtimestamp(now, timezone.utc) + timedelta(days=1)
    return d.replace(hour=8, minute=random.randint(0, 30), second=0, microsecond=0).timestamp()


# --- captions for every clip -------------------------------------------------------------------------------------
def ensure_caption(c: sqlite3.Connection, library: storage.Library, clip: dict, force: bool = False) -> tuple[dict, dict]:
    have = None if force else captiongen.load(c, clip["id"])
    if have and have[0].get("source") != "edited" and (have[1].get("hook_emoji") or "") != (clip.get("hook_emoji") or ""):
        have = None          # the video was made again with another emoji: the post text follows it
    if have:
        return have
    facts = captiongen.collect_facts(clip, _transcript(library, clip))
    pkg = captiongen.generate(facts)
    captiongen.save(c, clip["id"], pkg, facts)
    return pkg, facts


def save_edit(c: sqlite3.Connection, clip_id: str, per_platform: dict) -> dict:
    pkg, facts = captiongen.load(c, clip_id) or ({}, {})
    if not pkg:
        raise KeyError(clip_id)
    pkg["per_platform"].update({k: v for k, v in per_platform.items() if k in pkg["per_platform"]})
    pkg["source"] = "edited"
    captiongen.save(c, clip_id, pkg, facts)
    return pkg


# --- routing and scheduling ------------------------------------------------------------------------------------
def accounts_for(c: sqlite3.Connection, platform: str, streamer: str, clip_types: list[str]) -> list[sqlite3.Row]:
    accs = c.execute("SELECT * FROM social_accounts WHERE platform=? AND status!='disconnected' ORDER BY id", (platform,)).fetchall()
    ids = {a["id"] for a in accs}
    rules = c.execute("SELECT * FROM routing_rules WHERE account_id IN (%s)" % ",".join("?" * len(ids)), tuple(ids)).fetchall() if ids else []
    hit = [r["account_id"] for r in rules
           if r["streamer"] in ("*", streamer) and (r["clip_type"] == "*" or r["clip_type"] in clip_types)]
    # a more specific rule beats '*'; with rules present for this platform, only matching accounts are used
    if rules:
        specific = [r["account_id"] for r in rules if r["streamer"] != "*" or r["clip_type"] != "*"]
        wanted = [a for a in accs if a["id"] in hit]
        if wanted and any(a["id"] in specific for a in wanted):
            wanted = [a for a in wanted if a["id"] in specific] or wanted
        return wanted
    return accs[:1]


def next_slot(c: sqlite3.Connection, account_id: int, clip_id: str, now: float, st: dict) -> float:
    gap = float(st["min_gap_minutes"]) * 60
    jitter = random.uniform(0, float(st["jitter_minutes"]) * 60)
    last = c.execute("SELECT MAX(not_before) m FROM upload_jobs WHERE account_id=? AND dry_run=0 AND state IN "
                     "('queued','posted','drafted')", (account_id,)).fetchone()["m"] or 0
    t = max(now, last + gap + jitter if last else now)
    cross = c.execute("SELECT MAX(not_before) m FROM upload_jobs WHERE clip_id=? AND account_id!=?", (clip_id, account_id)).fetchone()["m"]
    if cross:
        t = max(t, cross + float(st["cross_account_gap_minutes"]) * 60 + random.uniform(0, 120))
    day = datetime.fromtimestamp(t, timezone.utc).date()
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    done = c.execute("SELECT COUNT(*) n FROM upload_jobs WHERE account_id=? AND dry_run=0 AND state IN ('queued','posted','drafted')"
                     " AND not_before>=? AND not_before<?", (account_id, start, start + 86400)).fetchone()["n"]
    if done >= int(st["daily_max"]):
        t = _tomorrow(t)
    return t


def respace(c: sqlite3.Connection, now: float | None = None) -> int:
    """Re-time the jobs still waiting (real ones, not practice runs) with the current spacing settings, so a changed
    "minutes between posts" applies to what is already queued. Returns how many were moved."""
    now = now or time.time()
    st = store.all_settings(c)
    gap = float(st["min_gap_minutes"]) * 60
    jitter = float(st["jitter_minutes"]) * 60
    moved = 0
    for acc in c.execute("SELECT DISTINCT account_id FROM upload_jobs WHERE state='queued' AND dry_run=0 AND account_id>0").fetchall():
        aid = acc["account_id"]
        t = c.execute("SELECT MAX(updated_at) m FROM upload_jobs WHERE account_id=? AND dry_run=0 AND state IN ('posted','drafted')",
                      (aid,)).fetchone()["m"]                      # when this account last posted, if ever
        for job in c.execute("SELECT id FROM upload_jobs WHERE account_id=? AND state='queued' AND dry_run=0 ORDER BY not_before, id",
                             (aid,)).fetchall():
            t = now if t is None else max(now, t + gap + random.uniform(0, jitter))
            c.execute("UPDATE upload_jobs SET not_before=? WHERE id=?", (t, job["id"]))
            moved += 1
    return moved


class NotAllowed(Exception):
    """The clip can't be queued (no permission, no phone version)."""


def enqueue(c: sqlite3.Connection, library: storage.Library, clip_id: str, now: float | None = None) -> list[int]:
    """Create one job per target platform. Platforms with no connected account get a manual job straight away."""
    now = now or time.time()
    clip = clip_info(c, clip_id)
    if clip is None:
        raise NotAllowed("That clip doesn't exist.")
    reason = may_post(clip)
    if reason:
        raise NotAllowed(reason)
    if clip_path(library, clip) is None:
        raise NotAllowed("This clip has no phone version yet.")
    st = store.all_settings(c)
    pkg, facts = ensure_caption(c, library, clip)
    ids = []
    for platform in UPLOADERS:
        if st[f"enabled_{platform}"] != "1":
            continue
        accs = accounts_for(c, platform, facts.get("login") or "", facts.get("moment_types") or [])
        if not accs:
            ids.append(_insert(c, clip_id, 0, platform, "manual", now, f"{PLATFORM_NAMES[platform]} isn't connected.", pkg, st))
            continue
        for a in accs:
            if c.execute("SELECT 1 FROM upload_jobs WHERE clip_id=? AND account_id=?", (clip_id, a["id"])).fetchone():
                continue
            ids.append(_insert(c, clip_id, a["id"], platform, "queued", next_slot(c, a["id"], clip_id, now, st), "", pkg, st))
    return ids


def _insert(c, clip_id, account_id, platform, state, not_before, reason, pkg, st) -> int:
    now = time.time()
    cur = c.execute("INSERT OR IGNORE INTO upload_jobs (clip_id, account_id, platform, state, not_before, reason, caption, dry_run,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (clip_id, account_id, platform, state, not_before, reason,
                     json.dumps(pkg["per_platform"]), int(st["dry_run"] == "1"), now, now))
    return cur.lastrowid


def ready_clips(c: sqlite3.Connection, library: storage.Library, limit: int = 60) -> list[dict]:
    """Clips with a phone version that aren't queued yet, newest first, with whether they may be posted."""
    rows = c.execute(CLIP_SQL + "WHERE EXISTS (SELECT 1 FROM renders r WHERE r.clip_id = c.id AND r.path IS NOT NULL) "
                     "AND NOT EXISTS (SELECT 1 FROM upload_jobs j WHERE j.clip_id = c.id) "
                     "ORDER BY COALESCE(c.created_at, c.updated_at) DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["blocked"] = may_post(d)
        d["qa_failed"] = d["qa_passed"] == 0
        out.append(d)
    return out


def sync_auto(c: sqlite3.Connection, library: storage.Library, now: float | None = None) -> int:
    """With auto-approve on, queue every clip that passed QA and whose streamer is cleared. Only clips made after
    auto-approve was switched on; the clips already in the library are left alone."""
    st = store.all_settings(c)
    if st["auto_approve"] != "1":
        return 0
    since = c.execute("SELECT value FROM posting_settings WHERE key='auto_since'").fetchone()
    if since is None:
        since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        c.execute("INSERT INTO posting_settings VALUES ('auto_since', ?)", (since,))
    else:
        since = since["value"]
    n = 0
    for r in c.execute("SELECT c.id FROM clips c JOIN renders r ON r.clip_id = c.id AND r.path IS NOT NULL AND r.qa_passed = 1 "
                       "WHERE r.created_at >= ? AND NOT EXISTS (SELECT 1 FROM upload_jobs j WHERE j.clip_id = c.id) "
                       "GROUP BY c.id", (since,)).fetchall():
        try:
            n += len(enqueue(c, library, r["id"], now))
        except NotAllowed:
            continue
    return n


# --- running jobs ------------------------------------------------------------------------------------------------
def _finish(c, job, state, reason="", url="", detail="") -> None:
    c.execute("UPDATE upload_jobs SET state=?, reason=?, result_url=?, updated_at=? WHERE id=?",
              (state, reason or detail, url, time.time(), job["id"]))
    store.log_attempt(c, job["platform"], job["account_id"], job["clip_id"], state, url, reason or detail)


def run_job(c: sqlite3.Connection, library: storage.Library, secrets: Secrets, job: sqlite3.Row, *,
            now: float | None = None, http=requests, sleep=time.sleep, uploaders=None) -> str:
    now = now or time.time()
    uploaders = uploaders or UPLOADERS
    st = store.all_settings(c)
    dry = bool(job["dry_run"]) or st["dry_run"] == "1"
    platform = job["platform"]
    clip = clip_info(c, job["clip_id"])
    if clip is None:
        _finish(c, job, "manual", "The clip was removed.")
        return "manual"
    reason = may_post(clip)                      # re-checked at upload time: permission may have been withdrawn
    if reason:
        _finish(c, job, "blocked", reason)
        return "blocked"
    path = clip_path(library, clip)
    if path is None:
        _finish(c, job, "manual", "The clip's phone version wasn't found.")
        return "manual"
    pkg, facts = ensure_caption(c, library, clip)
    pkg = dict(pkg, per_platform=json.loads(job["caption"] or "{}") or pkg["per_platform"])
    file, codec = clip_file(job["clip_id"], path)
    problems = checks.check_all(file, pkg, facts, platform, codec=codec)
    if problems:
        _finish(c, job, "blocked", "Theo blocked this upload: " + " ".join(problems))
        return "blocked"
    acc = c.execute("SELECT * FROM social_accounts WHERE id=?", (job["account_id"],)).fetchone()
    if acc is None:
        _finish(c, job, "manual", f"{PLATFORM_NAMES[platform]} account was removed.")
        return "manual"
    try:
        token = oauth.ensure_fresh(c, secrets, acc["id"], http=http)
        res = uploaders[platform].upload(file, pkg, dict(acc), token, st, dry_run=dry, http=http, sleep=sleep)
    except OAuthError as e:
        _finish(c, job, "manual", f"{acc['label']} needs reconnecting: {e}")
        return "manual"
    except UploadError as e:
        if e.code == "quota":
            c.execute("UPDATE upload_jobs SET not_before=?, reason=?, updated_at=? WHERE id=?",
                      (_tomorrow(now), str(e), now, job["id"]))
            store.log_attempt(c, platform, acc["id"], job["clip_id"], "quota", detail=str(e))
            return "queued"
        if e.code == "temporary" and job["attempts"] + 1 < MAX_ATTEMPTS:
            wait = min(3600, 120 * 2 ** job["attempts"]) + random.uniform(0, 60)
            c.execute("UPDATE upload_jobs SET attempts=attempts+1, not_before=?, reason=?, updated_at=? WHERE id=?",
                      (now + wait, str(e), now, job["id"]))
            store.log_attempt(c, platform, acc["id"], job["clip_id"], "retry", detail=str(e))
            return "queued"
        _finish(c, job, "manual", str(e))
        return "manual"
    state = "dry_run_ok" if res.dry_run else ("drafted" if platform == "tiktok" and st["tiktok_mode"] == "drafts" else "posted")
    _finish(c, job, state, detail=res.detail, url=res.url)
    return state


def tick(c: sqlite3.Connection, library: storage.Library, secrets: Secrets, *, now: float | None = None,
         http=requests, sleep=time.sleep, uploaders=None, limit: int = 3) -> dict:
    """One pass: auto-queue (if on), run what's due (a few at a time), report counts."""
    now = now or time.time()
    queued = sync_auto(c, library, now)
    due = c.execute("SELECT * FROM upload_jobs WHERE state='queued' AND not_before<=? ORDER BY not_before LIMIT ?",
                    (now, limit)).fetchall()
    outcome: dict[str, int] = {}
    for job in due:
        r = run_job(c, library, secrets, job, now=now, http=http, sleep=sleep, uploaders=uploaders)
        outcome[r] = outcome.get(r, 0) + 1
    return {"queued": queued, "ran": outcome}


def mark_posted(c: sqlite3.Connection, job_id: int) -> None:
    job = c.execute("SELECT * FROM upload_jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise KeyError(job_id)
    _finish(c, job, "posted", detail="Marked as posted by hand.")


def retry_job(c: sqlite3.Connection, job_id: int, now: float | None = None) -> None:
    c.execute("UPDATE upload_jobs SET state='queued', attempts=0, not_before=?, reason='', updated_at=? WHERE id=?",
              (now or time.time(), time.time(), job_id))


def overview(c: sqlite3.Connection) -> dict:
    jobs = [dict(r) for r in c.execute("SELECT * FROM upload_jobs ORDER BY not_before DESC LIMIT 200")]
    for j in jobs:
        j["caption"] = json.loads(j["caption"] or "{}")
    return {"queue": [j for j in jobs if j["state"] == "queued"],
            "manual": [j for j in jobs if j["state"] in ("manual", "blocked")],
            "done": [j for j in jobs if j["state"] in ("posted", "drafted", "dry_run_ok")][:30],
            "log": [dict(r) for r in c.execute("SELECT * FROM upload_log ORDER BY id DESC LIMIT 40")]}
