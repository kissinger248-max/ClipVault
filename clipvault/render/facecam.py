"""Finding a streamer's facecam with OpenCV's YuNet face detector (SPEC §10.4).

Detection runs once per streamer and is stored; each clip then only gets a quick
check that the face is still where the stored rectangle says.
"""

from __future__ import annotations

import logging
import os
import statistics
import tempfile
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from clipvault.logging_setup import ctx
from clipvault.render import ffmpeg
from clipvault.render.layouts import Rect

MIN_CONFIDENCE = 0.8
FRAMES_PER_CLIP = 5
CHECK_FRAMES = 3
BIG_FACE_SHARE = 0.22  # a face this wide (share of the frame) is a full-screen cam scene
_MODEL = "face_detection_yunet_2023mar.onnx"

Face = tuple[float, float, float, float]  # x, y, w, h
# OpenCV prints a warning for every detector it creates; keep the console clean.
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
log = logging.getLogger(__name__)


_detectors: dict[tuple[int, int], Any] = {}


class FacecamUnavailable(Exception):
    """OpenCV is not installed (the ``render`` extra), so faces can't be detected."""


@lru_cache(maxsize=1)
def _model_bytes() -> bytes:
    return resources.files("clipvault.data").joinpath(_MODEL).read_bytes()


def detect_faces(image: Any) -> list[Face]:
    """Return confident face boxes in a BGR image, largest first."""
    try:
        import cv2
    except ImportError as exc:
        raise FacecamUnavailable('install with: pip install -e ".[render]"') from exc
    height, width = image.shape[:2]
    detector = _detectors.get((width, height))
    if detector is None:
        detector = cv2.FaceDetectorYN.create(
            "onnx", _model_bytes(), b"", (width, height), MIN_CONFIDENCE, 0.3, 50
        )
        _detectors[(width, height)] = detector
    _count, found = detector.detect(image)
    faces = [tuple(float(v) for v in row[:4]) for row in (found if found is not None else [])]
    return sorted(faces, key=lambda f: -f[2] * f[3])


def load_frames(video: Path, count: int) -> list[Any]:
    """Read ``count`` frames spread evenly through a video (skipping the very ends)."""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise FacecamUnavailable('install with: pip install -e ".[render]"') from exc
    duration = ffmpeg.probe(video).duration
    frames = []
    with tempfile.TemporaryDirectory(prefix="clipvault-frames-") as folder:
        for index in range(count):
            second = duration * (index + 1) / (count + 1)
            target = Path(folder) / f"{index}.png"
            try:
                ffmpeg.extract_frame(video, second, target)
            except ffmpeg.RenderError:
                continue
            image = cv2.imdecode(np.frombuffer(target.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                frames.append(image)
    return frames


def is_big_centred(face: Face, width: int, height: int) -> bool:
    """True for one large face in the middle: a full-screen "just chatting" scene."""
    x, y, w, h = face
    centre_x, centre_y = (x + w / 2) / width, (y + h / 2) / height
    return w / width >= BIG_FACE_SHARE and 0.3 <= centre_x <= 0.7 and 0.2 <= centre_y <= 0.8


def find_cam_rect(frames: list[Any], faces_per_frame: list[list[Face]]) -> Rect | None:
    """Work out the facecam rectangle from faces seen across many frames.

    The cam is accepted when a face of similar size sits in the same off-centre
    spot in most frames. The rectangle is then grown from the face to the
    overlay's border, found as edges that stay put from frame to frame.
    """
    if not frames:
        return None
    height, width = frames[0].shape[:2]
    small = [
        faces[0]
        for faces in faces_per_frame
        if faces and not is_big_centred(faces[0], width, height)
    ]
    if len(small) < max(3, 0.6 * len(frames)):
        return None
    centre_x = statistics.median(f[0] + f[2] / 2 for f in small)
    centre_y = statistics.median(f[1] + f[3] / 2 for f in small)
    steady = [
        f
        for f in small
        if abs(f[0] + f[2] / 2 - centre_x) < 0.1 * width
        and abs(f[1] + f[3] / 2 - centre_y) < 0.1 * height
    ]
    if len(steady) < 0.6 * len(frames):
        return None
    if 0.35 < centre_x / width < 0.65 and 0.35 < centre_y / height < 0.65:
        return None  # dead centre: not a corner overlay
    face_w = statistics.median(f[2] for f in steady)
    face_h = statistics.median(f[3] for f in steady)
    face = (centre_x - face_w / 2, centre_y - face_h / 2, face_w, face_h)
    return _grow_to_border(frames, face, width, height)


def _grow_to_border(frames: list[Any], face: Face, width: int, height: int) -> Rect:
    """Expand a face box to the camera overlay's edges (or a sensible default size)."""
    import cv2
    import numpy as np

    x, y, w, h = face
    grey = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames]
    # Edges present in every frame belong to the overlay, not to the moving picture.
    vertical = np.min([np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)) for g in grey], axis=0)
    horizontal = np.min([np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)) for g in grey], axis=0)

    top_limit, bottom_limit = int(max(y - 2.0 * h, 0)), int(min(y + 4.0 * h, height))
    left_limit, right_limit = int(max(x - 3.5 * w, 0)), int(min(x + 4.5 * w, width))
    rows = slice(int(max(y - 0.5 * h, 0)), int(min(y + 1.5 * h, height)))
    cols = slice(int(max(x - 0.5 * w, 0)), int(min(x + 1.5 * w, width)))

    def strongest(profile: Any, start: int, stop: int, default: int) -> int:
        if stop <= start:
            return default
        segment = profile[start:stop]
        peak = int(np.argmax(segment))
        return start + peak if segment[peak] > 4 * (np.median(profile) + 1) else default

    column_strength = vertical[rows, :].mean(axis=0)
    row_strength = horizontal[:, cols].mean(axis=1)
    left = strongest(
        column_strength, left_limit, int(max(x - 0.2 * w, 0)), int(max(x - 1.2 * w, 0))
    )
    right = strongest(
        column_strength, int(min(x + 1.2 * w, width)), right_limit, int(min(x + 2.2 * w, width))
    )
    top = strongest(row_strength, top_limit, int(max(y - 0.2 * h, 0)), int(max(y - 0.9 * h, 0)))
    bottom = strongest(
        row_strength, int(min(y + 1.2 * h, height)), bottom_limit, int(min(y + 2.6 * h, height))
    )
    return Rect(left, top, max(right - left, 2), max(bottom - top, 2))


def detect_streamer_cam(videos: list[Path], login: str = "") -> Rect | None:
    """Run the full detection over a streamer's recent clips; None when there is no steady cam."""
    frames: list[Any] = []
    for video in videos:
        frames += load_frames(video, FRAMES_PER_CLIP)
    sizes = {frame.shape[:2] for frame in frames}
    if len(sizes) > 1:  # clips of different sizes can't be compared pixel for pixel
        wanted = max(sizes, key=lambda s: sum(1 for f in frames if f.shape[:2] == s))
        frames = [f for f in frames if f.shape[:2] == wanted]
    faces = [detect_faces(frame) for frame in frames]
    rect = find_cam_rect(frames, faces)
    log.info(
        "facecam detection finished",
        extra=ctx(streamer=login, job="facecam", frames=len(frames), cam=rect and rect.to_json()),
    )
    return rect


def check_clip(video: Path, cam: Rect | None) -> tuple[bool, tuple[float, float] | None]:
    """Is the face inside ``cam`` in this clip? Returns (usable for split, face centre).

    Not usable when the face is missing from the rectangle in most sampled
    frames, or when a single large face fills the middle of the picture.
    """
    if cam is None:
        return False, None
    frames = load_frames(video, CHECK_FRAMES)
    centres: list[tuple[float, float]] = []
    for frame in frames:
        height, width = frame.shape[:2]
        faces = detect_faces(frame)
        if faces and is_big_centred(faces[0], width, height):
            return False, None
        inside = [f for f in faces if cam.contains(f[0] + f[2] / 2, f[1] + f[3] / 2)]
        if inside:
            x, y, w, h = inside[0]
            centres.append((x + w / 2, y + h / 2))
    if len(centres) * 2 <= len(frames) or not centres:
        return False, None
    return True, (
        statistics.median(c[0] for c in centres),
        statistics.median(c[1] for c in centres),
    )
