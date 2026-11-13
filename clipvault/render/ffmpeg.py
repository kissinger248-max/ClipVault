"""Everything that runs FFmpeg or ffprobe for rendering and QA (SPEC §10.6)."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from clipvault import procs
from clipvault.render.template import Look

TIMEOUT_S = 900


class RenderError(Exception):
    """FFmpeg failed or produced something unusable."""


def _run(*args: Any, **kwargs: Any) -> Any:
    """The one place this module starts a process (tests replace it).

    A timeout ends the whole tree the tool started (``procs.run``).
    """
    return procs.run(*args, **kwargs)


def _ffmpeg(arguments: list[str], cwd: Path | None = None, what: str = "FFmpeg") -> str:
    """Run FFmpeg and return what it wrote to stderr; raise ``RenderError`` on failure."""
    command = ["ffmpeg", "-nostdin", "-hide_banner", *arguments]
    try:
        result = _run(
            command,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            cwd=str(cwd) if cwd else None,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"{what} took longer than {TIMEOUT_S} s and was stopped") from exc
    except OSError as exc:
        raise RenderError(f"could not start FFmpeg: {exc}") from exc
    if result.returncode != 0:
        raise RenderError(f"{what} failed: {(result.stderr or '').strip()[-400:]}")
    return result.stderr or ""


@dataclass(frozen=True)
class MediaInfo:
    """What ffprobe says about a video file."""

    width: int
    height: int
    fps: float
    duration: float
    has_audio: bool
    codec: str


def probe(path: Path) -> MediaInfo:
    """Read size, frame rate, duration and codec of a video file."""
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,codec_name,width,height,r_frame_rate:format=duration",
        "-of", "json", str(path),
    ]  # fmt: skip
    try:
        result = _run(command, capture_output=True, text=True, timeout=60)
        data = json.loads(result.stdout or "{}")
        video = next(s for s in data["streams"] if s["codec_type"] == "video")
        return MediaInfo(
            width=int(video["width"]),
            height=int(video["height"]),
            fps=float(Fraction(video.get("r_frame_rate") or "30/1")),
            duration=float(data["format"]["duration"]),
            has_audio=any(s["codec_type"] == "audio" for s in data["streams"]),
            codec=video.get("codec_name", ""),
        )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration) as exc:
        raise RenderError(f"could not read {path.name}: {exc}") from exc


# --- loudness ------------------------------------------------------------------


def measure_loudness(path: Path, start: float = 0.0, duration: float | None = None) -> dict | None:
    """First loudnorm pass: measure the audio. None when there is no usable audio."""
    arguments = ["-ss", f"{start:.3f}"] if start > 0 else []
    arguments += ["-i", str(path)]
    if duration:
        arguments += ["-t", f"{duration:.3f}"]
    arguments += [
        "-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", "-",
    ]  # fmt: skip
    try:
        stderr = _ffmpeg(arguments, what="loudness measurement")
    except RenderError:
        return None
    found = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, flags=re.S)
    if not found:
        return None
    values = json.loads(found[-1])
    try:
        if float(values["input_i"]) < -69:  # silence
            return None
    except ValueError:
        return None  # "-inf"
    return values


def loudnorm_filter(measured: dict | None, look: Look) -> str:
    """Second loudnorm pass built from the measurement (or a plain resample for silence)."""
    if not measured:
        return "aresample=48000"
    target = look.export
    return (
        f"loudnorm=I={target.loudness}:TP={target.true_peak - 0.5}:LRA=11"
        f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}:linear=true,aresample=48000"
    )


# --- encode --------------------------------------------------------------------


def encode(
    *,
    source: Path,
    dest: Path,
    workdir: Path,
    video_filter: str,
    audio_filter: str,
    has_audio: bool,
    start: float,
    duration: float,
    look: Look,
) -> None:
    """Render the vertical video. ``workdir`` holds ``overlay.ass`` and ``fonts/``.

    FFmpeg runs inside ``workdir`` and is given plain relative names for the
    subtitle file and font folder: Windows paths (drive colons, backslashes)
    are a classic way to break the ``subtitles`` filter.
    """
    export = look.export
    arguments = ["-y", "-loglevel", "error"]
    if start > 0:
        arguments += ["-ss", f"{start:.3f}"]
    arguments += ["-i", str(source)]
    if not has_audio:
        arguments += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    graph = (
        f"[0:v]{video_filter}[base];[base]subtitles=overlay.ass:fontsdir=fonts,"
        "setsar=1,format=yuv420p[v]"  # square pixels: scaling 1280x720 to 1080x608 leaves 1216:1215
    )
    arguments += ["-filter_complex", graph, "-map", "[v]"]
    arguments += ["-map", "0:a:0", "-af", audio_filter] if has_audio else ["-map", "1:a:0"]
    arguments += [
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-b:v", str(export.video_bitrate), "-maxrate", str(export.max_bitrate), "-bufsize", "20M",
        "-c:a", "aac", "-ar", "48000", "-b:a", str(export.audio_bitrate),
        "-movflags", "+faststart", str(dest),
    ]  # fmt: skip
    _ffmpeg(arguments, cwd=workdir, what="rendering")
    if not dest.is_file() or dest.stat().st_size == 0:
        raise RenderError("FFmpeg reported success but the video is missing or empty")


def extract_frame(path: Path, second: float, dest: Path) -> Path:
    """Save one frame of a video as an image."""
    _ffmpeg(
        [
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{second:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            str(dest),
        ],
        what="frame extraction",
    )
    if not dest.is_file():
        raise RenderError(f"no frame at {second:.1f} s in {path.name}")
    return dest


def find_black_and_frozen(
    path: Path, crop: tuple[int, int, int, int] | None = None, min_seconds: float = 1.0
) -> tuple[list[float], list[float]]:
    """Return the lengths of black and of frozen stretches longer than ``min_seconds``.

    ``crop`` (w, h, x, y) limits the check to the part of the frame holding the
    video, so letterbox bars and the static headline don't count.
    """
    chain = f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}," if crop else ""
    chain += f"blackdetect=d={min_seconds}:pix_th=0.08,freezedetect=n=-60dB:d={min_seconds}"
    stderr = _ffmpeg(
        ["-i", str(path), "-vf", chain, "-an", "-f", "null", "-"], what="black/freeze check"
    )
    black = [float(v) for v in re.findall(r"black_duration:([\d.]+)", stderr)]
    frozen = [float(v) for v in re.findall(r"freeze_duration: ([\d.]+)", stderr)]
    return black, frozen
