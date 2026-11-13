"""The ClipVault logo mark, drawn with Pillow: a purple rounded square with a white
phone outline and play button (the same mark as the website's favicon).

Used for the app icon (packaging/make_icons.py), the tray icon and the website's
icons, so they always match.
"""

from __future__ import annotations

from typing import Any

PURPLE = "#7C3AED"
STATUS_COLOURS = {"green": "#22C55E", "yellow": "#EAB308", "red": "#EF4444"}


def logo(size: int, *, inset: float = 0.0) -> Any:
    """The mark at ``size`` pixels. ``inset`` (0-0.2) leaves a transparent margin."""
    from PIL import Image, ImageDraw

    scale = 4  # draw large, then shrink, for smooth edges
    big = size * scale
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    pad = round(big * inset)
    box = big - 2 * pad
    unit = box / 32  # the mark is designed on a 32-unit square
    d.rounded_rectangle(
        [pad, pad, pad + box - 1, pad + box - 1], radius=round(8 * unit), fill=PURPLE
    )
    ox, oy = pad + 4 * unit, pad + 4 * unit  # the phone sits in a 24-unit box inside
    width = max(scale, round(2 * unit))
    d.rounded_rectangle([ox + 6 * unit, oy + 2.5 * unit, ox + 18 * unit, oy + 21.5 * unit],
                        radius=round(3 * unit), outline="white", width=width)  # fmt: skip
    d.polygon([(ox + 10.5 * unit, oy + 9.2 * unit), (ox + 14.7 * unit, oy + 12 * unit),
               (ox + 10.5 * unit, oy + 14.8 * unit)], fill="white")  # fmt: skip
    return image.resize((size, size), Image.LANCZOS)


def tray_icon(status: str, size: int = 64) -> Any:
    """The mark with a status dot in the corner: green running, yellow paused, red needs you."""
    from PIL import ImageDraw

    image = logo(size)
    d = ImageDraw.Draw(image)
    dot = round(size * 0.36)
    edge = max(2, size // 20)
    d.ellipse([size - dot - 1, size - dot - 1, size - 1, size - 1],
              fill=STATUS_COLOURS[status], outline="white", width=edge)  # fmt: skip
    return image
