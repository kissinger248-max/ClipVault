"""Draw the app icon (the tray icon's purple play button) as .png, .ico and .icns.

Run by packaging/build.py; output goes to packaging/icons/ (not in git).
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "icons"


def draw(size: int = 1024) -> Image.Image:
    """The icon at ``size`` pixels: rounded purple square with a white play triangle."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    inset = round(size * 0.06)
    d.rounded_rectangle([inset, inset, size - inset, size - inset], radius=round(size * 0.22),
                        fill="#6441a5")  # fmt: skip
    s = size / 64
    d.polygon([(23 * s, 17 * s), (23 * s, 47 * s), (47 * s, 32 * s)], fill="white")
    return image


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw()
    big.save(OUT / "clipvault.png")
    big.save(OUT / "clipvault.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    if sys.platform == "darwin":
        big.save(OUT / "clipvault.icns")
    print(f"icons written to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
