"""Download the tools the installed app bundles into packaging/tools/<platform>/.

- FFmpeg + ffprobe (GPL). Windows: BtbN's shared GPL build of the newest release
  branch (github.com/BtbN/FFmpeg-Builds). macOS (Apple silicon): Martin Riedl's
  static builds (ffmpeg.martin-riedl.de). Both include libass, which the captions
  need; this script checks for the ``subtitles`` filter and stops if it is missing.
- TwitchDownloaderCLI (MIT, github.com/lay295/TwitchDownloader), for chat replays.
- yt-dlp's official standalone program (Unlicense, github.com/yt-dlp/yt-dlp). The
  installed app copies it to its data folder and keeps it current with ``yt-dlp -U``.

Each tool's licence is copied next to it; the installer ships them.
Set GITHUB_TOKEN to avoid GitHub's limit on anonymous API calls (CI does).
"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
TIMEOUT = httpx.Timeout(60.0, read=300.0)
FFMPEG_NOTICE = """FFmpeg and ffprobe are included under the GNU General Public License (GPL).
Source code: https://ffmpeg.org/download.html (build: {source}).
They are run as separate programs; ClipVault's own code is not part of FFmpeg.
"""


def platform_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    raise SystemExit("The installed app is built for Windows and macOS only.")


def _client() -> httpx.Client:
    headers = {"User-Agent": "clipvault-build"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=headers)


def _release_asset(http: httpx.Client, api_url: str, pattern: str) -> tuple[str, str]:
    """(name, download URL) of the asset whose name matches; the highest version wins."""
    release = http.get(api_url)
    release.raise_for_status()
    found = [(a["name"], a["browser_download_url"]) for a in release.json()["assets"]
             if re.fullmatch(pattern, a["name"], flags=re.IGNORECASE)]  # fmt: skip
    if not found:
        raise SystemExit(f"No download matching {pattern!r} at {api_url}")

    def version(item: tuple[str, str]) -> tuple[int, ...]:
        return tuple(int(n) for n in re.findall(r"\d+", item[0]))

    return max(found, key=version)


def _download(http: httpx.Client, url: str) -> bytes:
    print(f"  downloading {url}")
    response = http.get(url)
    response.raise_for_status()
    return response.content


def _make_runnable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def ffmpeg_windows(http: httpx.Client, out: Path) -> None:
    api = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest"
    name, url = _release_asset(http, api, r"ffmpeg-n\d+\.\d+-latest-win64-gpl-shared-\d+\.\d+\.zip")
    with zipfile.ZipFile(io.BytesIO(_download(http, url))) as archive:
        for member in archive.namelist():
            filename = member.rsplit("/", 1)[-1]
            keep = (
                ("/bin/" in member and filename.endswith((".exe", ".dll"))
                 and filename != "ffplay.exe")
                or filename == "LICENSE.txt"
            )  # fmt: skip
            if keep and filename:
                target = out / ("FFMPEG-LICENSE.txt" if filename == "LICENSE.txt" else filename)
                target.write_bytes(archive.read(member))
    (out / "FFMPEG-NOTICE.txt").write_text(FFMPEG_NOTICE.format(source=name), encoding="utf-8")


def ffmpeg_macos(http: httpx.Client, out: Path) -> None:
    base = "https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release"
    for tool in ("ffmpeg", "ffprobe"):
        with zipfile.ZipFile(io.BytesIO(_download(http, f"{base}/{tool}.zip"))) as archive:
            member = next(m for m in archive.namelist() if m.rsplit("/", 1)[-1] == tool)
            target = out / tool
            target.write_bytes(archive.read(member))
            _make_runnable(target)
    (out / "FFMPEG-NOTICE.txt").write_text(FFMPEG_NOTICE.format(source=base), encoding="utf-8")


def twitch_downloader(http: httpx.Client, out: Path, plat: str) -> None:
    api = "https://api.github.com/repos/lay295/TwitchDownloader/releases/latest"
    pattern = (r"TwitchDownloaderCLI-[\d.]+-Windows-x64\.zip" if plat == "windows"
               else r"TwitchDownloaderCLI-[\d.]+-MacOS-?Arm64\.zip")  # fmt: skip
    _name, url = _release_asset(http, api, pattern)
    with zipfile.ZipFile(io.BytesIO(_download(http, url))) as archive:
        for member in archive.namelist():
            filename = member.rsplit("/", 1)[-1]
            if filename.startswith("TwitchDownloaderCLI"):
                target = out / filename
                target.write_bytes(archive.read(member))
                _make_runnable(target)
            elif filename.upper().startswith(("COPYING", "COPYRIGHT", "LICENSE", "THIRD-PARTY")):
                (out / f"TwitchDownloader-{filename}").write_bytes(archive.read(member))


def ytdlp_standalone(http: httpx.Client, out: Path, plat: str) -> None:
    base = "https://github.com/yt-dlp/yt-dlp/releases/latest/download"
    source = "yt-dlp.exe" if plat == "windows" else "yt-dlp_macos"
    target = out / ("yt-dlp.exe" if plat == "windows" else "yt-dlp")
    target.write_bytes(_download(http, f"{base}/{source}"))
    _make_runnable(target)
    (out / "YT-DLP-LICENSE.txt").write_text(
        "yt-dlp is released into the public domain (The Unlicense).\n"
        "https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE\n",
        encoding="utf-8",
    )


def check(out: Path, plat: str) -> None:
    """Stop the build if FFmpeg can't burn in captions or a tool is missing."""
    exe = ".exe" if plat == "windows" else ""
    for tool in (f"ffmpeg{exe}", f"ffprobe{exe}", f"TwitchDownloaderCLI{exe}", f"yt-dlp{exe}"):
        if not (out / tool).exists():
            raise SystemExit(f"{tool} is missing from {out}")
    filters = subprocess.run([str(out / f"ffmpeg{exe}"), "-hide_banner", "-filters"],
                             capture_output=True, text=True, check=True).stdout  # fmt: skip
    if not re.search(r"\ssubtitles\s", filters):
        raise SystemExit("This FFmpeg build has no 'subtitles' filter (libass); captions need it.")
    version = subprocess.run([str(out / f"yt-dlp{exe}"), "--version"], capture_output=True,
                             text=True, check=True, timeout=120).stdout.strip()  # fmt: skip
    print(f"  yt-dlp {version}")
    print(f"  tools ready in {out}: FFmpeg with captions, ffprobe, TwitchDownloaderCLI, yt-dlp")


def main() -> int:
    plat = platform_name()
    out = HERE / "tools" / plat
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    with _client() as http:
        print("FFmpeg:")
        (ffmpeg_windows if plat == "windows" else ffmpeg_macos)(http, out)
        print("TwitchDownloaderCLI:")
        twitch_downloader(http, out, plat)
        print("yt-dlp:")
        ytdlp_standalone(http, out, plat)
    check(out, plat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
