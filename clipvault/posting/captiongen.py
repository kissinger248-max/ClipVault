"""Copywriter's caption and hashtag generator. Facts first, model second, rules last.

Facts: the clip's transcript, the real Twitch category (Helix), the stream title, why the Analyst picked it.
A local Ollama model turns those facts into a hook + caption (JSON). Hashtags are built in CODE from the streamer,
the real category (config/hashtags.yaml), the moment type and a blocklist; the model can't add or remove them."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path

from clipvault.posting import http as requests
import yaml


log = logging.getLogger(__name__)
OLLAMA = "http://localhost:11434"
LIMITS = {"tiktok": 2200, "youtube_title": 100, "youtube_description": 5000, "instagram": 2200}
STOP = set("a an and are as at be but by for from has have he her him his i if in is it its me my no not of on or our "
           "she so that the their them then there they this to up us was we were what when who why will with you your "
           "just like really very".split())


def load_rules(path: Path | None = None) -> dict:
    """The hashtag rules: ``hashtags.yaml`` in the app's folder when the user made one, else the built-in file."""
    from importlib import resources

    from clipvault import config as cfg

    if path is None:
        mine = cfg.home_dir() / "hashtags.yaml"
        if mine.exists():
            path = mine
    if path is not None:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    text = resources.files("clipvault.data").joinpath("hashtags.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


# --- facts -------------------------------------------------------------------------------------------------------
def moment_type(text: str, rules: dict) -> list[str]:
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    scored = []
    for kind, kws in (rules.get("moment_keywords") or {}).items():
        n = sum(1 for k in kws if k in words)
        if n:
            scored.append((n, kind))
    return [k for _n, k in sorted(scored, reverse=True)]


def slug(text: str, limit: int = 25) -> str:
    s = re.sub(r"[^a-z0-9]", "", text.lower())
    return s[:limit]


_SRT_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->")


def srt_text(raw: str) -> str:
    """The spoken words of an .srt subtitle file, without numbers and timings."""
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.isdigit() or _SRT_TIME.match(line):
            continue
        lines.append(re.sub(r"<[^>]+>", "", line))
    return " ".join(lines).strip()


def collect_facts(clip: dict, transcript: str = "", rules: dict | None = None) -> dict:
    """What the caption may talk about: ClipVault's own record of the clip (real Twitch category, title,
    streamer) and what was said in it. Nothing is looked up from the model."""
    facts = {
        "clip_id": clip.get("id"), "streamer": clip.get("display_name") or clip.get("login"),
        "login": clip.get("login"), "transcript": (transcript or "")[:2500],
        "clip_title": clip.get("title") or "", "game": clip.get("game_name") or None,
        "stream_title": "", "why_picked": "", "hook": clip.get("headline") or "",
        "hook_emoji": clip.get("hook_emoji") or "",      # the emoji drawn in the video, so the post matches it
    }
    rules = rules or load_rules()
    facts["moment_types"] = moment_type(" ".join([facts["clip_title"], facts["hook"], facts["transcript"]]), rules)
    return facts


# --- hashtags (rules in code) ------------------------------------------------------------------------------------
def build_hashtags(facts: dict, rules: dict | None = None, platform: str = "tiktok") -> list[str]:
    rules = rules or load_rules()
    block = {b.lower() for b in rules.get("blocklist", [])}
    lim = rules.get("limits") or {}
    lo, hi = int(lim.get("min_tags", 4)), int(lim.get("max_tags", 8))
    tags: list[str] = []
    login = slug(facts.get("login") or facts.get("streamer") or "")
    if login:
        tags.append("#" + login)
    tags.append("#twitch")
    game = facts.get("game")
    if game:
        mapped = {k.lower(): v for k, v in (rules.get("games") or {}).items()}.get(game.lower())
        if mapped:
            tags += mapped
        elif slug(game):
            tags.append("#" + slug(game))
    for kind in (facts.get("moment_types") or [])[:2]:
        tags += (rules.get("moments", {}).get(kind) or [])[:1]
    if len(tags) < lo:                       # top up from the types' second tags, then neutral tags
        for kind in (facts.get("moment_types") or [])[:2]:
            tags += (rules.get("moments", {}).get(kind) or [])[1:2]
        tags += ["#clips", "#streamer", "#gaming"]
    if platform == "youtube":
        tags.append("#shorts")
    out: list[str] = []
    for t in tags:
        t = t.lower()
        if t not in out and t not in block and re.fullmatch(r"#[a-z0-9_]{2,30}", t):
            out.append(t)
    keep = out[:hi]
    if platform == "youtube" and "#shorts" in out and "#shorts" not in keep:
        keep = keep[:hi - 1] + ["#shorts"]
    return keep


# --- grounding check -------------------------------------------------------------------------------------------
def _vocab(facts: dict) -> set[str]:
    blob = " ".join(str(facts.get(k) or "") for k in
                    ("transcript", "clip_title", "game", "stream_title", "why_picked", "hook", "streamer", "login"))
    return set(re.findall(r"[a-z0-9']+", blob.lower()))


def unsupported_terms(text: str, facts: dict) -> list[str]:
    """Names, numbers and SHOUTED words in the caption that appear nowhere in the clip's facts."""
    vocab = _vocab(facts)
    text = re.sub(r"https?://\S+|twitch\.tv/\S+|#\w+", " ", text)
    bad = []
    for sent in re.split(r"(?<=[.!?])\s+|\n", text):
        toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9']*", sent)
        for i, tok in enumerate(toks):
            low = tok.lower()[:-2] if tok.lower().endswith("'s") else tok.lower()
            special = tok.isdigit() or (tok.isupper() and len(tok) > 1) or (tok[0].isupper() and i > 0)
            if special and low not in STOP and low not in vocab and tok.lower() not in vocab:
                bad.append(tok)
    return bad


# --- trimming ----------------------------------------------------------------------------------------------------
def trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > limit * 0.6 else cut).rstrip(" ,;:-") + "…"


def assemble(body: str, tags: list[str], credit: str, limit: int) -> str:
    tail = "\n\n" + " ".join(tags) + "\n" + credit
    room = max(20, limit - len(tail))
    return trim(body.strip(), room) + tail


# --- model -------------------------------------------------------------------------------------------------------
PROMPT = """You write short captions for a streamer-clip channel. Use ONLY the facts below. Never invent events,
names, scores or quotes that are not in the transcript or metadata. If the transcript is thin, stay vague instead of guessing.
Write: "hook" (max 10 words, punchy, streamer-clip style), "caption" (1-2 sentences, max 200 characters, describes what
actually happens). No hashtags, no emojis in the hook. Return ONLY JSON: {{"hook": "...", "caption": "..."}}

FACTS
Streamer: {streamer}
Twitch category: {game}
Stream title: {stream_title}
Why this moment was picked: {why_picked}
Clip title on Twitch: {clip_title}
Transcript: {transcript}
"""


def pick_model(host: str = OLLAMA, preferred: str | None = None, http=requests) -> str | None:
    try:
        tags = http.get(f"{host}/api/tags", timeout=3).json().get("models") or []
    except (requests.RequestException, ValueError):
        return None
    names = [m["name"] for m in tags]
    if preferred and preferred in names:
        return preferred
    for pref in ("llama3.2", "qwen2.5", "llama3.1", "gemma", "mistral", "phi"):
        for n in names:
            if n.startswith(pref):
                return n
    return names[0] if names else None


def ask_model(facts: dict, model: str, host: str = OLLAMA, http=requests) -> dict | None:
    prompt = PROMPT.format(**{k: facts.get(k) or "unknown" for k in
                              ("streamer", "game", "stream_title", "why_picked", "clip_title", "transcript")})
    try:
        r = http.post(f"{host}/api/chat", json={"model": model, "stream": False, "format": "json",
                      "options": {"temperature": 0.3}, "messages": [{"role": "user", "content": prompt}]}, timeout=120)
        data = json.loads(r.json()["message"]["content"])
        return {"hook": str(data.get("hook", "")).strip(), "caption": str(data.get("caption", "")).strip()}
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        log.info("Ollama caption failed: %s", e)
        return None


def fallback_text(facts: dict) -> dict:
    """No model: the Analyst's hook or the Twitch clip title, which are real data about the clip."""
    base = facts.get("hook") or facts.get("clip_title") or f"{facts.get('streamer') or 'Streamer'} clip"
    return {"hook": trim(base, 80), "caption": trim(base, 200)}


def generate(facts: dict, *, rules: dict | None = None, model: str | None = None, host: str = OLLAMA,
             http=requests) -> dict:
    """Full caption package for one clip, always returned (model failure → fallback text, flagged)."""
    rules = rules or load_rules()
    name = facts.get("streamer") or facts.get("login") or "the streamer"
    credit = f"🎥 twitch.tv/{facts.get('login') or slug(name)}"
    model = model or pick_model(host, http=http)
    out, source, notes = None, "fallback", []
    for _ in range(2 if model else 0):       # one retry when the first answer invents something
        out = ask_model(facts, model, host, http)
        if not out or not out["caption"]:
            out = None
            continue
        bad = unsupported_terms(out["hook"] + " " + out["caption"], facts)
        if not bad:
            source = "ollama"
            break
        notes.append("model invented: " + ", ".join(bad[:5]))
        out = None
    if out is None:
        out = fallback_text(facts)
        if model is None:
            notes.append("Ollama isn't running or has no model; used the clip's own title")
    hook, caption = out["hook"] or out["caption"], out["caption"]
    tt_tags = build_hashtags(facts, rules, "tiktok")
    ig_tags = build_hashtags(facts, rules, "instagram")
    yt_tags = build_hashtags(facts, rules, "youtube")
    emoji = (facts.get("hook_emoji") or "").strip()
    tail = f" {emoji}" if emoji else ""           # the same emoji as the video's hook, at the end of the hook line
    shown = f"{hook}{tail}"
    per = {
        "tiktok": assemble(f"{shown}\n{caption}" if hook != caption else f"{caption}{tail}", tt_tags, credit, LIMITS["tiktok"]),
        "youtube_title": trim(hook if hook else caption, LIMITS["youtube_title"] - len(" #shorts") - len(tail)) + tail + " #shorts",
        "youtube_description": assemble(caption, yt_tags, credit, LIMITS["youtube_description"]),
        "instagram": assemble(f"{shown}\n{caption}" if hook != caption else f"{caption}{tail}", ig_tags, credit, LIMITS["instagram"]),
    }
    return {"title": hook, "caption": caption, "hashtags": tt_tags, "credit": credit, "per_platform": per,
            "source": source, "model": model if source == "ollama" else None, "notes": notes}


# --- storage -----------------------------------------------------------------------------------------------------
def save(c: sqlite3.Connection, clip_id: str, data: dict, facts: dict) -> None:
    c.execute("INSERT OR REPLACE INTO clip_captions VALUES (?,?,?,?)",
              (clip_id, json.dumps(data), json.dumps(facts), time.time()))
    c.commit()


def load(c: sqlite3.Connection, clip_id: str) -> tuple[dict, dict] | None:
    r = c.execute("SELECT data, facts FROM clip_captions WHERE clip_id=?", (clip_id,)).fetchone()
    return (json.loads(r["data"]), json.loads(r["facts"] or "{}")) if r else None
