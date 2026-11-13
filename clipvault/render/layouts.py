"""The three vertical layouts and their geometry (SPEC §10.2 and §10.3).

Each layout function returns an FFmpeg filter chain that turns the source video
into a 1080x1920 frame, plus the part of that frame that holds moving video.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from clipvault.render.ffmpeg import MediaInfo
from clipvault.render.template import Look

LETTERBOX, SPLIT, NATIVE = "letterbox", "split", "native"
LAYOUTS = (LETTERBOX, SPLIT, NATIVE)


@dataclass(frozen=True)
class Rect:
    """A rectangle in source-video pixels."""

    x: int
    y: int
    w: int
    h: int

    @classmethod
    def from_json(cls, text: str | None) -> Rect | None:
        if not text:
            return None
        try:
            x, y, w, h = (int(v) for v in json.loads(text))
        except (ValueError, TypeError):
            return None
        return cls(x, y, w, h) if w > 0 and h > 0 else None

    def to_json(self) -> str:
        return json.dumps([self.x, self.y, self.w, self.h])

    @property
    def centre(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x <= self.x + self.w and self.y <= y <= self.y + self.h


@dataclass(frozen=True)
class Plan:
    """How to build the 1080x1920 picture for one render."""

    layout: str
    video_filter: str
    video_area: tuple[int, int, int, int]  # w, h, x, y of the moving picture, for QA


def _even(value: float) -> int:
    return max(int(round(value / 2)) * 2, 2)


def choose_layout(
    *,
    override: str | None,
    has_portrait: bool,
    cam: Rect | None,
    face_in_cam: bool,
    look: Look,
) -> str:
    """Pick the layout: an explicit choice wins, then native, split, letterbox."""
    if override in LAYOUTS:
        if override == SPLIT and cam is None:
            return LETTERBOX
        if override == NATIVE and not has_portrait:
            return LETTERBOX
        return override
    if has_portrait:
        return NATIVE
    if cam is not None and cam.w >= look.split.min_cam_width and face_in_cam:
        return SPLIT
    return LETTERBOX


def letterbox(info: MediaInfo, look: Look) -> Plan:
    """Whole frame scaled to the full width, in the upper middle, on black or a blurred copy."""
    width, height = look.frame.width, look.frame.height
    scaled_h = _even(width * info.height / info.width)
    standard_h = _even(width * 9 / 16)
    top = max(int(look.letterbox.video_top + (standard_h - scaled_h) / 2), 0)
    top = min(top, height - scaled_h)
    front = f"scale={width}:{scaled_h}:flags=lanczos"
    if look.letterbox.background == "blur":
        chain = (
            f"split[lb_a][lb_b];[lb_a]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=28:4,eq=brightness=-0.18[lb_bg];"
            f"[lb_b]{front}[lb_fg];[lb_bg][lb_fg]overlay=0:{top}"
        )
    else:
        chain = f"{front},pad={width}:{height}:0:{top}:black"
    return Plan(LETTERBOX, chain, (width, scaled_h, 0, top))


def cam_crop(cam: Rect, face: tuple[float, float] | None, info: MediaInfo, look: Look) -> Rect:
    """The 9:7 piece of the source that fills the bottom panel.

    The facecam is widened to the panel's shape and shifted so the face lands
    near ``split.face_y`` on the finished frame, without leaving the source.
    """
    panel_w = look.frame.width
    panel_h = look.frame.height - look.split.seam_y
    aspect = panel_w / panel_h
    width = min(max(cam.w, cam.h * aspect), info.width)
    height = width / aspect
    if height > info.height:
        height = info.height
        width = height * aspect
    face_x, face_y = face if face else cam.centre
    wanted_in_panel = (look.split.face_y - look.split.seam_y) / panel_h
    x = min(max(face_x - width / 2, 0), info.width - width)
    y = min(max(face_y - wanted_in_panel * height, 0), info.height - height)
    return Rect(int(x), int(y), _even(width), _even(height))


def split(
    info: MediaInfo, cam: Rect, content: Rect | None, face: tuple[float, float] | None, look: Look
) -> Plan:
    """Gameplay square on top, enlarged facecam underneath."""
    width, height, seam = look.frame.width, look.frame.height, look.split.seam_y
    side = _even(min(info.width, info.height) - 1)
    if content is not None:
        side = _even(min(content.w, content.h, side) - 1)
        centre_x, centre_y = content.centre
    else:
        centre_x, centre_y = info.width / 2, info.height / 2
    top_x = int(min(max(centre_x - side / 2, 0), info.width - side))
    top_y = int(min(max(centre_y - side / 2, 0), info.height - side))
    crop = cam_crop(cam, face, info, look)
    chain = (
        f"split[sp_a][sp_b];"
        f"[sp_a]crop={side}:{side}:{top_x}:{top_y},scale={width}:{seam}:flags=lanczos[sp_top];"
        f"[sp_b]crop={crop.w}:{crop.h}:{crop.x}:{crop.y},"
        f"scale={width}:{height - seam}:flags=lanczos,unsharp=5:5:0.6[sp_cam];"
        f"[sp_top][sp_cam]vstack"
    )
    return Plan(SPLIT, chain, (width, height, 0, 0))


def native(look: Look) -> Plan:
    """Twitch's own vertical version, scaled to fill the frame."""
    width, height = look.frame.width, look.frame.height
    chain = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={width}:{height}"
    )
    return Plan(NATIVE, chain, (width, height, 0, 0))
