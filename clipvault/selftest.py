"""``clipvault selftest``: checks that a build has everything it needs, without the internet.

Used after building the installed app (packaging/build.py, and the Windows and Mac
builds on GitHub) and handy for support: it names the first thing that is missing.
"""

from __future__ import annotations

import importlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from clipvault import config as cfg

Check = tuple[str, Callable[[], str]]


def _tools() -> str:
    found = {}
    for tool in ("ffmpeg", "ffprobe", "TwitchDownloaderCLI"):
        path = shutil.which(tool)
        if not path:
            raise RuntimeError(f"{tool} not found on PATH")
        found[tool] = path
    filters = subprocess.run([found["ffmpeg"], "-hide_banner", "-filters"], capture_output=True,
                             text=True, check=True, timeout=60).stdout  # fmt: skip
    if not re.search(r"\ssubtitles\s", filters):
        raise RuntimeError("FFmpeg has no 'subtitles' filter, so captions can't be drawn")
    return f"FFmpeg ({found['ffmpeg']}) with captions, ffprobe, TwitchDownloaderCLI"


def _libraries() -> str:
    names = [
        "cv2",
        "numpy",
        "PIL",
        "faster_whisper",
        "ctranslate2",
        "onnxruntime",
        "yt_dlp",
        "uvicorn",
        "fastapi",
        "jinja2",
        "keyring",
        "httpx",
        "websockets",
        "tzlocal",
    ]
    if sys.platform in ("win32", "darwin"):
        names.append("pystray")
    for name in names:
        importlib.import_module(name)
    return f"{len(names)} libraries import"


def _whisper_assets() -> str:
    import faster_whisper

    folder = Path(faster_whisper.__file__).parent / "assets"
    models = list(folder.glob("*.onnx"))
    if not models:
        raise RuntimeError(f"no voice-activity model in {folder}")
    return models[0].name


def _face_model() -> str:
    import cv2
    import numpy as np

    from clipvault.render import facecam

    faces = facecam.detect_faces(np.zeros((360, 640, 3), dtype=np.uint8))
    return f"YuNet loads with OpenCV {cv2.__version__} ({len(faces)} faces in a blank frame)"


def _fonts() -> str:
    from clipvault.render.template import FONTS_DIR

    fonts = sorted(p.name for p in FONTS_DIR.glob("*.ttf"))
    if not any(name.startswith("Montserrat") for name in fonts):
        raise RuntimeError(f"Montserrat missing from {FONTS_DIR}")
    return ", ".join(fonts)


CAPTION_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 540
PlayResY: 960

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, \
Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, \
Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Montserrat,48,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,\
0,0,1,4,0,5,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,SELF TEST
"""


def _caption_render() -> str:
    """Burn a caption into a short test video, the way phone versions are made."""
    import shutil as sh

    from clipvault.render.template import FONTS_DIR

    ffmpeg = sh.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        sh.copytree(FONTS_DIR, work / "fonts", ignore=sh.ignore_patterns("*.txt", ".*"))
        (work / "overlay.ass").write_text(CAPTION_ASS, encoding="utf-8")
        command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-f", "lavfi", "-i",
                   "color=c=0x202020:s=540x960:d=1:r=30",
                   "-vf", "subtitles=overlay.ass:fontsdir=fonts", "-c:v", "libx264",
                   "-pix_fmt", "yuv420p", "out.mp4"]  # fmt: skip
        result = subprocess.run(command, cwd=work, capture_output=True, text=True, timeout=120)
        out = work / "out.mp4"
        if result.returncode != 0 or not out.exists() or out.stat().st_size < 1000:
            raise RuntimeError("FFmpeg could not burn in a caption: " + result.stderr[-300:])
        import cv2  # the face finder reads clips this way

        video = cv2.VideoCapture(str(out))
        try:
            ok, frame = video.read()
        finally:
            video.release()
        if not ok or frame is None:
            raise RuntimeError("OpenCV could not read the test video back")
        return f"test video with a caption made and read back ({out.stat().st_size:,} bytes)"


def _panel_files() -> str:
    from clipvault.web import server

    needed = [server.HERE / "templates" / "base.html", server.HERE / "static" / "tokens.css",
              server.HERE / "static" / "fonts" / "Inter-latin.woff2"]  # fmt: skip
    missing = [str(p) for p in needed if not p.exists()]
    if missing:
        raise RuntimeError("missing: " + ", ".join(missing))
    return "templates, styles and fonts present"


def _database() -> str:
    from clipvault import db

    with tempfile.TemporaryDirectory() as folder:
        conn = db.open_db(Path(folder) / "check.db")
        try:
            tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'")
            count = tables.fetchone()[0]
        finally:
            conn.close()
    return f"migrations apply ({count} tables)"


def _twitch_app() -> str:
    bundled = cfg.bundled_twitch_client_id()
    return "Sign in with Twitch available" if bundled else "no built-in Twitch app (keys needed)"


CHECKS: list[Check] = [
    ("Tools", _tools),
    ("Libraries", _libraries),
    ("Speech model files", _whisper_assets),
    ("Face model", _face_model),
    ("Caption fonts", _fonts),
    ("Caption drawing", _caption_render),
    ("Control panel files", _panel_files),
    ("Database", _database),
    ("Twitch", _twitch_app),
]


def main() -> int:
    """Run every check, print one line each, and return 1 if any failed."""
    failed = 0
    for name, check in CHECKS:
        try:
            print(f"[ OK ] {name}: {check()}")
        except Exception as exc:  # report every failure, then fail at the end
            failed += 1
            print(f"[FAIL] {name}: {exc}")
    print("selftest passed" if not failed else f"selftest FAILED ({failed} problem(s))")
    return 1 if failed else 0
