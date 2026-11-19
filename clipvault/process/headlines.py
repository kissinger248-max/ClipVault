"""Hook headlines: the line on top of every phone version (SPEC §9; rebuilt 2026-11-19).

Every clip gets several candidate headlines from different sources. Each is checked and
scored, and the best one that fits the headline box **whole** is used: nothing is ever
cut to fit (the old stand-in dropped words from the end of the clip title, which left
headlines like "Ray Mond is a master"). A candidate that doesn't fit at a readable
size is simply not used.

Sources, roughly best first:

1. **AI** (optional, with a key in Settings): reads the transcript and writes the hook.
2. **The viewer's title**, when it actually says something. Junk ("uhuhu", "kai") and
   a copied stream title ("LIVE CLICK HERE ...") are skipped.
3. **The streamer's reaction**, from what was said: shock ("no way", "oh my god"),
   hype ("let's go"), laughter, fear, rage, "clip that", sadness.
4. **The chat moment**: how much busier chat got (live and finished-stream moments).
5. **How it did**: far more views than that streamer's usual clip.
6. **The game**, then evergreen hooks ("Wait for the ENDING").

Style: at most 6 words, one word in CAPS (shown in the accent colour), curiosity and
exaggerated emotion. Not allowed: invented events or claims about real people,
fake-news framing (BREAKING, LEAKED ...) unless said, slurs, emoji. Reactions come only
from interjection-like phrases: a streamer watching a video would otherwise be
"quoted" saying the narrator's words.

Variety: the patterns used for the streamer's recent headlines score lower, so ten
clips in a row don't all say "Chat LOST IT here".
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from clipvault import config as cfg
from clipvault.logging_setup import ctx

log = logging.getLogger(__name__)

# Bump when the system writes clearly better headlines: phone versions made by an older
# version are made again (newest first, a few per round, on mains power).
VERSION = 2
GENERATION_SETTING = "headline_generation"  # bumped when an AI key is added
MAX_WORDS = 6  # the hooks it writes; a viewer's own title may have up to MAX_TITLE_WORDS
MAX_TITLE_WORDS = 8
PREFERRED_SIZE = 72  # px; fitting only at a smaller size costs a little
RECENT_LOOKED_AT = 8
CUT_SOURCES = ("live_spike", "vod_spike")
FRAMING_WORDS = ("breaking", "leaked", "banned", "exposed", "confirmed", "official")
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿️‍]")


@dataclass(frozen=True)
class ClipSignals:
    """What the headline writer knows about one clip."""

    clip_id: str
    login: str
    name: str  # the streamer's display name
    title: str = ""  # the viewer's title (or, for chat moments, the stream's)
    source: str = "viewer"
    game: str = ""
    views: int | None = None
    median_views: float | None = None  # that streamer's usual viewer clip
    surge: float | None = None  # chat moments: how many times busier chat got
    text: str = ""  # what was said
    language: str | None = None
    title_shared: bool = False  # the same title on several clips: the stream's title
    recent: tuple[str, ...] = ()  # patterns of the streamer's latest headlines


@dataclass(frozen=True)
class Headline:
    text: str
    key_word: str | None
    kind: str  # ai | title | reaction | moment | views | game | hook | custom
    pattern: str  # which template, for variety
    score: float = 0.0


# --- the streamer's reactions ------------------------------------------------------------

# Phrases a streamer says about what is happening to them. Kept to interjections, so a
# narrator's sentences (a video being watched) don't turn into "quotes".
REACTIONS: dict[str, tuple[str, ...]] = {
    "shock": (
        r"no way",
        r"oh my god",
        r"oh my gosh",
        r"what the (?:hell|heck|fuck)",
        r"are you serious",
        r"bro what",
        r"wait,? what",
        r"what just happened",
        r"holy (?:shit|crap|moly)",
        r"that'?s insane",
        r"how is that",
        r"are you kidding",
        r"whoa,? whoa",
        r"what\?!",
    ),
    "hype": (
        r"let'?s go+",
        r"we did it",
        r"we won",
        r"we'?re so back",
        r"yes,? yes",
        r"that'?s crazy",
        r"oh my god yes",
        r"dub",
    ),
    "funny": (
        r"ha(?:ha)+",
        r"lmao+",
        r"i'?m dying",
        r"i'?m crying",
        r"i can'?t breathe",
        r"bro i can'?t",
        r"\(laugh(?:s|ing|ter)\)",
    ),
    "fear": (  # clear signs only: "oh no" and "what is that" are everyday speech
        r"i'?m (?:so )?scared",
        r"i'?m terrified",
        r"oh no,? no",
        r"help me",
        r"get away",
        r"run,? run",
        r"don'?t freak out",
    ),
    "rage": (
        r"i'?m done",
        r"i quit",
        r"shut up",
        r"bro come on",
        r"come on,? bro",
        r"are you kidding me",
        r"i hate this",
    ),
    "clip": (r"clip (?:that|it)", r"somebody clip"),
    "sad": (r"rest in peace", r"passed away", r"i'?m so sorry", r"i miss (?:you|him|her)"),
}
_REACTION_RE = {kind: [re.compile(rf"\b{p}(?![\w'])", re.I) for p in phrases]
                for kind, phrases in REACTIONS.items()}  # fmt: skip

# Templates: {name} is the streamer, {names} their possessive. The CAPS word is shown in
# the accent colour. They exaggerate the *reaction*, never what happened.
REACTION_HOOKS: dict[str, tuple[str, ...]] = {
    "shock": (
        "{name} was NOT ready for this",
        "{name} CAN'T believe this",
        "Nobody was READY for this",
        "{names} reaction is PRICELESS",
    ),
    "hype": (
        "{name} goes absolutely CRAZY",
        "The HYPE is unreal",
        "{name} LOSES IT",
        "This is why we WATCH",
    ),
    "funny": (
        "{name} can't stop LAUGHING",
        "{name} is CRYING laughing",
        "This had chat in TEARS",
        "I can't stop LAUGHING",
    ),
    "fear": ("{name} got SCARED", "This SCARED {name}", "{name} was TERRIFIED"),
    "rage": ("{name} is DONE", "{name} is FURIOUS", "This made {name} RAGE"),
    "clip": ('"CLIP THAT"', "{name} said CLIP THAT", "Chat had to CLIP this"),
    "sad": ("This one HURTS", "{name} got EMOTIONAL", "Chat was in TEARS"),
}
REACTION_BASE = {"shock": 58, "hype": 56, "funny": 58, "fear": 57, "rage": 55, "clip": 60,
                 "sad": 54}  # fmt: skip

MOMENT_HOOKS = ("Chat could NOT handle this", "The moment chat EXPLODED", "Chat LOST IT here",
                "Why chat went INSANE", "Chat went absolutely FERAL")  # fmt: skip
VIEWS_HOOKS = ("Everyone is CLIPPING this", "The clip that BLEW UP", "{views} people watched THIS")
GAME_HOOKS = ("{game} moment of the YEAR", "{name} in {game} is WILD", "Peak {game} CHAOS")
EVERGREEN_HOOKS = ("Wait for the ENDING", "Watch till the END", "Nobody saw this COMING",
                   "This escalated QUICKLY", "{name} did NOT expect this",
                   "It got out of HAND")  # fmt: skip

# Viewer-title clean-up.
_STOPWORDS = frozenset(
    "a an the and or but of to in on at for with by from is are was were be been it its this "
    "that these those i me my we our you your he she they them his her their all just so very "
    "lol lmao clip clips stream live w l".split()
)
_ACTION_WORDS = frozenset(
    "gets got get loses lost wins won finds found visits meets met reacts react breaks broke "
    "falls fell crashes crashed kills killed dies died cries cried rages raged quits quit "
    "tries tried fails failed saves saved catches caught steals stole runs ran escapes "
    "escaped destroys destroyed scares scared surprises calls called sees saw tells told "
    "says said first last never best worst crazy insane funny wild reaction reacts".split()
)
_ACRONYMS = frozenset({"gta", "irl", "pd", "rp", "fps", "gg", "ai", "tv", "wtf", "omg", "cg"})
# Twitch chat slang and emote names that mean nothing to anyone outside the stream.
_SLANG = re.compile(
    r"\b(?:xdd+|xd|kekw|lul|omegalul|pog(?:champ|gers)?|monkas|pepega|sadge|copium|aware|"
    r"icant|lmao+|lol+)\b",
    re.I,
)
_GAME_ALIASES = (
    (re.compile(r"^grand theft auto (vi|6)$", re.I), "GTA 6"),
    (re.compile(r"^grand theft auto (v|5)$", re.I), "GTA 5"),
    (re.compile(r"^grand theft auto (iv|4)$", re.I), "GTA 4"),
    (re.compile(r"^grand theft auto.*$", re.I), "GTA"),
    (re.compile(r"^counter-strike.*$", re.I), "CS2"),
    (re.compile(r"^call of duty.*$", re.I), "CoD"),
    (re.compile(r"^league of legends$", re.I), "League"),
)
_NOT_GAMES = frozenset({
    "just chatting", "irl", "music", "art", "special events", "talk shows & podcasts",
    "pools, hot tubs, and beaches", "sports", "asmr",
})  # fmt: skip


# --- building candidates -----------------------------------------------------------------


def possessive(name: str) -> str:
    return f"{name}'" if name.endswith(("s", "S")) else f"{name}'s"


def game_alias(game: str) -> str:
    """A short game name for a headline ("GTA 6"), or "" when there is none."""
    plain = (game or "").strip()
    if not plain or plain.lower() in _NOT_GAMES:
        return ""
    for pattern, alias in _GAME_ALIASES:
        if pattern.match(plain):
            return alias
    plain = re.sub(r"^the ", "", plain.split(":")[0], flags=re.I).strip()
    return plain if len(plain.split()) <= 2 and len(plain) <= 14 else ""


def key_word_of(text: str) -> str | None:
    """The word shown in the accent colour: the first word written in CAPS (placeholders
    such as {game} are skipped, so call it on the template)."""
    for word in re.sub(r"\{\w+\}", " ", text).split():
        letters = re.sub(r"[^A-Za-z]", "", word)
        if len(letters) >= 2 and letters.isupper():
            return word.strip('".,!?')
    return None


def _fill(template: str, signals: ClipSignals, **extra: str) -> str:
    name = spoken_name(signals.name)
    return template.format(name=name, names=possessive(name), **extra)


def spoken_name(display_name: str) -> str:
    """How a headline names the streamer: "KaiCenat" reads as "Kai Cenat" (names that run
    capitalised words together); "xQc" and "Valkyrae" stay as they are."""
    parts = re.findall(r"[A-Z][a-z]{2,}", display_name)
    if len(parts) >= 2 and "".join(parts) == display_name:
        return " ".join(parts)
    return display_name


def _jitter(signals: ClipSignals, pattern: str) -> float:
    """A small, steady nudge so equally good patterns take turns across clips."""
    digest = hashlib.sha1(f"{signals.clip_id}|{pattern}".encode()).digest()
    return digest[0] / 255 * 3


def reactions(text: str) -> dict[str, int]:
    """How often each kind of reaction is said."""
    found = {}
    for kind, patterns in _REACTION_RE.items():
        count = sum(len(p.findall(text)) for p in patterns)
        if count:
            found[kind] = count
    return found


def clean_title(title: str) -> str:
    """A viewer's title without emoji, hashtags, mentions, links and filler."""
    text = _EMOJI.sub(" ", title or "")
    text = re.sub(r"https?://\S+|[#@]\w+", " ", text)
    text = _SLANG.sub(" ", text)
    text = re.sub(r"[^\w\s'?!&,.\-]", " ", text)
    text = re.sub(r"([?!])[?!]+", r"\1", text)  # "LANDMINE????" -> "LANDMINE?"
    text = re.sub(r"\s+", " ", text).strip(" .,-")
    return text


def _name_tokens(signals: ClipSignals) -> set[str]:
    parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", signals.name)
    return {signals.login.lower(), signals.name.lower(), *(p.lower() for p in parts if len(p) > 2)}


def _tidy_case(text: str, signals: ClipSignals) -> str:
    names = {t: t for t in _name_tokens(signals)}
    words = []
    for i, word in enumerate(text.split()):
        bare = word.lower().strip("?!,.")
        if bare in (signals.login.lower(), signals.name.lower()):
            word = word.lower().replace(bare, signals.name)
        elif bare in names and word.islower():
            word = word.capitalize()
        elif bare in _ACRONYMS:
            word = word.upper()
        elif bare == "i":
            word = "I"
        elif i == 0 and word.islower():
            word = word[0].upper() + word[1:]
        words.append(word)
    return " ".join(words)


def title_candidate(signals: ClipSignals) -> Headline | None:
    """The viewer's own title, when it actually says something (never shortened)."""
    if signals.source in CUT_SOURCES or signals.title_shared:
        return None
    text = clean_title(signals.title)
    words = text.split()
    real = [w for w in words if re.search(r"[A-Za-z]{2,}", w)]
    if len(words) > MAX_TITLE_WORDS:
        return None
    if len(real) < 3:  # a short one only as an exclamation or question: "A LANDMINE?"
        punchy = text.endswith(("?", "!")) and any(len(w.strip("?!")) >= 5 for w in real)
        if not punchy or not real:
            return None
        tidy = _tidy_case(text, signals)
        return Headline(tidy, max((w.strip("?!") for w in real), key=len), "title", "title", 58)
    game_words = {w.lower() for w in re.findall(r"\w+", signals.game or "")} | {"gta", "gtavi"}
    names = _name_tokens(signals)
    meaningful = [w for w in real if w.lower().strip("?!,.'") not in _STOPWORDS | game_words
                  | names]  # fmt: skip
    if len(meaningful) < 2:
        return None
    lowered = {w.lower().strip("?!,.") for w in words}
    score = 52 + 2 * min(len(meaningful), 4)
    if lowered & _ACTION_WORDS:
        score += 12  # something happens: "visits", "loses", "first"
    if text.endswith("?"):
        score += 9  # a question pulls people in
    tidy = _tidy_case(text, signals)
    return Headline(tidy, _title_key(tidy, meaningful), "title", "title", score)


def _title_key(text: str, meaningful: list[str]) -> str | None:
    """The word to highlight in a viewer's title: an action word ("visits", "loses"),
    else one they wrote in CAPS, else the longest word that isn't a name or a game."""
    words = [w.strip("?!,.") for w in text.split()]
    action = [w for w in words if w.lower() in _ACTION_WORDS]
    if action:
        return action[0]
    caps = [w for w in words if len(w) >= 3 and w.isupper() and w.lower() not in _ACRONYMS]
    if caps:
        return caps[0]
    plain = [w.strip("?!,.") for w in meaningful]
    return max(plain, key=len) if plain else None


def offline_candidates(signals: ClipSignals) -> list[Headline]:
    """Every headline the built-in writer can offer for a clip, scored."""
    out: list[Headline] = []
    title = title_candidate(signals)
    if title:
        out.append(title)
    english = (signals.language or "en").startswith("en")
    if english and signals.text:
        for kind, count in reactions(signals.text).items():
            base = REACTION_BASE[kind] + 5 * min(count - 1, 3)
            for i, template in enumerate(REACTION_HOOKS[kind]):
                pattern = f"reaction:{kind}:{i}"
                text = _fill(template, signals)
                out.append(Headline(text, key_word_of(template), "reaction", pattern, base))
    if signals.source in CUT_SOURCES:  # found because chat went wild (at least 2x, usually 4x+)
        surge = signals.surge or 3.0
        base = 50 + min(surge, 12)
        for i, template in enumerate(MOMENT_HOOKS):
            if i == len(MOMENT_HOOKS) - 1 and surge < 7:
                continue  # "feral" is kept for the biggest
            out.append(Headline(template, key_word_of(template), "moment", f"moment:{i}", base))
    if (signals.views and signals.median_views and signals.views >= 200
            and signals.views >= 3 * signals.median_views):  # fmt: skip
        base = 48 + min(math.log2(signals.views / max(signals.median_views, 1)), 6)
        for i, template in enumerate(VIEWS_HOOKS):
            text = template.format(views=_compact(signals.views))
            out.append(Headline(text, key_word_of(template), "views", f"views:{i}", base))
    game = game_alias(signals.game)
    if game:
        for i, template in enumerate(GAME_HOOKS):
            text = _fill(template, signals, game=game)
            out.append(Headline(text, key_word_of(template), "game", f"game:{i}", 32))
    for i, template in enumerate(EVERGREEN_HOOKS):
        text = _fill(template, signals)
        out.append(Headline(text, key_word_of(template), "hook", f"hook:{i}", 22))
    return out


def _compact(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


# --- checking and choosing ------------------------------------------------------------------


def problems(text: str, signals: ClipSignals, blocked: re.Pattern[str] | None = None) -> list[str]:
    """Why a headline may not be used (empty when it may)."""
    found = []
    words = text.split()
    if not words:
        found.append("empty")
    if len(words) > MAX_TITLE_WORDS:
        found.append(f"{len(words)} words")
    if _EMOJI.search(text):
        found.append("emoji")
    said = f"{signals.title} {signals.text}".lower()
    for word in FRAMING_WORDS:
        if re.search(rf"\b{word}\b", text, re.I) and word not in said:
            found.append(f"'{word.upper()}' is not said in the clip")
    if blocked is not None and blocked.search(text):
        found.append("a blocked word")
    return found


def choose(
    candidates: Iterable[Headline],
    signals: ClipSignals,
    size_of: Callable[[str], float | None],
    *,
    blocked: re.Pattern[str] | None = None,
    exclude: Iterable[str] = (),
) -> list[Headline]:
    """Usable candidates, best first. ``size_of`` says the font size a headline fits at
    (None when it doesn't fit whole); one that doesn't fit is never used."""
    skip = {e.lower() for e in exclude}
    recent = list(signals.recent)
    recent_kinds = [pattern.split(":")[0] for pattern in recent]
    ranked = []
    for item in candidates:
        if item.text.lower() in skip or problems(item.text, signals, blocked):
            continue
        size = size_of(item.text)
        if size is None:
            continue
        score = item.score + _jitter(signals, item.pattern)
        if size < PREFERRED_SIZE:
            score -= 6
        if item.pattern in recent[:RECENT_LOOKED_AT]:
            score -= 32 - 3 * recent.index(item.pattern)  # used just now: much less likely
        elif item.kind in recent_kinds[:2]:
            score -= 4
        ranked.append(Headline(item.text, item.key_word, item.kind, item.pattern, score))
    ranked.sort(key=lambda h: -h.score)
    return ranked


# --- the clip in the database --------------------------------------------------------------


def current_version(conn: sqlite3.Connection) -> int:
    """The headline system's version plus how often an AI key was added since."""
    from clipvault.alerts import get_setting

    return VERSION * 1000 + int(get_setting(conn, GENERATION_SETTING, "0") or 0)


def ai_key_added(conn: sqlite3.Connection) -> None:
    """A new AI key: headlines written without it are written again (once)."""
    from clipvault.alerts import get_setting, set_setting

    set_setting(conn, GENERATION_SETTING, str(int(get_setting(conn, GENERATION_SETTING, "0")
                                                  or 0) + 1))  # fmt: skip


def signals_for(conn: sqlite3.Connection, config: cfg.Config, clip_id: str) -> ClipSignals:
    """Everything the writer may use for one clip, from the database and the library."""
    from clipvault.library import storage

    clip = conn.execute(
        "SELECT c.*, s.login, s.display_name FROM clips c JOIN streamers s "
        "ON s.id = c.streamer_id WHERE c.id = ?",
        (clip_id,),
    ).fetchone()
    if clip is None:
        raise KeyError(clip_id)
    library = storage.open_library(conn, config)
    text = ""
    video = storage.resolve(library, clip["file_path"])
    if video is not None:
        from clipvault.render.engine import _read_transcript

        text = _read_transcript(video).text
    views = [r[0] for r in conn.execute(
        "SELECT view_count FROM clips WHERE streamer_id = ? AND source IN ('viewer', 'auto') "
        "AND view_count IS NOT NULL", (clip["streamer_id"],))]  # fmt: skip
    median = sorted(views)[len(views) // 2] if views else None
    shared = (
        bool(clip["title"])
        and conn.execute(
            "SELECT count(*) FROM clips WHERE streamer_id = ? AND title = ?",
            (clip["streamer_id"], clip["title"]),
        ).fetchone()[0]
        >= 3
    )
    recent = tuple(r[0] for r in conn.execute(
        "SELECT headline_source FROM clips WHERE streamer_id = ? AND id != ? "
        "AND headline_source IS NOT NULL ORDER BY updated_at DESC LIMIT ?",
        (clip["streamer_id"], clip_id, RECENT_LOOKED_AT)))  # fmt: skip
    return ClipSignals(
        clip_id=clip_id,
        login=clip["login"],
        name=clip["display_name"] or clip["login"],
        title=clip["title"] or "",
        source=clip["source"],
        game=clip["game_name"] or "",
        views=clip["view_count"],
        median_views=median,
        surge=clip["surge"],
        text=text,
        language=clip["language"],
        title_shared=shared,
        recent=tuple(_pattern_of(r) for r in recent),
    )


def _pattern_of(source: str) -> str:
    """Stored as "kind|pattern" (see ``store``); old rows hold just a kind."""
    return source.split("|", 1)[-1]


def store(conn: sqlite3.Connection, clip_id: str, headline: Headline, version: int) -> None:
    conn.execute(
        "UPDATE clips SET headline = ?, headline_key = ?, headline_source = ?, "
        "headline_version = ? WHERE id = ?",
        (headline.text, headline.key_word, f"{headline.kind}|{headline.pattern}", version, clip_id),
    )


def write(
    conn: sqlite3.Connection,
    config: cfg.Config,
    clip_id: str,
    *,
    provider: Any = "auto",
    exclude: Iterable[str] = (),
) -> Headline:
    """Write and store the best headline for a clip (AI when a key is stored, else built-in)."""
    from clipvault.process import transcribe
    from clipvault.process import write as ai
    from clipvault.render.ass import layout_headline
    from clipvault.render.template import load_look

    look = load_look(config)
    signals = signals_for(conn, config, clip_id)
    blocked = transcribe.mask_pattern(transcribe.load_mask_words())

    def size_of(text: str) -> float | None:
        layout = layout_headline(text, look)
        return layout.size if layout.fits else None

    if provider == "auto":
        provider = ai.build_provider(config)
    options: list[Headline] = []
    if provider is not None and not exclude:
        options = _ai_options(conn, config, signals, provider, blocked, size_of)
    options += choose(offline_candidates(signals), signals, size_of, blocked=blocked,
                      exclude=exclude)  # fmt: skip
    if not options:  # nothing fits: the shortest evergreen hook always does
        options = [Headline("Wait for the ENDING", "ENDING", "hook", "hook:0")]
    best = options[0]
    store(conn, clip_id, best, current_version(conn))
    log.info("headline written (%s): %s", best.kind, best.text,
             extra=ctx(clip=clip_id, streamer=signals.login, job="headline"))  # fmt: skip
    return best


def _facts(conn: sqlite3.Connection, config: cfg.Config, signals: ClipSignals) -> Any:
    """What the AI is told: the transcript and title, plus what else is known."""
    import dataclasses

    from clipvault import jobs
    from clipvault.library import storage

    clip = conn.execute("SELECT * FROM clips WHERE id = ?", (signals.clip_id,)).fetchone()
    facts = jobs.clip_facts(conn, storage.open_library(conn, config), clip)
    notes = []
    title = facts.title
    if signals.source in CUT_SOURCES or signals.title_shared:
        title = ""  # the stream's title, not this moment's
    if signals.source in CUT_SOURCES and signals.surge:
        when = "live, during the stream" if signals.source == "live_spike" else "in the replay"
        notes.append(f"Why this clip exists: chat got {signals.surge:.1f} times busier than "
                     f"usual ({when}).")  # fmt: skip
    if signals.views is not None and signals.median_views:
        notes.append(f"Views: {signals.views} (this streamer's usual clip: "
                     f"{signals.median_views:.0f}).")  # fmt: skip
    rows = conn.execute(
        "SELECT headline FROM clips WHERE streamer_id = ? AND id != ? AND headline IS NOT NULL "
        "ORDER BY updated_at DESC LIMIT ?",
        (clip["streamer_id"], signals.clip_id, RECENT_LOOKED_AT),
    )
    recent = tuple(row[0] for row in rows)
    return dataclasses.replace(facts, title=title, notes=tuple(notes), recent=recent)


def _ai_options(conn, config, signals, provider, blocked, size_of) -> list[Headline]:
    from clipvault.process import write as ai

    facts = _facts(conn, config, signals)
    try:
        copy, why = ai.write_copy(facts, provider, blocked=blocked,
                                  fits=lambda text: size_of(text) is not None)  # fmt: skip
    except ai.LLMError as exc:
        log.warning("AI headline unavailable, using the built-in writer: %s", exc,
                    extra=ctx(clip=signals.clip_id, job="headline"))  # fmt: skip
        return []
    if copy is None:
        log.info("no usable AI headline (%s); using the built-in writer", "; ".join(why),
                 extra=ctx(clip=signals.clip_id, job="headline"))  # fmt: skip
        return []
    conn.execute(
        "UPDATE clips SET post_caption = ?, hashtags_json = ?, copy_provider = ? WHERE id = ?",
        (copy.post_caption, copy.hashtags_json(), copy.provider, signals.clip_id),
    )
    key = copy.key_word or key_word_of(copy.headline)
    return [Headline(copy.headline, key, "ai", "ai", 100.0)]


def ensure(conn: sqlite3.Connection, config: cfg.Config, clip_id: str) -> Headline:
    """The clip's headline: the stored one if this version wrote it (or the user typed
    it), otherwise a new one."""
    row = conn.execute(
        "SELECT headline, headline_key, headline_source, headline_version FROM clips WHERE id = ?",
        (clip_id,),
    ).fetchone()
    if row is not None and row["headline"]:
        source = row["headline_source"] or ""
        if source.startswith("custom") or row["headline_version"] == current_version(conn):
            kind, _, pattern = source.partition("|")
            return Headline(row["headline"], row["headline_key"], kind, pattern or kind)
    return write(conn, config, clip_id)


def set_custom(conn: sqlite3.Connection, clip_id: str, text: str, look: Any) -> Headline:
    """A headline the user typed. Raises ValueError when it can't be used as is."""
    from clipvault.render.ass import layout_headline

    text = " ".join(text.split())
    if not text:
        raise ValueError("Type a headline.")
    if _EMOJI.search(text):
        raise ValueError("Emoji can't be drawn on the video; leave them out.")
    if not layout_headline(text, look).fits:
        raise ValueError("That's too long for two lines on the video; make it shorter.")
    headline = Headline(text, key_word_of(text), "custom", "custom")
    store(conn, clip_id, headline, current_version(conn))
    return headline
