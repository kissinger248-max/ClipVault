"""Automatic quality checks on a finished render (SPEC §11). All must pass before posting."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from clipvault.render import emoji as emoji_mod
from clipvault.render import ffmpeg
from clipvault.render.ass import Overlay
from clipvault.render.layouts import SPLIT, Plan
from clipvault.render.template import Look, get_font

LOUDNESS_TOLERANCE = 2.0
# Words that turn a clip into fake news unless the streamer actually said them (SPEC §9).
HYPE_WORDS = ("breaking", "leaked", "banned")


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    still: str | None = None

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append(Check(name, bool(passed), detail))

    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]

    def to_json(self) -> str:
        return json.dumps(
            {"passed": self.passed, "still": self.still, "checks": [asdict(c) for c in self.checks]}
        )


def blocked_words(text: str, mask: re.Pattern | None, spoken: str = "") -> list[str]:
    """Words in ``text`` that must not be burned in: masked words, and unspoken hype words."""
    found = [m.group(0) for m in mask.finditer(text)] if mask else []
    said = spoken.lower()
    found += [w for w in HYPE_WORDS if re.search(rf"\b{w}\b", text.lower()) and w not in said]
    return found


def run_checks(
    *,
    output: Path,
    overlay: Overlay,
    plan: Plan,
    look: Look,
    headline: str | None,
    transcript_text: str = "",
    mask: re.Pattern | None = None,
    still_path: Path | None = None,
) -> Report:
    """Run every check on ``output`` and, if asked, save the preview still."""
    report = Report()
    export, safe = look.export, look.safe_box
    info = ffmpeg.probe(output)

    in_range = export.min_seconds - 0.05 <= info.duration <= export.max_seconds + 0.05
    report.add(
        "duration",
        in_range,
        f"{info.duration:.1f} s (Shorts need {export.min_seconds}-{export.max_seconds} s)",
    )

    outside = [b for b in overlay.boxes if not b.inside(safe)]
    detail = "all text inside the safe box"
    if outside:
        worst = outside[0]
        detail = (
            f"{len(outside)} text box(es) outside the safe box, e.g. {worst.name} at "
            f"x {worst.x0:.0f}-{worst.x1:.0f}, y {worst.y0:.0f}-{worst.y1:.0f}"
        )
    report.add("safe_box", not outside, detail)

    if headline:
        fits = overlay.headline_fits and overlay.headline_lines <= look.headline.max_lines
        report.add(
            "headline_fit",
            fits,
            f"{overlay.headline_lines} line(s) at {overlay.headline_size:.0f} px"
            if fits
            else f"does not fit in {look.headline.max_lines} lines at {look.headline.min_size} px",
        )

    if headline:
        report.add(*_emoji_check(overlay, headline, look))

    if plan.layout == SPLIT:
        report.add(*_face_check(output, look))

    measured = ffmpeg.measure_loudness(output)
    if measured is None:
        report.add("loudness", True, "no audible sound to measure")
    else:
        level = float(measured["input_i"])
        ok = abs(level - export.loudness) <= LOUDNESS_TOLERANCE
        report.add("loudness", ok, f"{level:.1f} LUFS (target {export.loudness} +/- 2)")

    black, frozen = ffmpeg.find_black_and_frozen(output, plan.video_area)
    report.add(
        "black_or_frozen",
        not black and not frozen,
        "no black or frozen stretch over 1 s"
        if not black and not frozen
        else f"black: {[round(b, 1) for b in black]} s, frozen: {[round(f, 1) for f in frozen]} s",
    )

    minimum = look.caption.min_seconds
    short = [b for b in overlay.blocks if b.end - b.start < minimum - 0.005]
    report.add(
        "caption_time",
        not short,
        f"{len(overlay.blocks)} caption blocks, all at least {minimum} s"
        if not short
        else f"{len(short)} of {len(overlay.blocks)} caption blocks are shorter than {minimum} s",
    )

    captions = " ".join(w.text for b in overlay.blocks for w in b.words)
    bad = blocked_words(headline or "", mask, transcript_text) + (
        [m.group(0) for m in mask.finditer(captions)] if mask else []
    )
    report.add(
        "blocklist", not bad, "no blocked words" if not bad else f"blocked: {sorted(set(bad))}"
    )

    if still_path is not None:
        try:
            save_preview_still(
                output, still_path, look, min(export.cover_second, info.duration / 2)
            )
            report.still = str(still_path)
            report.add("preview_still", True, still_path.name)
        except (ffmpeg.RenderError, OSError, ImportError) as exc:
            report.add("preview_still", False, f"could not save the preview: {exc}")
    return report


def _emoji_check(overlay: Overlay, headline: str, look: Look) -> tuple[str, bool, str]:
    """Hook emoji: every emoji has a picture, no letter of the hook would show as a blank box, nothing leaves the safe box."""
    font = get_font(look.headline.font)
    blank = sorted({ch for ch in headline if not font.has_glyph(ch)})
    pictures = [p for p in overlay.emojis]
    missing = [p.char for p in pictures if not emoji_mod.has_asset(p.char)]
    outside = [p.char for p in pictures
               if not (look.safe_box.x0 <= p.x and p.x + p.side <= look.safe_box.x1
                       and look.safe_box.y0 <= p.y and p.y + p.side <= look.safe_box.y1)]
    problems = []
    if blank:
        problems.append(f"the hook has characters the font would draw as blank boxes: {blank}")
    if missing:
        problems.append(f"no picture for emoji {missing}")
    if outside:
        problems.append(f"emoji outside the safe box: {outside}")
    if problems:
        return "hook_emoji", False, "; ".join(problems)
    return "hook_emoji", True, f"{len(pictures)} emoji picture(s), all inside the safe box" if pictures else "no emoji, no blank boxes"


def _face_check(output: Path, look: Look) -> tuple[str, bool, str]:
    """Split layout: the face must be visible inside the safe box on three frames."""
    from clipvault.render import facecam

    safe, seam = look.safe_box, look.split.seam_y
    try:
        frames = facecam.load_frames(output, 3)
        hits = 0
        for frame in frames:
            centres = [(x + w / 2, y + h / 2) for x, y, w, h in facecam.detect_faces(frame)]
            hits += any(safe.x0 <= cx <= safe.x1 and seam <= cy <= safe.y1 for cx, cy in centres)
    except facecam.FacecamUnavailable as exc:
        return "face_position", False, f"face detection is not installed: {exc}"
    ok = bool(frames) and hits == len(frames)
    return "face_position", ok, f"face inside the safe box on {hits} of {len(frames)} frames"


def save_preview_still(output: Path, dest: Path, look: Look, second: float) -> Path:
    """Save a frame with the TikTok and Shorts buttons' areas drawn over it.

    Red is where TikTok's interface sits, blue is YouTube Shorts', and the green
    outline is the safe box that all burned-in text must stay inside.
    """
    from PIL import Image, ImageDraw

    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = dest.with_suffix(".frame.png")
    ffmpeg.extract_frame(output, second, raw)
    try:
        with Image.open(raw) as source:
            image = source.convert("RGBA")
        width, height = image.size
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        zones = (("tiktok", (255, 40, 80, 70)), ("shorts", (40, 120, 255, 60)))
        for name, colour in zones:
            zone = look.ui_overlays[name]
            draw.rectangle([0, height - zone["bottom"], width, height], fill=colour)
            draw.rectangle([width - zone["right"], 0, width, height - zone["bottom"]], fill=colour)
        safe = look.safe_box
        draw.rectangle([safe.x0, safe.y0, safe.x1, safe.y1], outline=(60, 255, 120, 255), width=4)
        Image.alpha_composite(image, layer).convert("RGB").save(dest, quality=88)
    finally:
        raw.unlink(missing_ok=True)
    return dest
