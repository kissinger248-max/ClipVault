"""The look template: every number that shapes a vertical video, loaded from YAML.

The bundled ``look_default.yaml`` holds the SPEC §10 values. A user template in
``<home>/looks/<name>.yaml`` only needs the keys it changes.
"""

from __future__ import annotations

import struct
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from clipvault import config as cfg

# Source checkout: <repo>/assets/fonts. Installed app: bundled next to the code.
FONTS_DIR = cfg.resource_dir() / "assets" / "fonts"


class Look(dict):
    """Template values with attribute access: ``look.caption.size``."""

    def __getattr__(self, name: str) -> Any:
        try:
            value = self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        return Look(value) if isinstance(value, dict) else value


def _merge(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_look(config: cfg.Config | None = None) -> Look:
    """Load the look for ``config`` (default template, user template, accent override)."""
    text = resources.files("clipvault.data").joinpath("look_default.yaml").read_text("utf-8")
    values = yaml.safe_load(text)
    if config is not None:
        if config.look.template != "default":
            path = cfg.home_dir() / "looks" / f"{config.look.template}.yaml"
            if not path.is_file():
                raise cfg.ConfigError(f"look template not found: {path}")
            values = _merge(values, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        values["accent"] = config.look.accent
        values["headline_style"] = config.look.headline_style
        values["credit"] = {**values["credit"], "enabled": config.look.show_credit}
        if config.look.caption_size != cfg.LookConfig().caption_size:  # set by the user
            # Bigger captions also need a bigger lower limit for over-long words.
            values["caption"] = {**values["caption"], "size": config.look.caption_size,
                                 "min_size": min(values["caption"].get("min_size", 56),
                                                 config.look.caption_size)}  # fmt: skip
    values.setdefault("headline_style", "box")
    return Look(values)


class Font:
    """A bundled TrueType font, measured the way libass draws it.

    libass does not use an ASS "font size" as the letter size: it scales the font
    so that the Windows ascent + descent equals that number. For Montserrat that
    makes letters only 64% of the stated size. Sizes in the look template are real
    letter sizes in pixels, and :meth:`ass_size` converts them.
    """

    def __init__(self, name: str, fonts_dir: Path = FONTS_DIR) -> None:
        self.path = fonts_dir / f"{name}.ttf"
        data = self.path.read_bytes()
        count = struct.unpack(">H", data[4:6])[0]
        tables = {
            data[12 + 16 * i : 16 + 16 * i].decode("latin-1"): struct.unpack(
                ">I", data[20 + 16 * i : 24 + 16 * i]
            )[0]
            for i in range(count)
        }
        self.units_per_em = struct.unpack(">H", data[tables["head"] + 18 : tables["head"] + 20])[0]
        os2 = tables["OS/2"]
        self.win_ascent, self.win_descent = struct.unpack(">HH", data[os2 + 74 : os2 + 78])
        from PIL import ImageFont

        family, style = ImageFont.truetype(str(self.path), 20).getname()
        self.family = family or name
        self.full_name = f"{family} {style}".strip() if style != "Regular" else family

    @lru_cache(maxsize=64)  # noqa: B019 - fonts live for the whole process
    def _pil(self, px: float) -> Any:
        from PIL import ImageFont

        return ImageFont.truetype(str(self.path), px)

    def ass_size(self, px: float) -> float:
        """ASS font size that draws letters ``px`` pixels tall (em size)."""
        return px * (self.win_ascent + self.win_descent) / self.units_per_em

    def width(self, text: str, px: float) -> float:
        """Width of ``text`` in pixels at letter size ``px``."""
        return float(self._pil(px).getlength(text))

    def ascent(self, px: float) -> float:
        """Distance from the top of a line (as libass places it) down to the baseline."""
        return self.win_ascent * px / self.units_per_em

    def line_height(self, px: float) -> float:
        """Height libass reserves for one line."""
        return (self.win_ascent + self.win_descent) * px / self.units_per_em

    def cap_height(self, px: float) -> float:
        """Height of a capital letter."""
        _x0, top, _x1, _bottom = self._pil(px).getbbox("H", anchor="ls")
        return float(-top)


@lru_cache(maxsize=16)
def get_font(name: str) -> Font:
    """Return the bundled font called ``name`` (for example ``Montserrat-Black``)."""
    return Font(name)
