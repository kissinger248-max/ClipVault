"""Gather the licence texts of everything the installed app ships, for its LICENSES folder.

Used by clipvault.spec: every Python package ClipVault depends on (``clipvault[app]``,
followed through each package's own requirements), plus the bundled fonts and tools.
Each package's licence files from its metadata go to ``LICENSES/<package>/``.
"""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

LICENCE_NAME = re.compile(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS|COPYRIGHT)", re.IGNORECASE)


def _requirement_names(dist: metadata.Distribution, extras: set[str]) -> list[str]:
    names = []
    for line in dist.requires or []:
        name, _, marker = line.partition(";")
        marker = marker.strip()
        if marker and "extra" in marker:
            if not any(f'extra == "{extra}"' in marker for extra in extras):
                continue
        elif marker:
            try:
                from packaging.markers import Marker

                if not Marker(marker).evaluate():
                    continue
            except Exception:  # unknown marker syntax: include, licences are harmless
                pass
        names.append(re.split(r"[\s\[<>=!~(]", name.strip(), maxsplit=1)[0])
    return names


def shipped_distributions() -> list[metadata.Distribution]:
    """clipvault and everything it needs at run time, each once."""
    seen: dict[str, metadata.Distribution] = {}
    queue: list[tuple[str, set[str]]] = [("clipvault", {"app", "render", "transcribe", "desktop"})]
    while queue:
        name, extras = queue.pop()
        key = name.lower().replace("_", "-")
        if key in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        seen[key] = dist
        for requirement in _requirement_names(dist, extras):
            queue.append((requirement, set()))
    return [dist for key, dist in sorted(seen.items()) if key != "clipvault"]


def licence_datas(root: Path, tools: Path) -> list[tuple[str, str]]:
    """(source file, folder in the app) pairs for PyInstaller's ``datas``."""
    datas: list[tuple[str, str]] = []
    for dist in shipped_distributions():
        name = dist.metadata["Name"]
        for file in dist.files or []:
            if LICENCE_NAME.search(file.name) and ".dist-info" in str(file):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    datas.append((str(source), f"LICENSES/{name}"))
    import sys

    python_licence = Path(sys.base_prefix) / "LICENSE.txt"
    if python_licence.exists():
        datas.append((str(python_licence), "LICENSES/Python"))
    for font_licence in (root / "assets" / "fonts").glob("*.txt"):
        datas.append((str(font_licence), "LICENSES/fonts"))
    for emoji_licence in (root / "clipvault" / "data" / "emoji").glob("*.txt"):
        datas.append((str(emoji_licence), "LICENSES/emoji"))  # Twemoji graphics, CC-BY 4.0 (attribution required)
    inter = root / "clipvault" / "web" / "static" / "fonts" / "OFL-Inter.txt"
    if inter.exists():
        datas.append((str(inter), "LICENSES/fonts"))
    for tool_licence in tools.glob("*"):
        if tool_licence.suffix == ".txt":
            datas.append((str(tool_licence), "LICENSES/tools"))
    return datas


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    for source, folder in licence_datas(here.parent, here / "tools" / "windows"):
        print(f"{folder:40} {Path(source).name}")
