"""Choosing the emoji for a video hook.

Rules (all checked in code, a model can only choose within them):

- one emoji (two at most, as the Look setting says), from the allowed list in ``hook_emojis.yaml``;
- picked from what the hook, the clip title and the transcript say, the moment type and the game;
- none when the hook already has one, when nothing clearly fits, or for a sensitive moment
  (injuries, police, fights, blocked words, or a clip whose last render QA flagged);
- never 🍆🍑💦 or a flag.

A local Ollama model decides when it is running; otherwise the table does.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from clipvault.render import emoji as emoji_mod

log = logging.getLogger(__name__)
OLLAMA = "http://localhost:11434"
THRESHOLD = 2  # a hook/title word counts 3, what was said counts 1 (up to 3), the game 1
MODEL_TIMEOUT_S = 12


def _words(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9'$ ]+", " ", (text or "").lower()) + " "


def _hits(text: str, keywords: list[str]) -> int:
    padded = _words(text)
    return sum(1 for k in keywords if f" {_words(str(k)).strip()} " in padded)


def is_sensitive(text: str, rules: dict[str, Any], blocked: re.Pattern[str] | None = None) -> bool:
    """True for moments that should never get an emoji (injuries, police, fights, ...) or contain blocked words."""
    if blocked is not None and blocked.search(text or ""):
        return True
    return bool(_hits(text, [str(w) for w in rules.get("sensitive") or []]))


def score_table(
    headline: str, title: str, transcript: str, game: str, rules: dict[str, Any], allowed: dict[str, str]
) -> list[tuple[int, str]]:
    """(score, emoji key) for every moment type with a signal, best first."""
    scores: dict[str, int] = {}
    for moment in (rules.get("moments") or {}).values():
        key = str(moment.get("emoji"))
        if key not in allowed:
            continue
        words = [str(k) for k in moment.get("keywords") or []]
        n = 3 * min(_hits(f"{headline} {title}", words), 2) + min(_hits(transcript, words), 3)
        if n:
            scores[key] = scores.get(key, 0) + n
    padded = (game or "").lower()
    for part, key in (rules.get("games") or {}).items():
        if str(part) in padded and str(key) in allowed:
            scores[str(key)] = scores.get(str(key), 0) + 1
    return sorted(((s, k) for k, s in scores.items()), key=lambda t: (-t[0], t[1]))


def ask_model(
    headline: str, transcript: str, game: str, allowed: dict[str, str], *, model: str, http: Any, host: str = OLLAMA
) -> str | None:
    """Let a local model pick a key from ``allowed`` (or "none"). Returns the key, "none", or None when unusable."""
    keys = ", ".join(allowed)
    prompt = (
        "Pick ONE emoji key that matches the meaning of this short video hook, or \"none\" if nothing clearly fits.\n"
        f"Allowed keys: {keys}, none.\nHook: {headline}\nGame: {game or 'unknown'}\n"
        f"What was said: {transcript[:600]}\n"
        'Answer only with JSON like {"emoji": "laugh"}.'
    )
    try:
        reply = http.post(
            f"{host}/api/chat",
            json={"model": model, "stream": False, "format": "json", "options": {"temperature": 0},
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=MODEL_TIMEOUT_S,
        )
        answer = str(json.loads(reply.json()["message"]["content"]).get("emoji", "")).strip().lower()
    except Exception as exc:  # noqa: BLE001 - a model that is down or rambling must never stop a render
        log.info("emoji model unavailable: %s", exc)
        return None
    return answer if answer in allowed or answer == "none" else None


def choose(
    *,
    headline: str,
    title: str = "",
    transcript: str = "",
    game: str = "",
    count: int = 1,
    qa_flagged: bool = False,
    blocked: re.Pattern[str] | None = None,
    rules: dict[str, Any] | None = None,
    http: Any = None,
    model: str | None = None,
) -> list[str]:
    """The emojis for ``headline`` (empty list for none). ``count`` is the most to use (0 turns emojis off)."""
    if count <= 0:
        return []
    rules = rules or emoji_mod.load_rules()
    allowed = emoji_mod.allowed(rules)
    plain, present = emoji_mod.split_headline(headline, rules)
    if present:                                      # the hook already has one: keep it, add nothing
        return present[:count]
    if qa_flagged or not plain:
        return []
    context = f"{plain} {title} {transcript}"
    if is_sensitive(context, rules, blocked):
        return []
    ranked = score_table(plain, title, transcript, game, rules, allowed)
    chosen: list[str] = []
    if http is not None and model:
        answer = ask_model(plain, transcript, game, allowed, model=model, http=http)
        if answer == "none":
            return []
        if answer:
            chosen = [answer]
    if not chosen:
        chosen = [k for s, k in ranked[:count] if s >= THRESHOLD][:1]   # the first by score; see below for a second
        if count >= 2 and chosen:
            chosen += [k for s, k in ranked[1:2] if s >= THRESHOLD and k != chosen[0]]
    return [allowed[k] for k in chosen][:count]
