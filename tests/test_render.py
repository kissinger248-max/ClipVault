"""Vertical rendering: text layout, layouts, QA and the engine (FFmpeg is faked)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clipvault import config as cfg
from clipvault import jobs
from clipvault.__main__ import main
from clipvault.process import filter as flt
from clipvault.process.transcribe import Transcript, Word, mask_pattern
from clipvault.render import ass, engine, ffmpeg, layouts, qa
from clipvault.render.ffmpeg import MediaInfo, RenderError
from clipvault.render.layouts import Rect
from clipvault.render.template import get_font, load_look
from tests.helpers import FakeDownloader, add_clip, add_streamer, clip_row, make_config

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
LOOK = load_look()
SOURCE = MediaInfo(1280, 720, 60.0, 30.0, True, "hevc")


def spoken(text: str, start: float = 0.5, each: float = 0.3, gap: float = 0.05) -> tuple[Word, ...]:
    words, t = [], start
    for token in text.split():
        if token == "|":  # a pause
            t += 1.0
            continue
        words.append(Word(token, round(t, 2), round(t + each, 2)))
        t += each + gap
    return tuple(words)


# --- template and font metrics ---------------------------------------------------


def test_default_look_matches_the_hard_rule_safe_box() -> None:
    safe = LOOK.safe_box
    assert (safe.x0, safe.y0, safe.x1, safe.y1) == (150, 270, 930, 1340)
    assert (LOOK.frame.width, LOOK.frame.height) == (1080, 1920)
    assert (LOOK.export.min_seconds, LOOK.export.max_seconds) == (15, 59)


def test_user_look_overrides_only_what_it_sets(home: Path) -> None:
    (home / "looks").mkdir()
    (home / "looks" / "mine.yaml").write_text("caption: {size: 60}\n", encoding="utf-8")
    config = cfg.Config(look=cfg.LookConfig(template="mine", accent="#00FF00"))
    look = load_look(config)
    assert (look.caption.size, look.caption.outline, look.accent) == (60, 7, "#00FF00")
    with pytest.raises(cfg.ConfigError, match="look template not found"):
        load_look(cfg.Config(look=cfg.LookConfig(template="missing")))


def test_font_sizes_are_converted_for_libass() -> None:
    font = get_font("Montserrat-Black")
    assert font.full_name == "Montserrat Black"
    # libass scales by Windows ascent + descent (1109 + 453 per 1000 units for Montserrat).
    assert font.ass_size(88) == pytest.approx(88 * 1.562)
    assert font.line_height(100) == pytest.approx(156.2)
    assert 0 < font.cap_height(100) < 80
    assert font.width("WW", 50) == pytest.approx(2 * font.width("W", 50), rel=0.05)
    assert font.width("Hello", 100) == pytest.approx(2 * font.width("Hello", 50), rel=0.02)


# --- headline --------------------------------------------------------------------


def test_short_headline_is_one_line_at_full_size() -> None:
    shape = ass.layout_headline("Clutch play", LOOK)
    assert (len(shape.lines), shape.size, shape.fits) == (1, 88, True)
    assert shape.box.y0 == 270 and shape.box.inside(LOOK.safe_box)
    assert (shape.box.x0 + shape.box.x1) / 2 == pytest.approx(540)


def test_longer_headline_wraps_evenly_then_shrinks() -> None:
    two = ass.layout_headline("Kai loses it over GTA cops", LOOK)
    assert [list(line) for line in two.lines] == [["Kai", "loses", "it"], ["over", "GTA", "cops"]]
    assert two.size == 88 and two.fits
    shrunk = ass.layout_headline("Streamer accidentally deletes entire inventory", LOOK)
    assert shrunk.fits and len(shrunk.lines) == 2 and 64 <= shrunk.size < 88
    assert shrunk.box.x1 - shrunk.box.x0 <= 780 and shrunk.box.inside(LOOK.safe_box)


def test_headline_that_cannot_fit_is_reported() -> None:
    shape = ass.layout_headline("supercalifragilisticexpialidocious " * 4, LOOK)
    assert not shape.fits and shape.size == 64


def test_key_word_defaults_to_the_longest() -> None:
    assert ass.pick_key_word(["Kai", "loses", "it!!!"]) == 1
    assert ass.pick_key_word([]) == -1


# --- captions --------------------------------------------------------------------


def test_caption_blocks_split_on_pauses_phrases_and_size() -> None:
    words = spoken("no way, that was insane | clip it right now chat please do it")
    blocks = ass.build_caption_blocks(words, LOOK, duration=20.0)
    texts = [" ".join(w.text for w in b.words) for b in blocks]
    assert texts[0] == "NO WAY"  # a comma ends the phrase; the comma itself is not shown
    assert texts[1] == "THAT WAS INSANE"  # the pause starts a new block
    assert all(1 <= len(b.words) <= 5 for b in blocks)
    assert "".join(texts).isupper()
    for block in blocks:
        assert len(block.lines) <= 2
        assert sorted(i for line in block.lines for i in line) == list(range(len(block.words)))


def test_every_block_stays_on_screen_long_enough_even_in_fast_speech() -> None:
    rapid = tuple(Word(f"w{i}.", i * 0.12, i * 0.12 + 0.1) for i in range(40))
    blocks = ass.build_caption_blocks(rapid, LOOK, duration=6.0)
    assert all(b.end - b.start >= 0.4 - 1e-6 for b in blocks)
    assert all(a.end <= b.start + 1e-6 for a, b in zip(blocks, blocks[1:], strict=False))
    assert blocks[-1].end <= 6.0 + 1e-6


def test_wide_blocks_use_two_lines_and_stay_inside_the_safe_box() -> None:
    words = spoken("EXTRAORDINARILY UNBELIEVABLE CATASTROPHIC MISUNDERSTANDING HAPPENING")
    overlay = ass.build_overlay(
        look=LOOK, layout="letterbox", duration=20.0, headline=None, login="alpha", words=words
    )
    captions = [b for b in overlay.boxes if b.name == "caption"]
    assert captions and all(b.inside(LOOK.safe_box) for b in captions)
    assert all(len(block.lines) <= 2 for block in overlay.blocks)


def test_masked_words_never_reach_the_captions() -> None:
    mask = mask_pattern(["frick"])
    blocks = ass.build_caption_blocks(spoken("oh frick that hurt"), LOOK, mask=mask)
    assert "****" in [w.text for b in blocks for w in b.words]


def test_words_before_a_trimmed_start_are_dropped() -> None:
    words = (Word("gone", -3.0, -2.5), Word("kept", 0.2, 0.6), Word("late", 99.0, 99.5))
    blocks = ass.build_caption_blocks(words, LOOK, duration=30.0)
    assert [w.text for b in blocks for w in b.words] == ["KEPT"]


# --- the ASS file ----------------------------------------------------------------


@pytest.mark.parametrize("layout", ["letterbox", "split", "native"])
def test_overlay_text_is_inside_the_safe_box_for_every_layout(layout: str) -> None:
    overlay = ass.build_overlay(
        look=LOOK,
        layout=layout,
        duration=30.0,
        headline="Kai loses it over GTA cops",
        login="kaicenat",
        words=spoken("it was not one thing it was every single thing at once"),
        disclosure="#ad Paid partnership with @kaicenat",
    )
    assert {b.name for b in overlay.boxes} == {"headline", "credit", "disclosure", "caption"}
    assert all(b.inside(LOOK.safe_box) for b in overlay.boxes), overlay.boxes
    assert overlay.headline_fits and overlay.headline_lines == 2


def test_ass_file_structure_and_karaoke_events() -> None:
    overlay = ass.build_overlay(
        look=LOOK,
        layout="letterbox",
        duration=10.0,
        headline="Big {play}",
        login="alpha",
        words=(Word("hello", 1.0, 1.4), Word("there", 1.5, 1.9)),
    )
    text = overlay.ass
    assert "PlayResX: 1080" in text and "PlayResY: 1920" in text
    assert "\\fnMontserrat Black" in text and "\\fnMontserrat ExtraBold" in text
    assert "{play}" not in text and "(play)" in text  # braces can't open an override block
    assert "@alpha on Twitch" in text
    assert text.count("\\p1") == 2  # the white box and the key-word patch
    accent_events = [line for line in text.splitlines() if "\\fscx110" in line]
    assert len(accent_events) == 2  # each word is highlighted once
    assert "0:00:01.00,0:00:01.50" in accent_events[0] and "HELLO" in accent_events[0]
    assert "&H00D4FF&" in accent_events[0]  # #FFD400 in ASS colour order


def test_outline_headline_style_has_no_box() -> None:
    look = load_look(cfg.Config(look=cfg.LookConfig(headline_style="outline")))
    overlay = ass.build_overlay(
        look=look, layout="letterbox", duration=5.0, headline="Big play", login="a"
    )
    assert "\\p1" not in overlay.ass and "\\bord6" in overlay.ass


def test_ass_helpers() -> None:
    assert ass.ass_color("#FFD400", 0.8) == ("&H00D4FF&", "&H33&")
    assert ass.ass_time(3661.239) == "1:01:01.24"
    assert ass.rounded_rect(100, 40, 10).startswith("m 10.0 0 l 90.0 0 b")


# --- layouts ---------------------------------------------------------------------


def test_letterbox_geometry() -> None:
    plan = layouts.letterbox(SOURCE, LOOK)
    assert plan.video_area == (1080, 608, 0, 600)
    assert "scale=1080:608" in plan.video_filter and "pad=1080:1920:0:600" in plan.video_filter
    square = layouts.letterbox(MediaInfo(1000, 1000, 30, 20, True, "h264"), LOOK)
    assert square.video_area[1] == 1080 and square.video_area[3] == 364  # centred on y 904
    blur = load_look()
    blur["letterbox"] = {"video_top": 600, "background": "blur"}
    assert "boxblur" in layouts.letterbox(SOURCE, blur).video_filter


def test_layout_choice_follows_the_spec_order() -> None:
    cam, tiny = Rect(40, 40, 400, 300), Rect(40, 40, 200, 150)

    def choose(**kw):
        base = {"override": None, "has_portrait": False, "cam": None, "face_in_cam": False}
        return layouts.choose_layout(look=LOOK, **{**base, **kw})

    assert choose(has_portrait=True, cam=cam, face_in_cam=True) == "native"
    assert choose(cam=cam, face_in_cam=True) == "split"
    assert choose(cam=cam, face_in_cam=False) == "letterbox"  # face not in the cam this clip
    assert choose(cam=tiny, face_in_cam=True) == "letterbox"  # under 320 px: too blurry
    assert choose() == "letterbox"
    assert choose(override="letterbox", has_portrait=True) == "letterbox"
    assert choose(override="split", cam=tiny, face_in_cam=False) == "split"
    assert choose(override="split") == "letterbox" and choose(override="native") == "letterbox"


def test_split_geometry_puts_the_face_near_the_target() -> None:
    info = MediaInfo(1920, 1080, 60, 30, True, "h264")
    cam = Rect(1500, 700, 400, 300)
    crop = layouts.cam_crop(cam, (1700, 820), info, LOOK)
    assert crop.w / crop.h == pytest.approx(1080 / 840, rel=0.01)
    assert crop.x >= 0 and crop.y >= 0 and crop.x + crop.w <= 1920 and crop.y + crop.h <= 1080
    scale = 840 / crop.h
    face_on_frame = 1080 + (820 - crop.y) * scale
    assert abs(face_on_frame - 1250) < 60
    plan = layouts.split(info, cam, None, (1700, 820), LOOK)
    assert "vstack" in plan.video_filter and "scale=1080:1080" in plan.video_filter
    assert "scale=1080:840" in plan.video_filter and plan.layout == "split"
    assert "crop=1080:1920" in layouts.native(LOOK).video_filter


def test_rect_json_roundtrip_and_bad_values() -> None:
    assert Rect.from_json(Rect(1, 2, 3, 4).to_json()) == Rect(1, 2, 3, 4)
    for bad in (None, "", "nope", "[1, 2]", "[0, 0, 0, 5]"):
        assert Rect.from_json(bad) is None


# --- facecam logic (synthetic frames) --------------------------------------------


def test_find_cam_rect_needs_a_steady_off_centre_face() -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from clipvault.render import facecam

    rng = np.random.default_rng(1)
    frames = []
    for _ in range(10):
        frame = rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8)
        frame[500:700, 40:300] = 30  # a dark cam panel with a hard border
        frames.append(frame)
    face = (140.0, 560.0, 60.0, 70.0)
    cam = facecam.find_cam_rect(frames, [[face]] * 10)
    assert cam is not None and cam.contains(170, 595)
    assert abs(cam.x - 40) <= 6 and abs((cam.x + cam.w) - 300) <= 6  # snapped to the border

    assert facecam.find_cam_rect(frames, [[face]] * 3 + [[]] * 7) is None  # seen too rarely
    jumping = [[(100.0 + 90 * i, 100.0, 60.0, 70.0)] for i in range(10)]
    assert facecam.find_cam_rect(frames, jumping) is None
    centred = (610.0, 330.0, 60.0, 70.0)
    assert facecam.find_cam_rect(frames, [[centred]] * 10) is None
    assert facecam.is_big_centred((440.0, 160.0, 400.0, 400.0), 1280, 720)
    assert not facecam.is_big_centred(face, 1280, 720)


# --- posting filter and score ------------------------------------------------------


def test_posting_verdict_rules() -> None:
    assert flt.posting_verdict(14.9, 0.0, 2.0) == flt.Verdict(False, "too_short")
    assert flt.posting_verdict(30, 0.51, 2.0) == flt.Verdict(False, "music")
    assert flt.posting_verdict(75, 0.2, 2.0).ok  # long clips are trimmed, not refused
    assert flt.posting_verdict(30, 0.2, 0.3).low_priority
    unknown = flt.posting_verdict(30, None, 2.0)
    assert unknown.ok and unknown.music_unchecked


def test_score_formula() -> None:
    made = NOW - timedelta(hours=4)
    assert flt.view_velocity(400, made, NOW) == 100
    assert flt.view_velocity(50, NOW, NOW) == 50  # under an hour old counts as one hour
    assert flt.token_similarity("we won the game", "the game we lost") == pytest.approx(3 / 5)
    assert flt.novelty("we won the game", ["totally different words", "we won the game"]) == 0.0
    assert flt.novelty("fresh", []) == 1.0
    assert flt.clip_score(100, 1.5, 1.0) == pytest.approx(100.0)
    assert flt.clip_score(100, 9.0, 0.5) == pytest.approx(75.0)  # speech density caps at 3 wps
    assert flt.clip_score(100, None, 1.0) == pytest.approx(50.0)


# --- QA --------------------------------------------------------------------------


@pytest.fixture
def fake_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace every FFmpeg call the engine and QA make with controllable stand-ins."""
    state = {
        "source": SOURCE,
        "output_duration": None,
        "loudness": "-14.2",
        "black": [],
        "frozen": [],
        "encodes": [],
    }

    def probe(path: Path) -> MediaInfo:
        if "_9x16" in path.name:
            duration = state["output_duration"] or min(state["source"].duration, 59.0)
            return MediaInfo(1080, 1920, 60.0, duration, True, "h264")
        return state["source"]

    def encode(**kwargs) -> None:
        state["encodes"].append(kwargs)
        assert (kwargs["workdir"] / "overlay.ass").is_file()
        assert (kwargs["workdir"] / "fonts" / "Montserrat-Black.ttf").is_file()
        kwargs["dest"].write_bytes(b"vertical video")

    def loudness(path: Path, start: float = 0.0, duration: float | None = None):
        level = "-20.0" if "_9x16" not in path.name else state["loudness"]
        if level is None:
            return None
        return {
            "input_i": level, "input_tp": "-3", "input_lra": "5",
            "input_thresh": "-30", "target_offset": "0.1",
        }  # fmt: skip

    monkeypatch.setattr(ffmpeg, "probe", probe)
    monkeypatch.setattr(ffmpeg, "encode", encode)
    monkeypatch.setattr(ffmpeg, "measure_loudness", loudness)
    monkeypatch.setattr(
        ffmpeg, "find_black_and_frozen", lambda *a, **k: (state["black"], state["frozen"])
    )
    monkeypatch.setattr(qa, "save_preview_still", lambda output, dest, look, second: dest)
    return state


def render_ready(conn: sqlite3.Connection, home: Path, text: str = "it was not one thing at all"):
    """A saved, transcribed clip in a temp library."""
    config = make_config(home)
    add_streamer(conn, "alpha")
    add_clip(conn, "c1", title="\U0001f525 Big play!!", duration=30.0)
    jobs.run_fetch(conn, config, now=NOW, download=FakeDownloader())
    video = config.library_root / clip_row(conn, "c1")["file_path"]
    transcript = Transcript(text, spoken(text), "en", "local")
    video.with_suffix(".words.json").write_text(transcript.to_json(), encoding="utf-8")
    return config, video


def test_render_writes_the_vertical_file_and_records_a_passing_report(
    conn: sqlite3.Connection, home: Path, fake_ffmpeg: dict
) -> None:
    config, video = render_ready(conn, home)
    result = engine.render_clip(conn, config, "c1")
    assert result.layout == "letterbox" and result.report.passed, result.report.failures()
    assert result.headline == "Big play!!"  # emoji stripped from the title
    assert result.path == video.parent / "vertical" / (video.stem + "_9x16.mp4")
    assert result.path.read_bytes() == b"vertical video"
    assert not list(result.path.parent.glob("*.rendering.mp4"))

    row = conn.execute("SELECT * FROM renders").fetchone()
    assert row["path"] == clip_row(conn, "c1")["file_path"].replace(
        "/2026-11/", "/2026-11/vertical/"
    ).replace(".mp4", "_9x16.mp4")
    assert (row["layout"], row["qa_passed"], row["headline"]) == ("letterbox", 1, "Big play!!")
    names = [c["name"] for c in json.loads(row["qa_report_json"])["checks"]]
    assert names == [
        "duration", "safe_box", "headline_fit", "loudness", "black_or_frozen",
        "caption_time", "blocklist", "preview_still",
    ]  # fmt: skip
    encode = fake_ffmpeg["encodes"][0]
    assert encode["start"] == 0 and encode["duration"] == 30.0
    assert (
        "loudnorm=I=-14" in encode["audio_filter"] and "measured_I=-20.0" in encode["audio_filter"]
    )

    again = engine.render_clip(conn, config, "c1", headline="New headline")
    assert again.render_id == result.render_id  # same row, no duplicate
    assert conn.execute("SELECT count(*), max(headline) FROM renders").fetchone()[:] == (
        1,
        "New headline",
    )


def test_long_clips_keep_their_last_59_seconds(
    conn: sqlite3.Connection, home: Path, fake_ffmpeg
) -> None:
    config, video = render_ready(conn, home)
    fake_ffmpeg["source"] = MediaInfo(1280, 720, 60.0, 80.0, True, "hevc")
    words = (Word("early", 5.0, 5.4), Word("late", 30.0, 30.4))
    video.with_suffix(".words.json").write_text(
        Transcript("early late", words, "en", "local").to_json(), encoding="utf-8"
    )
    result = engine.render_clip(conn, config, "c1")
    encode = fake_ffmpeg["encodes"][0]
    assert (encode["start"], encode["duration"]) == (21.0, 59.0)
    assert result.report.passed
    overlay_text = [c.detail for c in result.report.checks if c.name == "caption_time"][0]
    assert overlay_text.startswith("1 caption")  # "early" fell before the trimmed start


def test_qa_catches_each_kind_of_problem(conn: sqlite3.Connection, home: Path, fake_ffmpeg) -> None:
    config, _video = render_ready(conn, home)

    def failed(**kwargs) -> set[str]:
        result = engine.render_clip(conn, config, "c1", **kwargs)
        return {check.name for check in result.report.failures()}

    assert failed() == set()
    fake_ffmpeg["output_duration"] = 9.0
    assert failed() == {"duration"}
    fake_ffmpeg["output_duration"] = None
    fake_ffmpeg["loudness"] = "-19.0"
    assert failed() == {"loudness"}
    fake_ffmpeg["loudness"] = None  # a silent clip has nothing to measure
    assert failed() == set()
    fake_ffmpeg["loudness"] = "-14.0"
    fake_ffmpeg["frozen"] = [2.5]
    assert failed() == {"black_or_frozen"}
    fake_ffmpeg["frozen"] = []
    assert failed(headline="antidisestablishmentarianism " * 3) == {"headline_fit", "safe_box"}
    assert failed(headline="BREAKING he got banned") == {"blocklist"}
    assert clip_row(conn, "c1")["state"] == "fetched"  # a manual render doesn't move the clip


def test_hype_words_are_fine_when_actually_said() -> None:
    assert qa.blocked_words("He got BANNED live", None, "oh no i got banned") == []
    assert qa.blocked_words("He got BANNED live", None, "nothing of the sort") == ["banned"]
    assert qa.blocked_words("what a frick", mask_pattern(["frick"])) == ["frick"]


def test_safe_box_check_fails_when_the_look_pushes_text_out(
    conn: sqlite3.Connection, home: Path, fake_ffmpeg
) -> None:
    config, _video = render_ready(conn, home)
    look = load_look(config)
    look["caption"] = {
        **look["caption"],
        "y_center": {"letterbox": 1320, "split": 1080, "native": 1240},
    }
    result = engine.render_clip(conn, config, "c1", look=look)
    assert [c.name for c in result.report.failures()] == ["safe_box"]


def test_render_errors_are_clear(conn: sqlite3.Connection, home: Path, fake_ffmpeg) -> None:
    config, video = render_ready(conn, home)
    with pytest.raises(RenderError, match="no clip with id"):
        engine.render_clip(conn, config, "missing")
    video.unlink()
    with pytest.raises(RenderError, match="not in the library"):
        engine.render_clip(conn, config, "c1")


def test_default_headline_and_vertical_path() -> None:
    assert engine.default_headline("\U0001f991LIVE\U0001f991DRAMA\U0001f991 ok") == "LIVE DRAMA ok"
    assert engine.default_headline("???") is None and engine.default_headline(None) is None
    assert engine.default_headline("a b c d e f g h i j") == "a b c d e f g"
    assert engine.vertical_relpath("alpha/2026-11/x_y.mp4") == "alpha/2026-11/vertical/x_y_9x16.mp4"


def test_cli_render_reports_results(home: Path, capsys, fake_ffmpeg, monkeypatch) -> None:
    library = home / "lib"
    (home / "config.yaml").write_text(f"library_root: '{library.as_posix()}'\n", encoding="utf-8")
    from clipvault import db

    conn = db.open_db(cfg.db_path())
    add_streamer(conn, "alpha")
    add_clip(conn, "long-enough", duration=30.0, view_count=9)
    add_clip(conn, "too-short", duration=8.0, view_count=99)
    jobs.run_fetch(conn, cfg.load_config(), now=NOW, download=FakeDownloader())
    conn.close()
    assert main(["render", "--layout", "letterbox"]) == 0
    out = capsys.readouterr().out
    assert "[1/1] letterbox QA passed" in out and "_9x16.mp4" in out
    assert main(["render"]) == 0
    assert "Nothing to render" in capsys.readouterr().out
    assert main(["render", "nope"]) == 1
    assert "FAILED - no clip with id nope" in capsys.readouterr().out
