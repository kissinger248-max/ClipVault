"""Emoji in video hooks: finding them in text, their bundled pictures, and drawing them.

The headline font (Montserrat) has no emoji, so an emoji is never drawn as text. It is a picture
(a Twemoji PNG, CC-BY 4.0, see ``clipvault/data/emoji/ATTRIBUTION.txt``) laid over the video at the
position the layout code works out, sized to the headline's capital letters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

# Every emoji-like character: pictographs, symbols, dingbats, arrows, joiners, variation selector, keycaps.
_EMOJI_CHARS = (
    "\U0001f000-\U0001faff☀-➿⬀-⯿←-⇿⌀-⏿①-⓿■-◿"
    "⤀-⥿〰〽㊗㊙️‍⃣\U000e0020-\U000e007f"
)
EMOJI_RE = re.compile(f"[{_EMOJI_CHARS}]")
FLAG_RE = re.compile("[\U0001f1e6-\U0001f1ff]")
# How big the picture is compared with a capital letter of the headline.
SIDE_PER_CAP = 1.1
NEVER = ("🍆", "🍑", "💦")  # also enforced when someone edits the table


class EmojiError(Exception):
    """An emoji picture could not be found or read."""


@dataclass(frozen=True)
class Placement:
    """One emoji picture on the frame: its top-left corner and side in pixels."""

    char: str
    x: float
    y: float
    side: float


def is_emoji(token: str) -> bool:
    """True when ``token`` is made only of emoji characters (a single emoji or a short run of them)."""
    return bool(token) and all(EMOJI_RE.match(ch) for ch in token)


def strip_variation(char: str) -> str:
    return char.replace("️", "")


def code_name(char: str) -> str:
    """Twemoji's file name for an emoji: lower-case hex code points joined by '-', without the variation selector."""
    return "-".join(f"{ord(c):x}" for c in strip_variation(char))


def asset_bytes(char: str) -> bytes | None:
    try:
        return resources.files("clipvault.data").joinpath("emoji", f"{code_name(char)}.png").read_bytes()
    except (FileNotFoundError, OSError):
        return None


def has_asset(char: str) -> bool:
    return asset_bytes(char) is not None


@lru_cache(maxsize=1)
def _bundled_rules() -> str:
    return resources.files("clipvault.data").joinpath("hook_emojis.yaml").read_text(encoding="utf-8")


def load_rules(path: Path | None = None) -> dict[str, Any]:
    """The emoji table: ``hook_emojis.yaml`` in the app's folder when the user made one, else the built-in one."""
    from clipvault import config as cfg

    if path is None:
        mine = cfg.home_dir() / "hook_emojis.yaml"
        path = mine if mine.is_file() else None
    text = path.read_text(encoding="utf-8") if path else _bundled_rules()
    return yaml.safe_load(text) or {}


def allowed(rules: dict[str, Any] | None = None) -> dict[str, str]:
    """key -> emoji for every emoji that may be used: in the table, with a picture, and not on the never list."""
    rules = rules or load_rules()
    never = {strip_variation(e) for e in (*NEVER, *(rules.get("never") or []))}
    out = {}
    for key, char in (rules.get("emojis") or {}).items():
        if strip_variation(str(char)) in never or FLAG_RE.search(str(char)) or not has_asset(str(char)):
            continue
        out[str(key)] = str(char)
    return out


def split_headline(text: str, rules: dict[str, Any] | None = None) -> tuple[str, list[str]]:
    """The words of a hook with every emoji taken out, and the allowed emojis that were in it.

    Decoration that comes from stream titles (🔴, 🌴, flags, anything not in the allowed set) is dropped
    and never drawn. An allowed emoji that was already in the hook is returned so it can be drawn as a
    picture (and no second one is added).
    """
    chars = set(allowed(rules).values())
    found: list[str] = []
    for match in re.finditer(r"[%s]+" % _EMOJI_CHARS, text or ""):
        for ch in match.group(0):
            if strip_variation(ch) in {strip_variation(c) for c in chars} and ch not in "️‍":
                canonical = next(c for c in chars if strip_variation(c) == strip_variation(ch))
                if canonical not in found:
                    found.append(canonical)
    plain = EMOJI_RE.sub(" ", text or "")
    return " ".join(plain.split()), found


def prepare_images(placements: list[Placement], folder: Path) -> list[tuple[str, int, int]]:
    """Write each emoji picture, scaled to its on-screen size, into ``folder``.

    Returns ``(file name, x, y)`` for the overlay filter, in whole pixels. Raises ``EmojiError`` when a
    picture is missing (QA would report the same thing, but a render must never silently lack it).
    """
    import io

    from PIL import Image

    images = []
    for i, place in enumerate(placements):
        data = asset_bytes(place.char)
        if data is None:
            raise EmojiError(f"no picture for {place.char!r}")
        side = max(int(round(place.side)), 8)
        with Image.open(io.BytesIO(data)) as source:
            picture = source.convert("RGBA").resize((side, side), Image.LANCZOS)
        name = f"emoji_{i}.png"
        picture.save(folder / name)
        images.append((name, int(round(place.x)), int(round(place.y))))
    return images
