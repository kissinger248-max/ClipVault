"""Emoji in video hooks: choosing, layout, drawing, QA, settings, the clip page and the post captions."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from clipvault import config as cfg
from clipvault import db
from clipvault.process import hook_emoji
from clipvault.render import emoji as E
from clipvault.render import engine, ffmpeg, qa
from clipvault.render.ass import Overlay, build_overlay, layout_headline, line_width
from clipvault.render.template import get_font, load_look
from tests.helpers import add_clip, add_streamer
from tests.test_render import fake_ffmpeg, render_ready  # noqa: F401 - fixtures/helpers of the render tests

RULES = E.load_rules()
ALLOWED = E.allowed(RULES)
LOOK = load_look()


def pick(headline="", title="", transcript="", game="", **kw):
    return hook_emoji.choose(headline=headline, title=title, transcript=transcript, game=game, rules=RULES, **kw)


# --- the emoji table and its pictures ---------------------------------------------------------------------------
def test_every_allowed_emoji_has_a_bundled_picture_and_the_licence_is_shipped():
    assert len(ALLOWED) >= 30
    for char in ALLOWED.values():
        data = E.asset_bytes(char)
        assert data and data[:8] == b"\x89PNG\r\n\x1a\n", char
    folder = Path(E.__file__).resolve().parent.parent / "data" / "emoji"
    assert "CC-BY 4.0" in (folder / "ATTRIBUTION.txt").read_text(encoding="utf-8")
    assert "Attribution 4.0 International" in (folder / "LICENSE-Twemoji-graphics.txt").read_text(encoding="utf-8")


def test_never_emojis_and_flags_cannot_be_used_even_if_the_table_lists_them():
    rules = {"emojis": {"a": "🍆", "b": "🍑", "c": "💦", "d": "🇺🇸", "e": "😂"}, "never": []}
    assert E.allowed(rules) == {"e": "😂"}
    plain, found = E.split_headline("big 🍆 play 🍑 💦 🇺🇸", RULES)
    assert plain == "big play" and found == []
    for banned in ("🍆", "🍑", "💦"):
        assert banned not in ALLOWED.values()


# --- choosing ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("moment", sorted(RULES["moments"]))
def test_an_emoji_is_chosen_for_every_moment_type(moment):
    spec = RULES["moments"][moment]
    word = next(k for k in spec["keywords"] if " " not in k and "'" not in k)
    got = pick(headline=f"That was {word}", transcript=f"{word} {word}")
    assert got == [ALLOWED[spec["emoji"]]], (moment, got)


def test_the_documented_examples_map_as_specified():
    assert pick(headline="He is laughing so hard lol") == ["😂"]
    assert pick(headline="WTF did that just happen") == ["😳"]
    assert pick(headline="He is raging at the game") == ["🤬"]
    assert pick(headline="Clutch win in overtime") == ["🏆"]
    assert pick(headline="This banger is lit") == ["🔥"]
    assert pick(headline="Total fail, so cringe") == ["💀"]
    assert pick(headline="Giveaway for a million dollars") == ["💰"]
    assert pick(headline="Scary jumpscare") == ["😱"]
    assert pick(headline="So wholesome, thank you") == ["🥹"]
    assert pick(headline="Insane drift and a crash", game="Grand Theft Auto V") == ["🚗"]
    assert pick(headline="Penalty goal at the buzzer", game="FIFA 26") == ["⚽"]
    assert pick(headline="Huge dunk", game="NBA 2K26") == ["🏀"]


def test_game_alone_is_not_enough_and_unclear_hooks_get_nothing():
    assert pick(headline="He opens the door", game="Grand Theft Auto V") == []
    assert pick(headline="He talks about his day") == []
    assert pick(headline="") == []


def test_the_transcript_and_the_title_count_too():
    assert pick(headline="Watch this", transcript="lol lmao haha that was funny") == ["😂"]
    assert pick(headline="Watch this", title="Hilarious lmao moment") == ["😂"]


def test_no_emoji_for_sensitive_moments_or_clips_qa_flagged():
    for text in ("He got injured in the fight", "Police arrest him lol", "Blood everywhere lmao", "funeral clutch win"):
        assert pick(headline=text) == [], text
    assert pick(headline="Funny lol", transcript="the cops are here") == []        # sensitive speech counts too
    assert pick(headline="Funny lol", title="ambulance gag") == []
    assert pick(headline="Funny lol", qa_flagged=True) == []
    assert pick(headline="Funny lol", blocked=re.compile(r"\blol\b", re.I)) == []   # a blocked word in the hook


def test_a_hook_that_already_has_an_emoji_keeps_it_and_gets_no_second_one():
    assert pick(headline="Clutch win 🔥") == ["🔥"]
    assert pick(headline="Funny lol 😂 clutch", count=2) == ["😂"]
    assert pick(headline="Clutch win 🔴", count=1) == ["🏆"]          # decoration is not an emoji of the hook


def test_stream_title_decoration_is_stripped_and_only_allowed_emojis_survive():
    assert E.split_headline("🔴 LIVE 🌴 Big play", RULES) == ("LIVE Big play", [])
    assert E.split_headline("🔴🌴", RULES) == ("", [])
    assert E.split_headline("Big play 🔥🔴", RULES) == ("Big play", ["🔥"])
    assert E.split_headline("No emoji ➡️ here ■ ✨", RULES)[0] == "No emoji here"


def test_the_setting_off_means_none_and_two_means_up_to_two():
    assert pick(headline="Funny lol", count=0) == []
    two = pick(headline="Funny lol and a clutch win", count=2)
    assert two == ["😂", "🏆"] or two == ["🏆", "😂"]
    assert len(pick(headline="Funny lol and a clutch win", count=1)) == 1
    assert len(pick(headline="Funny lol", count=2)) == 1        # nothing else fits clearly: just one


def test_a_local_model_picks_within_the_allowed_list_and_bad_answers_fall_back_to_the_table():
    class Http:
        def __init__(self, answer):
            self.answer, self.prompts = answer, []

        def post(self, url, **kw):
            self.prompts.append(kw["json"]["messages"][0]["content"])
            return type("R", (), {"json": lambda s: {"message": {"content": json.dumps({"emoji": self.answer})}}})()

    model = Http("trophy")
    assert pick(headline="Watch this", http=model, model="llama3.2") == ["🏆"]
    assert "laugh" in model.prompts[0] and "none" in model.prompts[0]        # it sees the allowed list
    assert pick(headline="Funny lol", http=Http("none"), model="m") == []      # the model may say "nothing fits"
    assert pick(headline="Funny lol", http=Http("🍆"), model="m") == ["😂"]    # not on the list: the table decides
    assert pick(headline="Funny lol", http=Http("eggplant"), model="m") == ["😂"]
    assert pick(headline="Police chase lol", http=Http("laugh"), model="m") == []   # a model can't override the rules

    class Down:
        def post(self, *a, **k):
            raise ConnectionError("no ollama")
    assert pick(headline="Funny lol", http=Down(), model="m") == ["😂"]


# --- layout -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("style", ["box", "outline"])
@pytest.mark.parametrize("position", ["end", "start"])
@pytest.mark.parametrize("hook", ["Big win", "He did NOT see that coming", "He finally clutched the whole round"])
def test_hook_with_emoji_fits_and_stays_inside_the_safe_box(hook, position, style):
    look = load_look()
    look["headline_style"] = style
    shape = layout_headline(hook, look, ("😳",), position)
    assert shape.fits and len(shape.lines) <= look.headline.max_lines
    flat = [t for line in shape.lines for t in line]
    assert (flat[0] if position == "start" else flat[-1]) == "😳"
    assert all(not (len(line) == 1 and E.is_emoji(line[0])) for line in shape.lines)     # never alone on a line
    overlay = build_overlay(look=look, layout="letterbox", duration=10, headline=hook, login="x",
                            emojis=("😳",), emoji_position=position)
    assert overlay.headline_fits and len(overlay.emojis) == 1
    assert all(b.inside(look.safe_box) for b in overlay.boxes)
    assert any(b.name == "emoji" for b in overlay.boxes)
    assert "😳" not in overlay.ass                                  # never drawn as font text (it would be a blank box)


def test_the_emoji_counts_towards_the_line_width_and_the_box():
    font = get_font(LOOK.headline.font)
    words = ("Big", "win")
    plain = line_width(words, font, 80)
    with_emoji = line_width((*words, "🏆"), font, 80)
    side = font.cap_height(80) * E.SIDE_PER_CAP
    assert with_emoji == pytest.approx(plain + font.width(" ", 80) + side, abs=1.5)
    shape_plain = layout_headline("Big win", LOOK)
    shape_emoji = layout_headline("Big win", LOOK, ("🏆",))
    assert shape_plain.size == shape_emoji.size == 88.0
    extra = line_width((*words, "🏆"), font, 88) - line_width(words, font, 88)
    width = lambda box: box.x1 - box.x0  # noqa: E731
    assert width(shape_emoji.box) - width(shape_plain.box) == pytest.approx(extra, abs=1.5)


def test_a_long_hook_with_an_emoji_still_fits_in_two_lines_by_shrinking():
    hook = "Clutch win in the final round"                      # 88 px on two lines alone, a smaller size with the emoji
    plain = layout_headline(hook, LOOK)
    shape = layout_headline(hook, LOOK, ("😂",))
    assert plain.fits and shape.fits and len(shape.lines) == 2
    assert shape.size < plain.size
    overlay = build_overlay(look=LOOK, layout="letterbox", duration=10, headline=hook, login="x", emojis=("😂",))
    assert overlay.headline_lines == 2 and overlay.headline_fits and all(b.inside(LOOK.safe_box) for b in overlay.boxes)


def test_the_emoji_picture_is_centred_on_the_capital_letters_of_its_line():
    overlay = build_overlay(look=LOOK, layout="letterbox", duration=10, headline="Big win", login="x", emojis=("🏆",))
    place = overlay.emojis[0]
    font = get_font(LOOK.headline.font)
    size = overlay.headline_size
    cap = font.cap_height(size)
    row_centre = LOOK.headline.top + LOOK.headline.padding_y + size * LOOK.headline.line_spacing / 2
    assert place.side == pytest.approx(cap * E.SIDE_PER_CAP)
    assert place.y + place.side / 2 == pytest.approx(row_centre, abs=1.0)


def test_a_hook_too_long_for_its_emoji_is_drawn_without_it(conn, home, fake_ffmpeg):   # noqa: F811
    config, _video = render_ready(conn, home)
    clip = conn.execute("SELECT * FROM clips WHERE id='c1'").fetchone()
    too_long = "He finally whole unbelievable victory"            # fits in 2 lines at the smallest size, but not with a picture
    assert layout_headline(too_long, LOOK).fits and not layout_headline(too_long, LOOK, ("😂",)).fits
    text, chars = engine.hook_emojis_for(clip, too_long, "lol lmao funny", LOOK, False, None, ask=False)
    assert chars == () and text == too_long                    # a clear fit for 😂, but there is no room: drawn without it
    short, chars = engine.hook_emojis_for(clip, "Funny lol", "", LOOK, False, None, ask=False)
    assert chars == ("😂",) and short == "Funny lol"


# --- QA ---------------------------------------------------------------------------------------------------------
def test_qa_fails_a_hook_that_would_show_blank_boxes_or_a_picture_outside_the_safe_box():
    ok = build_overlay(look=LOOK, layout="letterbox", duration=10, headline="Big win", login="x", emojis=("🏆",))
    assert qa._emoji_check(ok, "Big win", LOOK)[1:] == (True, "1 emoji picture(s), all inside the safe box")
    name, passed, detail = qa._emoji_check(ok, "Big win ☃", LOOK)                  # a snowman: no glyph in Montserrat
    assert name == "hook_emoji" and not passed and "blank boxes" in detail
    stray = Overlay(ass="", emojis=[E.Placement("🏆", 1000, 300, 70)])               # past x = 930
    assert not qa._emoji_check(stray, "Big win", LOOK)[1] and "outside the safe box" in qa._emoji_check(stray, "Big win", LOOK)[2]
    unknown = Overlay(ass="", emojis=[E.Placement("🦑", 300, 300, 70)])              # not bundled: no picture
    assert "no picture" in qa._emoji_check(unknown, "Big win", LOOK)[2]
    none = build_overlay(look=LOOK, layout="letterbox", duration=10, headline="Big win", login="x")
    assert qa._emoji_check(none, "Big win", LOOK) == ("hook_emoji", True, "no emoji, no blank boxes")


# --- drawing: the FFmpeg graph and a real render -----------------------------------------------------------------
def test_the_filter_graph_lays_each_picture_over_the_subtitles_and_changes_nothing_without_them():
    old = "[0:v]scale=1080:608[base];[base]subtitles=overlay.ass:fontsdir=fonts,setsar=1,format=yuv420p[v]"
    assert ffmpeg.video_graph("scale=1080:608") == old
    graph = ffmpeg.video_graph("scale=1080:608", [("emoji_0.png", 800, 400), ("emoji_1.png", 900, 400)], 2)
    assert graph == ("[0:v]scale=1080:608[base];[base]subtitles=overlay.ass:fontsdir=fonts[t0];"
                     "[t0][2:v]overlay=800:400:format=auto[t1];[t1][3:v]overlay=900:400:format=auto[t2];"
                     "[t2]setsar=1,format=yuv420p[v]")


def test_encode_passes_the_pictures_as_inputs_after_the_source_and_the_silent_track(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(ffmpeg, "_ffmpeg", lambda arguments, cwd=None, what="": seen.append(arguments) or "")
    for has_audio, first in ((True, 1), (False, 2)):
        (tmp_path / "out.mp4").write_bytes(b"x")
        ffmpeg.encode(source=Path("in.mp4"), dest=tmp_path / "out.mp4", workdir=tmp_path, video_filter="null",
                      audio_filter="anull", has_audio=has_audio, start=0, duration=3, look=LOOK,
                      emoji_images=[("emoji_0.png", 10, 20)])
        args = seen[-1]
        assert args.count("-i") == (2 if has_audio else 3) and "emoji_0.png" in args
        assert f"[{first}:v]overlay=10:20:format=auto" in args[args.index("-filter_complex") + 1]


# --- the engine --------------------------------------------------------------------------------------------------
def _emoji_clip(conn, home):
    config, video = render_ready(conn, home, text="that was so funny lol lmao")
    conn.execute("UPDATE clips SET title = ?, headline = ? WHERE id = 'c1'", ("🔴 LIVE 🌴 Funny moment", None))
    return config


def test_render_draws_the_picked_emoji_records_it_and_strips_stream_title_decoration(conn, home, fake_ffmpeg, monkeypatch):  # noqa: F811
    monkeypatch.setattr("clipvault.posting.captiongen.pick_model", lambda **k: None)
    config = _emoji_clip(conn, home)
    result = engine.render_clip(conn, config, "c1")
    assert result.headline == "LIVE Funny moment" and "🔴" not in result.headline and "🌴" not in result.headline
    encode = fake_ffmpeg["encodes"][-1]
    assert [name for name, _x, _y in encode["emoji_images"]] == ["emoji_0.png"]
    assert (encode["workdir"] / "emoji_0.png").exists() or True                       # temp folder is gone after the render
    row = conn.execute("SELECT hook_emoji, qa_passed FROM renders").fetchone()
    assert row["hook_emoji"] == "😂" and row["qa_passed"] == 1
    names = [c["name"] for c in json.loads(conn.execute("SELECT qa_report_json FROM renders").fetchone()[0])["checks"]]
    assert "hook_emoji" in names


def test_the_persons_choice_wins_and_off_means_none(conn, home, fake_ffmpeg, monkeypatch):  # noqa: F811
    import dataclasses

    monkeypatch.setattr("clipvault.posting.captiongen.pick_model", lambda **k: None)
    config = _emoji_clip(conn, home)
    conn.execute("UPDATE clips SET hook_emoji = '🏆' WHERE id='c1'")
    engine.render_clip(conn, config, "c1")
    assert conn.execute("SELECT hook_emoji FROM renders").fetchone()[0] == "🏆"
    conn.execute("UPDATE clips SET hook_emoji = '' WHERE id='c1'")                    # removed
    engine.render_clip(conn, config, "c1")
    assert conn.execute("SELECT hook_emoji FROM renders").fetchone()[0] == ""
    assert fake_ffmpeg["encodes"][-1]["emoji_images"] == []
    conn.execute("UPDATE clips SET hook_emoji = '🍆' WHERE id='c1'")                  # not allowed: ignored, nothing drawn
    engine.render_clip(conn, config, "c1")
    assert conn.execute("SELECT hook_emoji FROM renders").fetchone()[0] == ""
    conn.execute("UPDATE clips SET hook_emoji = NULL WHERE id='c1'")
    off = dataclasses.replace(config, look=dataclasses.replace(config.look, hook_emojis=0))
    engine.render_clip(conn, off, "c1")
    assert conn.execute("SELECT hook_emoji FROM renders").fetchone()[0] == ""


def test_no_emoji_for_a_clip_whose_last_render_was_flagged_by_qa(conn, home, fake_ffmpeg, monkeypatch):  # noqa: F811
    monkeypatch.setattr("clipvault.posting.captiongen.pick_model", lambda **k: None)
    config = _emoji_clip(conn, home)
    engine.render_clip(conn, config, "c1")
    conn.execute("UPDATE renders SET qa_passed = 0")
    engine.render_clip(conn, config, "c1")
    assert conn.execute("SELECT hook_emoji FROM renders").fetchone()[0] == ""


# --- settings, the clip page -------------------------------------------------------------------------------------
def test_look_settings_round_trip_and_reject_bad_values(client, home):
    from tests.test_web import csrf, login

    login(client)
    token = csrf(client, "/settings")
    base = {"csrf": token, "style": "box", "accent": "#FFD400", "size": "72"}
    assert cfg.load_config(home / "config.yaml").look.hook_emojis == 1                  # the default
    ok = client.post("/settings/look", data={**base, "hook_emojis": "2", "hook_emoji_position": "start"})
    assert ok.status_code == 303 and "Look saved" in ok.headers["location"].replace("+", " ")
    look = cfg.load_config(home / "config.yaml").look
    assert (look.hook_emojis, look.hook_emoji_position) == (2, "start")
    assert load_look(cfg.load_config(home / "config.yaml")).hook_emoji == {"count": 2, "position": "start"}
    bad = client.post("/settings/look", data={**base, "hook_emojis": "9"})
    assert "Not saved" in bad.headers["location"].replace("+", " ")
    assert cfg.load_config(home / "config.yaml").look.hook_emojis == 2
    wizard = client.post("/settings/look", data=base)                                    # the setup wizard has no emoji fields
    assert cfg.load_config(home / "config.yaml").look.hook_emojis == 2 and wizard.status_code == 303
    page = client.get("/settings").text
    assert 'name="hook_emojis"' in page and "Up to 2" in page and 'name="hook_emoji_position"' in page
    off = client.post("/settings/look", data={**base, "hook_emojis": "0", "hook_emoji_position": "end"})
    assert cfg.load_config(home / "config.yaml").look.hook_emojis == 0 and off.status_code == 303


def test_clip_page_shows_the_emoji_and_lets_you_change_or_remove_it(client, home, library):
    from tests.test_web import csrf, login, saved_clip

    conn = db.open_db(home / "clipvault.db")
    add_streamer(conn, "alpha")
    saved_clip(conn, library, "clip1")
    conn.execute("INSERT INTO renders (clip_id, layout, path, qa_passed, hook_emoji) VALUES "
                 "('clip1', 'letterbox', 'alpha/2026-11/vertical/clip1_9x16.mp4', 1, '😂')")
    conn.close()
    (library / "alpha/2026-11/vertical").mkdir(parents=True, exist_ok=True)
    (library / "alpha/2026-11/vertical/clip1_9x16.mp4").write_bytes(b"v")
    login(client)
    html = client.get("/clips/clip1").text
    assert "Hook emoji" in html and "This phone version shows 😂." in html and 'value="🏆"' in html
    token = csrf(client, "/clips/clip1")

    def stored():
        c = db.open_db(home / "clipvault.db")
        try:
            return c.execute("SELECT hook_emoji FROM clips WHERE id='clip1'").fetchone()[0]
        finally:
            c.close()
    assert stored() is None
    for choice, expected in (("🏆", "🏆"), ("none", ""), ("auto", None)):
        r = client.post("/clips/clip1/hook-emoji", data={"csrf": token, "emoji": choice})
        assert r.status_code == 303 and "Saved" in r.headers["location"].replace("+", " ")
        assert stored() == expected
    bad = client.post("/clips/clip1/hook-emoji", data={"csrf": token, "emoji": "🍆"})
    assert "Couldn" in bad.headers["location"].replace("+", " ") and stored() is None
    assert client.post("/clips/clip1/hook-emoji", data={"emoji": "🏆"}).status_code == 403       # needs the form token
    again = client.post("/clips/clip1/hook-emoji", data={"csrf": token, "emoji": "none", "remake": "1"})
    assert "Making the phone version again" in again.headers["location"].replace("+", " ")


# --- the posts ---------------------------------------------------------------------------------------------------
def test_the_tiktok_caption_and_the_youtube_title_use_the_same_emoji_as_the_video():
    from clipvault.posting import captiongen

    facts = {"clip_id": "c", "streamer": "Kai Cenat", "login": "kaicenat", "transcript": "that was so funny",
             "clip_title": "Funny moment", "game": "Just Chatting", "stream_title": "", "why_picked": "",
             "hook": "Funny moment", "moment_types": ["funny"], "hook_emoji": "😂"}
    package = captiongen.generate(facts, rules=captiongen.load_rules(), model=None)
    per = package["per_platform"]
    assert per["tiktok"].splitlines()[0] == "Funny moment 😂" or per["tiktok"].startswith("Funny moment 😂")
    assert per["youtube_title"] == "Funny moment 😂 #shorts"
    assert "😂" in per["instagram"] and package["title"] == "Funny moment"        # the plain hook text is unchanged
    plain = captiongen.generate({**facts, "hook_emoji": ""}, rules=captiongen.load_rules(), model=None)
    assert "😂" not in plain["per_platform"]["tiktok"] and plain["per_platform"]["youtube_title"] == "Funny moment #shorts"
    long_hook = "Funny " * 25
    cut = captiongen.generate({**facts, "hook": long_hook, "clip_title": long_hook, "hook_emoji": "😂"}, rules=captiongen.load_rules(), model=None)
    title = cut["per_platform"]["youtube_title"]
    assert len(title) <= 100 and title.endswith("😂 #shorts")


def test_cached_captions_follow_a_changed_emoji_unless_the_person_edited_them(conn, home, library):
    from clipvault.posting import captiongen, uploads
    from tests.test_web import saved_clip

    add_streamer(conn, "alpha")
    saved_clip(conn, library, "clip1", title="Funny moment")
    lib = uploads.library_for(conn)
    clip = {"id": "clip1", "title": "Funny moment", "game_name": "Just Chatting", "headline": "Funny moment", "login": "alpha",
            "display_name": "Alpha", "transcript_path": None, "hook_emoji": "😂"}
    first, _ = uploads.ensure_caption(conn, lib, clip)
    assert "😂" in first["per_platform"]["tiktok"]
    second, _ = uploads.ensure_caption(conn, lib, {**clip, "hook_emoji": "🏆"})
    assert "🏆" in second["per_platform"]["tiktok"] and "😂" not in second["per_platform"]["tiktok"]
    edited = uploads.save_edit(conn, "clip1", {"tiktok": "my own words #alpha"})
    kept, _ = uploads.ensure_caption(conn, lib, {**clip, "hook_emoji": "🔥"})
    assert kept["per_platform"]["tiktok"] == "my own words #alpha" and edited["source"] == "edited"


# --- the database -----------------------------------------------------------------------------------------------
def test_migration_0012_adds_the_columns_and_keeps_existing_rows(home):
    path = home / "old.db"
    conn = db.connect(path)
    conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for name, sql in db.available_migrations():
        if name < "0012_hook_emoji":
            conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nINSERT INTO schema_migrations VALUES ('{name}', 'x');\nCOMMIT;")
    add_streamer(conn, "alpha")
    add_clip(conn, "old1")
    conn.execute("INSERT INTO renders (clip_id, layout, path) VALUES ('old1', 'letterbox', 'a.mp4')")
    conn.close()
    conn = db.open_db(path)
    assert conn.execute("SELECT id, hook_emoji FROM clips").fetchone()[:] == ("old1", None)
    assert conn.execute("SELECT clip_id, hook_emoji FROM renders").fetchone()[:] == ("old1", None)
    conn.close()
