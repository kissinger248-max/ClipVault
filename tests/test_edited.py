"""The "Edited clips" folders and one streamer's page (library/edited.py, /streamers/{login})."""

from __future__ import annotations

import os
from pathlib import Path

from clipvault import activity
from clipvault.library import edited, retention, storage
from clipvault.library.storage import Library
from tests.helpers import add_clip, add_streamer, make_config
from tests.test_web import csrf, finish_setup, login, saved_clip


def rendered(conn, library: Path, clip_id: str, streamer: str = "alpha", **kw) -> Path:
    """A clip with a finished phone version on disk."""
    when = kw.pop("render_at", "2026-11-12T10:05:00Z")
    relpath = f"{streamer}/2026-11/vertical/{clip_id}_9x16.mp4"
    video = library / relpath
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"phone version of " + clip_id.encode())
    if not conn.execute("SELECT 1 FROM clips WHERE id = ?", (clip_id,)).fetchone():
        add_clip(conn, clip_id, state="rendered", **kw)
    conn.execute(
        "INSERT INTO renders (clip_id, layout, path, qa_passed, created_at) "
        "VALUES (?, 'letterbox', ?, 1, ?)",
        (clip_id, relpath, when),
    )
    return video


def open_lib(conn, home: Path) -> tuple[Library, object]:
    config = make_config(home)
    return storage.open_library(conn, config), config


def files(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# --- the folders ---------------------------------------------------------------------------


def test_a_phone_version_goes_in_its_streamers_folder_and_the_all_folder(conn, home, library):
    add_streamer(conn, "kaicenat", display_name="KaiCenat")
    video = rendered(conn, library, "c1", "kaicenat", headline="KAI LOSES IT")
    lib, config = open_lib(conn, home)
    made = edited.publish(conn, config, lib, "c1")
    own = library / "Edited clips" / "Kai Cenat" / "2026-11-10 14.00 KAI LOSES IT.mp4"
    everyone = library / "Edited clips" / "All streamers" / (
        "2026-11-10 14.00 Kai Cenat - KAI LOSES IT.mp4")  # fmt: skip
    assert made == [own, everyone]
    assert os.path.samefile(own, video) and os.path.samefile(everyone, video)  # no extra space
    rows = conn.execute("SELECT path FROM edited_files ORDER BY path").fetchall()
    assert [r[0] for r in rows] == [
        "Edited clips/All streamers/2026-11-10 14.00 Kai Cenat - KAI LOSES IT.mp4",
        "Edited clips/Kai Cenat/2026-11-10 14.00 KAI LOSES IT.mp4",
    ]


def test_a_copy_is_made_where_hard_links_dont_work(conn, home, library, monkeypatch):
    add_streamer(conn, "alpha", display_name="Alpha")
    video = rendered(conn, library, "c1", headline="BIG PLAY")

    def no_links(source, target):
        raise OSError("this drive can't hold hard links")

    monkeypatch.setattr(edited.os, "link", no_links)
    lib, config = open_lib(conn, home)
    own, _ = edited.publish(conn, config, lib, "c1")
    assert own.read_bytes() == video.read_bytes() and not os.path.samefile(own, video)


def test_making_the_clip_again_replaces_its_files(conn, home, library):
    add_streamer(conn, "alpha", display_name="Alpha")
    rendered(conn, library, "c1", headline="OLD WORDS")
    lib, config = open_lib(conn, home)
    edited.publish(conn, config, lib, "c1")
    conn.execute("UPDATE clips SET headline = 'NEW WORDS' WHERE id = 'c1'")
    newer = library / "alpha/2026-11/vertical/c1_split.mp4"
    newer.write_bytes(b"the split version")
    conn.execute(
        "INSERT INTO renders (clip_id, layout, path, qa_passed, created_at) "
        "VALUES ('c1', 'split', 'alpha/2026-11/vertical/c1_split.mp4', 1, '2026-11-12T11:00:00Z')"
    )
    edited.publish(conn, config, lib, "c1")
    assert files(library / "Edited clips" / "Alpha") == ["2026-11-10 14.00 NEW WORDS.mp4"]
    assert files(library / "Edited clips" / "All streamers") == [
        "2026-11-10 14.00 Alpha - NEW WORDS.mp4"
    ]
    assert (library / "Edited clips" / "Alpha" / "2026-11-10 14.00 NEW WORDS.mp4").read_bytes() == (
        b"the split version")  # fmt: skip
    assert conn.execute("SELECT count(*) FROM edited_files").fetchone()[0] == 2


def test_two_clips_with_the_same_name_both_fit(conn, home, library):
    add_streamer(conn, "alpha", display_name="Alpha")
    rendered(conn, library, "c1", headline="SAME")
    rendered(conn, library, "c2", headline="SAME")
    lib, config = open_lib(conn, home)
    edited.publish(conn, config, lib, "c1")
    edited.publish(conn, config, lib, "c2")
    assert files(library / "Edited clips" / "Alpha") == [
        "2026-11-10 14.00 SAME (2).mp4",
        "2026-11-10 14.00 SAME.mp4",
    ]


def test_deleting_a_clip_removes_only_its_tracked_edited_files(conn, home, library):
    add_streamer(conn, "alpha", display_name="Alpha")
    saved_clip(conn, library, "c1", headline="GONE SOON")
    rendered(conn, library, "c1")
    lib, config = open_lib(conn, home)
    edited.publish(conn, config, lib, "c1")
    mine = library / "Edited clips" / "Alpha" / "my own notes.txt"
    mine.write_text("the user's file")
    assert retention.remove_clip_files(conn, lib, "c1", "user", "panel")
    assert files(library / "Edited clips" / "Alpha") == ["my own notes.txt"]
    assert files(library / "Edited clips" / "All streamers") == []
    assert conn.execute("SELECT count(*) FROM edited_files").fetchone()[0] == 0


def test_older_phone_versions_are_added_once(conn, home, library):
    add_streamer(conn, "alpha", display_name="Alpha")
    rendered(conn, library, "c1", headline="FIRST")
    rendered(conn, library, "c2", headline="SECOND")
    add_clip(conn, "c3", state="transcribed")  # no phone version yet: not in the folders
    lib, config = open_lib(conn, home)
    assert edited.sync(conn, config, lib) == 2
    (library / "Edited clips" / "Alpha" / "2026-11-10 14.00 FIRST.mp4").unlink()  # by hand
    assert edited.sync(conn, config, lib) == 0  # deleted by hand stays deleted
    assert files(library / "Edited clips" / "Alpha") == ["2026-11-10 14.00 SECOND.mp4"]


def test_nothing_happens_while_the_library_is_away(conn, home, library):
    add_streamer(conn, "alpha")
    rendered(conn, library, "c1")
    _, config = open_lib(conn, home)
    away = Library(library, home / "cache", False)
    assert edited.publish(conn, config, away, "c1") == []
    assert edited.sync(conn, config, away) == 0
    assert not (library / "Edited clips").exists()


def test_names_work_on_windows_and_macos():
    assert edited.safe_name('What?! <He> said: "no" / ok.') == "What! He said no ok"
    assert edited.safe_name("con") == "_con"
    assert edited.safe_name("  ...  ", "clip") == "clip"
    long = edited._fit("x" * 300)
    assert len(long + ".mp4") <= edited.NAME_MAX


# --- the panel ---------------------------------------------------------------------------------


def test_a_streamers_page_shows_only_their_edited_clips(client, conn, home, library):
    finish_setup(home)
    login(client)
    add_streamer(conn, "alpha", display_name="Alpha")
    add_streamer(conn, "beta", display_name="Beta")
    rendered(conn, library, "a-done", headline="ALPHA WINS")
    add_clip(conn, "a-raw", state="transcribed")  # only the original: not shown
    rendered(conn, library, "b-done", "beta", streamer_id=2)
    html = client.get("/streamers/Alpha").text
    assert 'href="/clips/a-done"' in html
    assert "/clips/a-raw" not in html and "/clips/b-done" not in html
    assert 'data-streamer="alpha"' in html and "Edited Clips Folder" in html
    assert "isn" not in html or "Alpha" in html
    assert client.get("/streamers/nobody").status_code == 404
    listing = client.get("/streamers").text
    assert 'href="/streamers/alpha"' in listing and "1 edited" in listing
    assert "All Edited Clips Folder" in client.get("/clips").text


def test_the_live_view_can_follow_one_streamer(client, conn, home, library):
    finish_setup(home)
    login(client)
    add_streamer(conn, "alpha", display_name="Alpha")
    add_streamer(conn, "beta", display_name="Beta")
    saved_clip(conn, library, "a-next")
    add_clip(conn, "b-next", streamer_id=2, state="fetched", file_path="beta/b.mp4")
    activity.live_channel(activity.LiveChannel("100", "alpha", windows=[1, 2], baseline=1.0))
    activity.live_channel(activity.LiveChannel("200", "beta", windows=[1, 2], baseline=1.0))
    activity.event("moment", "Live moment caught", login="alpha")
    activity.event("moment", "Live moment caught", login="beta")
    try:
        every = client.get("/api/activity").json()
        one = client.get("/api/activity", params={"streamer": "alpha"}).json()
    finally:
        activity.live_ended("100")
        activity.live_ended("200")
    assert {p["id"] for p in every["pipeline"]} == {"a-next", "b-next"}
    assert [p["id"] for p in one["pipeline"]] == ["a-next"] and one["waiting"] == 1
    assert [ch["login"] for ch in one["live"]["channels"]] == ["alpha"]
    assert {e["login"] for e in one["events"]} == {"alpha"}
    assert one["live"]["streamer"] == "alpha"


def test_the_folder_buttons_open_the_folder_on_this_computer(client, conn, home, library,
                                                              monkeypatch):  # fmt: skip
    finish_setup(home)
    login(client)
    add_streamer(conn, "kaicenat", display_name="KaiCenat")
    opened = []
    monkeypatch.setattr(edited, "open_in_file_manager", opened.append)
    token = csrf(client)
    reply = client.post("/folders/open", data={"csrf": token, "streamer": "kaicenat",
                                               "next": "/streamers"})  # fmt: skip
    assert reply.status_code == 303 and reply.headers["location"].startswith("/streamers?msg=")
    client.post("/folders/open", data={"csrf": token, "next": "/clips"})
    assert opened == [library / "Edited clips" / "Kai Cenat",
                      library / "Edited clips" / "All streamers"]  # fmt: skip
    missing = client.post("/folders/open", data={"csrf": token, "streamer": "nobody"})
    assert "No+such+streamer" in missing.headers["location"] and len(opened) == 2
    outside = client.post("/folders/open", data={"csrf": token, "next": "https://evil.example"})
    assert outside.headers["location"].startswith("/clips?")


def test_a_phone_cant_open_a_folder_on_the_computer(client, conn, home, library, monkeypatch):
    from clipvault.web import server

    finish_setup(home)
    login(client)
    opened = []
    monkeypatch.setattr(edited, "open_in_file_manager", opened.append)
    monkeypatch.setattr(server, "_is_local", lambda request: False)
    reply = client.post("/folders/open", data={"csrf": csrf(client), "next": "/clips"})
    assert opened == [] and "On+the+computer" in reply.headers["location"]
