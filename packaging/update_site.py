"""Fill the download page (site/index.html) from the latest GitHub release.

    python packaging/update_site.py [--macos-min 11.0] [--site-url https://example.vercel.app]

Reads the release with ``gh`` and fills the elements marked ``data-fill="..."``:
version, release date, file sizes, the SHA-256 checksums from SHA256SUMS.txt, and
the oldest macOS (``--macos-min``, from the build's macos-build-facts artifact).
``--site-url`` replaces the ``[SITE URL]`` placeholder in every page, the sitemap
and robots.txt once the site's address is known.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
REPO = "kissinger248-max/ClipVault"
FILES = {"windows": "ClipVault-Setup-Windows.exe", "mac": "ClipVault-macOS.dmg"}


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def fill(html: str, key: str, value: str) -> str:
    """Replace the text inside every element with ``data-fill="key"``."""
    pattern = re.compile(rf'(<(\w+)[^>]*\sdata-fill="{re.escape(key)}"[^>]*>)(.*?)(</\2>)', re.S)
    new, count = pattern.subn(lambda m: m.group(1) + value + m.group(4), html)
    if not count:
        raise SystemExit(f'no data-fill="{key}" in the page')
    return new


def megabytes(size: int) -> str:
    return f"{size / 1_000_000:,.0f} MB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--macos-min", help="oldest macOS the Mac build runs on, e.g. 11.0")
    parser.add_argument("--site-url", help="the site's address, e.g. https://clipvault.vercel.app")
    args = parser.parse_args()

    release = json.loads(gh("release", "view", "--repo", REPO, "--json",
                            "tagName,publishedAt,assets,url"))  # fmt: skip
    sizes = {a["name"]: a["size"] for a in release["assets"]}
    sums_text = gh("release", "download", release["tagName"], "--repo", REPO,
                   "--pattern", "SHA256SUMS.txt", "--output", "-")  # fmt: skip
    sums = {line.split()[1].lstrip("*"): line.split()[0] for line in sums_text.splitlines() if line}

    page = SITE / "index.html"
    html = page.read_text(encoding="utf-8")
    html = fill(html, "version", release["tagName"].lstrip("v"))
    published = datetime.fromisoformat(release["publishedAt"].replace("Z", "+00:00"))
    html = fill(html, "date", f"{published.day} {published:%B %Y}")
    for os_name, file in FILES.items():
        if file not in sizes or file not in sums:
            raise SystemExit(f"{file} is missing from release {release['tagName']}")
        html = fill(html, f"size-{os_name}", megabytes(sizes[file]))
        html = fill(html, f"sha-{os_name}", sums[file])
    if args.macos_min:
        html = fill(html, "macos-min", args.macos_min)
    html = re.sub(r'(data-fill-href="release" href=")[^"]*', rf"\g<1>{release['url']}", html)
    page.write_text(html, encoding="utf-8", newline="\n")
    print(f"filled site/index.html from {release['tagName']}")

    if args.site_url:
        url = args.site_url.rstrip("/")
        for path in [*SITE.glob("*.html"), SITE / "sitemap.xml", SITE / "robots.txt"]:
            text = path.read_text(encoding="utf-8")
            if "[SITE URL]" in text:
                path.write_text(text.replace("[SITE URL]", url), encoding="utf-8", newline="\n")
        print(f"site address set to {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
