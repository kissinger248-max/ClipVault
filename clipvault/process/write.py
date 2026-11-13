"""Hook headline, post caption and hashtags written by an LLM (SPEC §9).

The model is reached through a small provider interface, so any service with an
OpenAI-style chat endpoint works (Groq is the default; docs checked 2026-11-13:
``POST https://api.groq.com/openai/v1/chat/completions``, ``max_completion_tokens``,
``response_format`` with a strict ``json_schema`` on ``openai/gpt-oss-20b``).

Every rule in the prompt is also checked in code; an answer that breaks one is
asked for again (3 tries). The headline must also fit the on-screen box.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from clipvault import config as cfg
from clipvault.logging_setup import ctx

GROQ_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-20b"
MAX_HEADLINE_WORDS = 6
ATTEMPTS = 3
TIMEOUT_S = 30.0
MAX_TRANSCRIPT_CHARS = 4000
# Fake-news framing, refused unless these words were actually said or in the title.
FRAMING_WORDS = ("breaking", "leaked", "banned", "exposed", "confirmed", "official")
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f900-\U0001f9ff\U00002190-\U000021ff"
    "\U00002b00-\U00002bff️‍]"
)
_HASHTAG = re.compile(r"^[a-z0-9_]{2,40}$")

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "post_caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "post_caption", "hashtags"],
    "additionalProperties": False,
}


class LLMError(Exception):
    """The model could not be reached or gave no usable answer."""


class Provider(Protocol):
    name: str

    def complete(self, messages: list[dict[str, str]]) -> str: ...


@dataclass
class OpenAIStyleProvider:
    """Any OpenAI-compatible chat endpoint (Groq by default)."""

    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str = GROQ_URL
    name: str = "groq"
    http: httpx.Client | None = None
    sleep: Callable[[float], None] = time.sleep

    def complete(self, messages: list[dict[str, str]]) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.7,
            "max_completion_tokens": 600,
        }
        if self.model.startswith("openai/gpt-oss"):
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "clip_copy", "strict": True, "schema": SCHEMA},
            }
        else:
            body["response_format"] = {"type": "json_object"}
        client = self.http or httpx.Client(timeout=TIMEOUT_S)
        try:
            for attempt in range(3):
                try:
                    response = client.post(
                        f"{self.base_url}/chat/completions",
                        json=body,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                except httpx.HTTPError as exc:
                    raise LLMError(
                        f"could not reach {self.name}: {exc.__class__.__name__}"
                    ) from exc
                if response.status_code == 429 and attempt < 2:
                    wait = float(response.headers.get("retry-after") or 5)
                    self.sleep(min(wait, 60))
                    continue
                if response.status_code in (401, 403):
                    raise LLMError(f"{self.name} refused the API key ({response.status_code})")
                if response.status_code >= 400:
                    raise LLMError(
                        f"{self.name} answered {response.status_code}: {response.text[:200]}"
                    )
                try:
                    return response.json()["choices"][0]["message"]["content"] or ""
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise LLMError(f"unexpected answer from {self.name}") from exc
            raise LLMError(f"{self.name} rate limit: still busy after waiting")
        finally:
            if self.http is None:
                client.close()


def build_provider(config: cfg.Config) -> Provider | None:
    """The configured model, or None when no key is stored (headlines then come from titles)."""
    key = cfg.get_secret("llm_api_key") or cfg.get_secret("groq_api_key")
    if not key:
        return None
    return OpenAIStyleProvider(
        api_key=key, model=config.llm.model, base_url=config.llm.base_url, name=config.llm.provider
    )


# --- input and prompt ------------------------------------------------------------------


@dataclass(frozen=True)
class ClipFacts:
    """What the writer may know about a clip."""

    login: str
    display_name: str
    title: str
    game: str = ""
    transcript: list[tuple[float, str]] = field(default_factory=list)  # (seconds, words)
    chat_words: list[str] = field(default_factory=list)
    disclosure: str | None = None  # campaign clips: required first line of the caption

    @property
    def credit(self) -> str:
        return f"@{self.login} on Twitch"

    def source_text(self) -> str:
        return (self.title + " " + " ".join(text for _, text in self.transcript)).lower()


@dataclass(frozen=True)
class Copy:
    headline: str
    post_caption: str
    hashtags: tuple[str, ...]
    provider: str = ""

    def hashtags_json(self) -> str:
        return json.dumps(list(self.hashtags))


SYSTEM_PROMPT = f"""You write the on-screen hook headline and the post text for a short vertical
clip from a Twitch stream. Answer with JSON only:
{{"headline": "...", "post_caption": "...", "hashtags": ["...", "..."]}}

Rules:
- headline: at most {MAX_HEADLINE_WORDS} words. Describe only what happens in the transcript or
  the clip title. Make people curious, but never invent events, quotes or outcomes.
- No claims about real people beyond what the clip shows. No fake-news framing such as
  BREAKING, LEAKED, BANNED or EXPOSED unless the streamer says that word in the clip.
- No slurs, insults about appearance, or emoji anywhere.
- post_caption: one or two short sentences, then the credit given to you, exactly.
  If a disclosure line is given, the caption starts with it.
- hashtags: 3 to 5, lowercase letters and digits only, no '#', no spaces.
"""


def build_messages(facts: ClipFacts, problems: list[str] | None = None) -> list[dict[str, str]]:
    """The chat messages for one attempt (with the last attempt's problems, if any)."""
    lines = []
    total = 0
    for seconds, text in facts.transcript:
        line = f"[{int(seconds) // 60}:{int(seconds) % 60:02d}] {text}"
        total += len(line)
        if total > MAX_TRANSCRIPT_CHARS:
            lines.append("[...]")
            break
        lines.append(line)
    user = [
        f"Streamer: {facts.display_name} (credit: {facts.credit})",
        f"Clip title on Twitch: {facts.title or '(none)'}",
        f"Game or category: {facts.game or '(unknown)'}",
    ]
    if facts.chat_words:
        user.append("Words chat repeated at this moment: " + ", ".join(facts.chat_words[:10]))
    if facts.disclosure:
        user.append(f"Disclosure line (must start the caption): {facts.disclosure}")
    user.append("Transcript:\n" + ("\n".join(lines) if lines else "(no speech)"))
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user)},
    ]
    if problems:
        messages.append(
            {"role": "user", "content": "Your last answer broke these rules: " + "; ".join(problems)
             + ". Write a new answer that follows every rule."}
        )  # fmt: skip
    return messages


# --- checking an answer ---------------------------------------------------------------


def parse_answer(text: str) -> dict[str, Any]:
    """The JSON object in a model answer (tolerates a code fence around it)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the answer")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("the answer is not a JSON object")
    return data


def check_copy(
    data: dict[str, Any],
    facts: ClipFacts,
    *,
    blocked: re.Pattern[str] | None = None,
    fits: Callable[[str], bool] | None = None,
) -> tuple[Copy | None, list[str]]:
    """Validate (and tidy) an answer; returns the copy, or None and what was wrong."""
    problems: list[str] = []
    headline = data.get("headline")
    caption = data.get("post_caption")
    tags = data.get("hashtags")
    if not isinstance(headline, str) or not headline.strip():
        problems.append("headline missing")
        headline = ""
    if not isinstance(caption, str) or not caption.strip():
        problems.append("post_caption missing")
        caption = ""
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        problems.append("hashtags must be a list of words")
        tags = []

    headline = " ".join(headline.split()).strip(" \"'")
    words = headline.split()
    if len(words) > MAX_HEADLINE_WORDS:
        problems.append(f"headline has {len(words)} words (at most {MAX_HEADLINE_WORDS})")
    said = facts.source_text()
    everything = f"{headline} {caption}"
    if _EMOJI.search(everything):
        problems.append("no emoji allowed")
    for word in FRAMING_WORDS:
        if re.search(rf"\b{word}\b", everything, re.IGNORECASE) and word not in said:
            problems.append(f"'{word.upper()}' is not said in the clip")
    if blocked is not None and blocked.search(everything):
        problems.append("contains a blocked word")
    if headline and fits is not None and not fits(headline):
        problems.append("headline too long to fit on screen; use shorter words")

    caption = " ".join(caption.split())
    if facts.credit.lower() not in caption.lower():
        caption = f"{caption.rstrip()} {facts.credit}".strip()
    if facts.disclosure and not caption.startswith(facts.disclosure):
        caption = f"{facts.disclosure}\n{caption}"

    cleaned_tags = []
    for tag in tags:
        tag = tag.strip().lstrip("#").lower()
        if _HASHTAG.match(tag) and tag not in cleaned_tags:
            cleaned_tags.append(tag)
    if not 3 <= len(cleaned_tags) <= 5:
        problems.append(f"{len(cleaned_tags)} usable hashtags (need 3 to 5, lowercase, no spaces)")

    if problems:
        return None, problems
    return Copy(headline, caption, tuple(cleaned_tags)), []


def write_copy(
    facts: ClipFacts,
    provider: Provider,
    *,
    blocked: re.Pattern[str] | None = None,
    fits: Callable[[str], bool] | None = None,
    attempts: int = ATTEMPTS,
) -> tuple[Copy | None, list[str]]:
    """Ask for the copy up to ``attempts`` times; returns it, or None and the last problems.

    Raises ``LLMError`` when the model can't be reached at all (try again later;
    that is not the clip's fault).
    """
    problems: list[str] = []
    for attempt in range(1, attempts + 1):
        answer = provider.complete(build_messages(facts, problems))
        try:
            data = parse_answer(answer)
        except (ValueError, json.JSONDecodeError) as exc:
            problems = [f"answer was not valid JSON ({exc})"]
            continue
        copy, problems = check_copy(data, facts, blocked=blocked, fits=fits)
        if copy is not None:
            return Copy(copy.headline, copy.post_caption, copy.hashtags, provider.name), []
        log.info("headline attempt %d rejected: %s", attempt, "; ".join(problems),
                 extra=ctx(streamer=facts.login, job="write"))  # fmt: skip
    return None, problems
