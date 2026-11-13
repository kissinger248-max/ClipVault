from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from clipvault.library import storage
from tests.helpers import make_config

CHICAGO = ZoneInfo("America/Chicago")
CREATED = datetime(2026, 11, 7, 0, 7, 23, tzinfo=UTC)  # still 6 November in Chicago


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("of course he's the first person", "of-course-he-s-the-first-person"),
        ("  WICKED   BACKFLIP!!! ", "wicked-backflip"),
        ("café crème", "cafe-creme"),
        ("\U0001f991LIVE\U0001f991DRAMA\U0001f991", "live-drama"),
        ("???", "clip"),
        ("", "clip"),
        ("a" * 80, "a" * 40),
    ],
)
def test_slugify(title: str, slug: str) -> None:
    assert storage.slugify(title) == slug


def test_clip_relpath_follows_the_spec_naming() -> None:
    path = storage.clip_relpath(
        "valkyrae",
        "TawdryMuddyDoge-Kjqq",
        "of course he's the first person",
        2536,
        CREATED,
        CHICAGO,
    )
    assert path == (
        "valkyrae/2026-11/2026-11-06_2536views_of-course-he-s-the-first-person_TawdryMuddyDoge-Kjqq.mp4"
    )
    assert storage.sidecar_relpath(path).endswith("_TawdryMuddyDoge-Kjqq.json")


def test_clip_relpath_is_safe_for_odd_ids_and_reserved_names() -> None:
    path = storage.clip_relpath("com1", "vod:123:4567", "x/y\\z: *?", 0, CREATED, CHICAGO)
    assert path == "_com1/2026-11/2026-11-06_0views_x-y-z_vod-123-4567.mp4"


def test_long_library_paths_shorten_the_title_not_the_id() -> None:
    clip_id = "ExpensiveEnchantingCourgettePeteZarollTie-hhhHU1d1xMvRJJjj"
    title = "word " * 30
    roomy = storage.clip_relpath("a" * 25, clip_id, title, 1234567, CREATED, CHICAGO, 30)
    tight = storage.clip_relpath("a" * 25, clip_id, title, 1234567, CREATED, CHICAGO, 100)
    assert len(roomy) > len(tight)
    assert 100 + 1 + len(tight) <= storage.MAX_PATH_CHARS
    assert clip_id in tight and clip_id in roomy
    # A hopelessly long root still gives a usable name: the title shrinks to a stub.
    stub = storage.clip_relpath("a" * 25, clip_id, title, 1234567, CREATED, CHICAGO, 400)
    assert "_word_" in stub and clip_id in stub


def test_library_is_set_up_once_then_detected_as_away(conn: sqlite3.Connection, home: Path) -> None:
    config = make_config(home)
    library = storage.open_library(conn, config)
    assert library.available and (config.library_root / storage.MARKER).is_file()
    assert library.write_base == config.library_root

    # The drive is unplugged: the folder (or its marker) is gone.
    (config.library_root / storage.MARKER).unlink()
    away = storage.open_library(conn, config)
    assert not away.available
    assert away.write_base == home / storage.CACHE_DIRNAME
    assert not (config.library_root / storage.MARKER).exists()  # not silently re-created


def test_unreachable_new_library_root_is_reported_not_raised(
    conn: sqlite3.Connection, home: Path
) -> None:
    blocker = home / "a-file"
    blocker.write_text("x", encoding="utf-8")
    config = make_config(blocker)  # library root would be inside a file: can't be created
    assert not storage.open_library(conn, config).available


def test_resolve_looks_in_library_cache_and_favorites(conn: sqlite3.Connection, home: Path) -> None:
    library = storage.open_library(conn, make_config(home))
    rel = "alpha/2026-11/clip.mp4"
    assert storage.resolve(library, rel) is None
    assert storage.resolve(library, None) is None

    favorite = library.root / "Favorites" / "clip.mp4"
    favorite.parent.mkdir(parents=True)
    favorite.write_bytes(b"f")
    assert storage.resolve(library, rel) == favorite
    assert storage.is_favorite(library, favorite)

    cached = library.cache / rel
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"c")
    assert storage.resolve(library, rel) == cached

    real = library.root / rel
    real.parent.mkdir(parents=True)
    real.write_bytes(b"r")
    assert storage.resolve(library, rel) == real
    assert not storage.is_favorite(library, real)
    assert storage.is_inside_library(library, real)
    assert not storage.is_inside_library(library, home / "elsewhere.mp4")


def test_cached_files_move_into_the_library_when_it_returns(
    conn: sqlite3.Connection, home: Path
) -> None:
    config = make_config(home)
    library = storage.open_library(conn, config)
    cached = library.cache / "alpha/2026-11/clip.mp4"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"video")
    (cached.parent / "clip.json").write_text("{}", encoding="utf-8")
    (cached.parent / "other.mp4.part").write_bytes(b"half")

    assert storage.relocate_cache(library) == 2
    assert (library.root / "alpha/2026-11/clip.mp4").read_bytes() == b"video"
    assert not cached.exists()
    assert (cached.parent / "other.mp4.part").exists()  # unfinished downloads stay put
    assert storage.relocate_cache(library) == 0


def test_sidecar_is_written_whole_and_replaces_the_old_one(home: Path) -> None:
    path = home / "x" / "clip.json"
    storage.write_sidecar(path, {"id": "a", "title": "café"})
    storage.write_sidecar(path, {"id": "b", "when": datetime(2026, 1, 1, tzinfo=UTC)})
    assert json.loads(path.read_text(encoding="utf-8"))["id"] == "b"
    assert list(path.parent.iterdir()) == [path]
