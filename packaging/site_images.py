# ruff: noqa: E501  (SVG markup reads better unwrapped)
"""Make the website's placeholder pictures, icons and share image (site/assets/img/).

    python packaging/site_images.py

Placeholders are dark abstract frames only (no people, logos, streamer names or
platform branding), named ``placeholder-*`` so they are easy to find and replace.
Icons and the share image are drawn from the purple logo mark.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site" / "assets" / "img"
FONTS = ROOT / "assets" / "fonts"
ACCENT = "#7C3AED"

# (base, band) colour pairs from the mockups' clip cards.
TONES = [("#1C2230", "#2A3346"), ("#2B1E2E", "#3A2840"), ("#1F2B24", "#2C3D33"),
         ("#2E2A1E", "#423B29"), ("#251E33", "#352A4A"), ("#1E2A2E", "#2B3C42")]  # fmt: skip


def frame_svg(width: int, height: int, base: str, band: str, seed: int) -> str:
    """A dark, softly lit abstract frame: gradient, a lighter band and two blurred shapes."""
    cx1, cy1 = (0.25 + 0.1 * (seed % 3)) * width, 0.35 * height
    cx2, cy2 = (0.7 - 0.08 * (seed % 2)) * width, (0.62 + 0.05 * (seed % 3)) * height
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">
<defs>
<linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{base}"/><stop offset="1" stop-color="#0B0B0D"/></linearGradient>
<filter id="b" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="{min(width, height) * 0.08:.1f}"/></filter>
</defs>
<rect width="{width}" height="{height}" fill="url(#g)"/>
<rect y="{height * 0.38:.0f}" width="{width}" height="{height * 0.24:.0f}" fill="{band}" opacity="0.85"/>
<g filter="url(#b)" opacity="0.55">
<circle cx="{cx1:.0f}" cy="{cy1:.0f}" r="{min(width, height) * 0.22:.0f}" fill="{band}"/>
<ellipse cx="{cx2:.0f}" cy="{cy2:.0f}" rx="{width * 0.22:.0f}" ry="{height * 0.12:.0f}" fill="#8B5CF6" opacity="0.35"/>
</g>
</svg>
"""


LOGO_SVG = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="8" fill="{ACCENT}"/>
<g transform="translate(4 4)" fill="none" stroke="#FFFFFF" stroke-width="2" stroke-linejoin="round">
<rect x="6" y="2.5" width="12" height="19" rx="3"/><path d="M10.5 9.2l4.2 2.8-4.2 2.8z" fill="#FFFFFF"/>
</g>
</svg>
"""


def logo_png(size: int) -> Image.Image:
    """The logo mark drawn at ``size`` pixels (square, rounded, white phone and play)."""
    scale = size / 32
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=round(8 * scale), fill=ACCENT)
    off = 4 * scale
    width = max(1, round(2 * scale))
    d.rounded_rectangle([off + 6 * scale, off + 2.5 * scale, off + 18 * scale, off + 21.5 * scale],
                        radius=round(3 * scale), outline="white", width=width)  # fmt: skip
    d.polygon([(off + 10.5 * scale, off + 9.2 * scale), (off + 14.7 * scale, off + 12 * scale),
               (off + 10.5 * scale, off + 14.8 * scale)], fill="white")  # fmt: skip
    return image


def share_image() -> Image.Image:
    """1200x630 share card: the hero's headline beside a phone frame, on the site's dark."""
    image = Image.new("RGB", (1200, 630), "#0B0B0D")
    d = ImageDraw.Draw(image)
    glow = Image.new("RGBA", (1200, 630), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([760, 60, 1240, 600], fill=(124, 58, 237, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(110))
    image.paste(glow, (0, 0), glow)
    bold = ImageFont.truetype(str(FONTS / "Montserrat-ExtraBold.ttf"), 76)
    semi = ImageFont.truetype(str(FONTS / "Montserrat-SemiBold.ttf"), 30)
    image.paste(logo_png(72), (80, 70), logo_png(72))
    d.text(
        (170, 84),
        "ClipVault",
        font=ImageFont.truetype(str(FONTS / "Montserrat-ExtraBold.ttf"), 40),
        fill="#F4F4F5",
    )
    d.text((80, 210), "Catch every", font=bold, fill="#F4F4F5")
    d.text((80, 300), "moment.", font=bold, fill="#F4F4F5")
    d.text((80, 390), "Keep it forever.", font=bold, fill="#A78BFA")
    d.text((80, 510), "Twitch moments, saved and ready for your phone.", font=semi, fill="#A1A1AA")
    # phone frame
    d.rounded_rectangle([900, 80, 1130, 560], radius=40, fill="#131316", outline="#232327", width=8)
    for i, (base, band) in enumerate(TONES[:3]):
        top = 130 + i * 140
        d.rounded_rectangle([930, top, 1100, top + 120], radius=14, fill=base)
        d.rectangle([930, top + 44, 1100, top + 76], fill=band)
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for i, (base, band) in enumerate(TONES, start=1):
        (OUT / f"placeholder-clip-{i}.svg").write_text(frame_svg(640, 360, base, band, i), "utf-8")
    for i, (base, band) in enumerate(TONES[:4], start=1):
        (OUT / f"placeholder-phone-{i}.svg").write_text(
            frame_svg(360, 640, base, band, i + 2), "utf-8"
        )
    (OUT / "favicon.svg").write_text(LOGO_SVG, "utf-8")
    logo_png(32).save(OUT / "favicon-32.png")
    apple = Image.new("RGB", (180, 180), ACCENT)
    apple.paste(logo_png(180), (0, 0), logo_png(180))
    apple.save(OUT / "apple-touch-icon.png")
    share_image().save(OUT / "share.png", optimize=True)
    print(f"images written to {OUT}")


if __name__ == "__main__":
    main()
