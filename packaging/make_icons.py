"""Draw the app icon (the ClipVault logo mark, clipvault/brand.py) as .png, .ico and .icns.

Run by packaging/build.py; output goes to packaging/icons/ (not in git).
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

OUT = Path(__file__).resolve().parent / "icons"


def draw(size: int = 1024) -> Image.Image:
    """The logo mark (clipvault/brand.py) with a small margin, as app icons usually have."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from clipvault.brand import logo

    return logo(size, inset=0.06)


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
