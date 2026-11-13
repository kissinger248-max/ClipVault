"""The download site: release details fill in, links point at stable download names."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"


def _update_site():
    spec = importlib.util.spec_from_file_location(
        "update_site", ROOT / "packaging" / "update_site.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_page_has_every_spot_the_release_fills() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    for key in (
        "version",
        "date",
        "size-windows",
        "size-mac",
        "sha-windows",
        "sha-mac",
        "macos-min",
    ):
        assert f'data-fill="{key}"' in html, key
    assert 'data-fill-href="release"' in html


def test_filling_replaces_only_the_marked_text() -> None:
    fill = _update_site().fill
    html = '<p>Version <span data-fill="version">0.0.0</span> · <span>keep</span></p>'
    assert fill(html, "version", "0.2.0") == (
        '<p>Version <span data-fill="version">0.2.0</span> · <span>keep</span></p>'
    )


def test_download_links_use_names_without_a_version() -> None:
    """releases/latest/download/<name> only works if the file name never changes."""
    links = set()
    for page in SITE.glob("*.html"):
        links |= set(re.findall(r"releases/latest/download/([\w.-]+)", page.read_text("utf-8")))
    links |= set(
        re.findall(r'"([\w.-]+\.(?:exe|dmg))"', (SITE / "assets" / "site.js").read_text("utf-8"))
    )
    assert links == {"ClipVault-Setup-Windows.exe", "ClipVault-macOS.dmg"}
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert all(name in workflow for name in links)


def test_every_page_has_the_basics() -> None:
    for page in SITE.glob("*.html"):
        html = page.read_text(encoding="utf-8")
        assert '<html lang="en">' in html, page.name
        assert "<title>" in html and 'name="description"' in html, page.name
        assert 'property="og:image"' in html and 'rel="icon"' in html, page.name
        assert 'class="skip" href="#main"' in html and 'id="main"' in html, page.name
        assert "not affiliated with twitch, tiktok, youtube or google" in html.lower(), page.name
