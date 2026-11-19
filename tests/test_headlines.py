"""Hook headlines: candidates, choosing one that fits whole, AI, the user's own, re-making."""

from __future__ import annotations

import json

import pytest

from clipvault import jobs
from clipvault.process import headlines, write
from clipvault.process.headlines import ClipSignals, Headline
from clipvault.render.ass import layout_headline
from clipvault.render.template import load_look
from tests.helpers import add_clip, add_streamer, make_config
from tests.test_web import csrf, finish_setup, login

LOOK = load_look(make_config(__import__("pathlib").Path(".")))


def size_of(text: str) -> float | None:
    layout = layout_headline(text, LOOK)
    return layout.size if layout.fits else None


def signals(**overrides) -> ClipSignals:
    values = {"clip_id": "c1", "login": "kaicenat", "name": "KaiCenat"}
    return ClipSignals(**{**values, **overrides})


def best(sig: ClipSignals) -> Headline:
    return headlines.choose(headlines.offline_candidates(sig), sig, size_of)[0]


# --- what the built-in writer offers ---------------------------------------------------------


def test_a_viewer_title_that_says_something_is_used_whole() -> None:
    chosen = best(signals(login="valkyrae", name="Valkyrae",
                          title="ray visits her dad one last time"))  # fmt: skip
    assert chosen.kind == "title" and chosen.text == "Ray visits her dad one last time"
    assert chosen.key_word == "visits"


@pytest.mark.parametrize("title", ["uhuhu", "kai", "gtavi", "kai playing Gta", "W W W marathon"])
def test_titles_that_say_nothing_are_skipped(title) -> None:
    assert headlines.title_candidate(signals(title=title, game="Grand Theft Auto V")) is None


def test_a_short_title_works_as_an_exclamation_and_chat_slang_is_dropped() -> None:
    landmine = headlines.title_candidate(signals(login="xqc", name="xQc", title="A LANDMINE????"))
    assert (landmine.text, landmine.key_word) == ("A LANDMINE?", "LANDMINE")
    assert headlines.title_candidate(signals(title="druski outfit")) is None  # no punch
    sleep = headlines.title_candidate(signals(title="Kai Sleep talking xdd KEKW"))
    assert sleep.text == "Kai Sleep talking"


def test_a_copied_stream_title_or_a_chat_moment_title_is_skipped() -> None:
    stream_title = "LIVE CLICK HERE LOCK IN DRAMA NEWS"
    assert headlines.title_candidate(signals(title=stream_title, title_shared=True)) is None
    moment = signals(title="Live chat moment at 4:27:29 - big stream day", source="live_spike")
    assert headlines.title_candidate(moment) is None


def test_reactions_become_hooks_with_the_right_word_highlighted() -> None:
    chosen = best(signals(text="Wait wait. Oh my god. No way, no way bro."))
    assert chosen.kind == "reaction" and "Kai Cenat" in chosen.text or "READY" in chosen.text
    assert chosen.key_word and chosen.key_word.isupper()
    funny = best(signals(text="hahaha bro I'm dying, I can't breathe"))
    assert funny.pattern.startswith("reaction:funny")


def test_a_narrator_is_not_quoted_as_the_streamer() -> None:
    narration = "Dr. Tarrinos was arrested for the murder of Roxana Vargas on August 12."
    chosen = best(signals(login="xqc", name="xQc", text=narration, source="live_spike"))
    assert "arrested" not in chosen.text.lower() and "murder" not in chosen.text.lower()
    assert chosen.kind == "moment"  # it was found because chat went wild


def test_names_games_and_views() -> None:
    assert headlines.spoken_name("KaiCenat") == "Kai Cenat"
    assert headlines.spoken_name("xQc") == "xQc" and headlines.spoken_name("Valkyrae") == "Valkyrae"
    assert headlines.game_alias("Grand Theft Auto VI") == "GTA 6"
    assert headlines.game_alias("Just Chatting") == ""
    assert headlines.game_alias("The Legend of Zelda: Ocarina of Time") == ""
    popular = best(signals(views=2633, median_views=40))
    assert popular.kind == "views"


def test_a_headline_that_does_not_fit_whole_is_never_used() -> None:
    long_title = "Streamer accidentally deletes entire inventory again"
    sig = signals(title=long_title)
    ranked = headlines.choose(headlines.offline_candidates(sig), sig,
                              lambda text: None if text == long_title else 80.0)  # fmt: skip
    assert ranked and all(h.text != long_title for h in ranked)
    assert all(not long_title.startswith(h.text) for h in ranked)  # and never a cut-off copy


def test_fake_news_words_and_blocked_words_are_refused() -> None:
    sig = signals(title="streamer gets kicked from the server")
    assert "'BANNED' is not said in the clip" in headlines.problems("Kai BANNED for this", sig)
    said = signals(text="they banned me bro")
    assert headlines.problems("Kai BANNED for this", said) == []
    import re

    assert headlines.problems("bad word here", sig, re.compile("bad")) == ["a blocked word"]


def test_recent_patterns_are_not_repeated() -> None:
    sig = signals(source="live_spike", surge=5.0)
    first = best(sig)
    again = best(signals(source="live_spike", surge=5.0, recent=(first.pattern,)))
    assert again.pattern != first.pattern


# --- the clip in the database -----------------------------------------------------------------


@pytest.fixture
def saved(conn, home):
    add_streamer(conn, "alpha", display_name="Alpha")
    add_clip(conn, "c1", state="transcribed", file_path="alpha/c1.mp4",
             title="alpha finds the hidden room")  # fmt: skip
    return make_config(home)


def test_a_headline_is_written_once_and_kept(conn, saved) -> None:
    chosen = headlines.write(conn, saved, "c1", provider=None)
    row = conn.execute("SELECT * FROM clips WHERE id = 'c1'").fetchone()
    assert row["headline"] == chosen.text == "Alpha finds the hidden room"
    assert row["headline_source"] == "title|title" and row["headline_key"] == "finds"
    assert row["headline_version"] == headlines.current_version(conn)
    assert headlines.ensure(conn, saved, "c1").text == chosen.text


class Scripted:
    name = "scripted"

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.sent = []

    def complete(self, messages):
        self.sent.append(messages)
        return self.answers.pop(0)


def test_the_ai_writes_the_hook_when_a_key_is_stored(conn, saved) -> None:
    answer = {
        "headline": "Alpha found the SECRET room",
        "key_word": "SECRET",
        "post_caption": "What was behind the wall.",
        "hashtags": ["gta", "twitch", "secret"],
    }
    provider = Scripted(json.dumps(answer))
    chosen = headlines.write(conn, saved, "c1", provider=provider)
    assert (chosen.kind, chosen.text, chosen.key_word) == ("ai", answer["headline"], "SECRET")
    prompt = provider.sent[0][0]["content"]
    assert "Hype it up" in prompt and "narrator" in prompt


def test_without_the_ai_the_built_in_writer_steps_in(conn, saved) -> None:
    class Down:
        name = "down"

        def complete(self, messages):
            raise write.LLMError("could not reach down")

    chosen = headlines.write(conn, saved, "c1", provider=Down())
    assert chosen.kind == "title"


def test_the_users_own_headline_is_kept_and_checked(conn, saved) -> None:
    mine = headlines.set_custom(conn, "c1", "Alpha's BEST moment", LOOK)
    assert mine.key_word == "BEST" and headlines.ensure(conn, saved, "c1").kind == "custom"
    with pytest.raises(ValueError, match="too long"):
        headlines.set_custom(conn, "c1", "word " * 30, LOOK)
    with pytest.raises(ValueError, match="Emoji"):
        headlines.set_custom(conn, "c1", "Alpha \U0001f525 moment", LOOK)


def test_older_phone_versions_are_made_again_with_a_new_headline(conn, saved) -> None:
    conn.execute(
        "INSERT INTO renders (clip_id, layout, path, created_at) "
        "VALUES ('c1', 'split', 'alpha/vertical/c1_9x16.mp4', '2026-11-10T00:00:00Z')"
    )
    add_clip(conn, "c2", state="transcribed", file_path="alpha/c2.mp4", headline="Typed by me",
             headline_source="custom|custom")  # fmt: skip
    conn.execute(
        "INSERT INTO renders (clip_id, layout, path, created_at) "
        "VALUES ('c2', 'letterbox', 'alpha/vertical/c2_9x16.mp4', '2026-11-10T00:00:00Z')"
    )
    assert jobs.clips_to_refresh(conn) == [("c1", "split")]  # the typed one is left alone
    made = []
    summary = jobs.run_render(
        conn, saved, limit=3, render=lambda conn, config, clip_id, **kw: made.append((clip_id, kw))
    )
    assert made == [("c1", {"layout": "split"})] and summary.refreshed == 1
    headlines.write(conn, saved, "c1", provider=None)
    assert jobs.clips_to_refresh(conn) == []
    headlines.ai_key_added(conn)  # a new AI key: written again, by the AI
    assert jobs.clips_to_refresh(conn) == [("c1", "split")]


# --- the panel ------------------------------------------------------------------------------------


def test_the_clip_page_can_try_another_or_take_a_typed_headline(
    client, conn, home, library, monkeypatch
) -> None:
    from clipvault.web import server
    from tests.test_web import saved_clip

    finish_setup(home)
    login(client)
    add_streamer(conn, "alpha")
    saved_clip(conn, library, "c1", title="alpha finds the hidden room")
    started = []
    monkeypatch.setattr(server, "_render", lambda state, home, clip_id, layout:
                        (started.append(clip_id), state.rendering.discard(clip_id)))  # fmt: skip
    token = csrf(client, "/clips/c1")
    first = client.post("/clips/c1/headline", data={"csrf": token, "action": "another"})
    assert first.status_code == 303 and started == ["c1"]
    one = conn.execute("SELECT headline FROM clips WHERE id = 'c1'").fetchone()[0]
    client.post("/clips/c1/headline", data={"csrf": token, "action": "another"})
    two = conn.execute("SELECT headline FROM clips WHERE id = 'c1'").fetchone()[0]
    assert one != two
    client.post("/clips/c1/headline", data={"csrf": token, "action": "save",
                                            "text": "Alpha found it FIRST"})  # fmt: skip
    row = conn.execute("SELECT headline, headline_source FROM clips WHERE id = 'c1'").fetchone()
    assert tuple(row) == ("Alpha found it FIRST", "custom|custom") and len(started) == 3
    too_long = client.post("/clips/c1/headline", data={"csrf": token, "action": "save",
                                                       "text": "word " * 30})  # fmt: skip
    assert (
        "too+long" in too_long.headers["location"] or "too%20long" in too_long.headers["location"]
    )


def test_an_ai_key_is_checked_and_saved_from_settings(client, conn, home, monkeypatch) -> None:
    from clipvault import config as cfg
    from clipvault.web import settings_routes

    finish_setup(home)
    login(client)
    token = csrf(client)
    monkeypatch.setattr(settings_routes, "_check_ai_key", lambda key: "refused")
    client.post("/settings/ai-key", data={"csrf": token, "key": "gsk_bad"})
    assert cfg.get_secret("llm_api_key") is None
    monkeypatch.setattr(settings_routes, "_check_ai_key", lambda key: "ok")
    before = headlines.current_version(conn)
    client.post("/settings/ai-key", data={"csrf": token, "key": "gsk_good"})
    assert cfg.get_secret("llm_api_key") == "gsk_good"
    assert headlines.current_version(conn) == before + 1
    assert "Remove Key" in client.get("/settings").text
    client.post("/settings/ai-key/remove", data={"csrf": token})
    assert cfg.get_secret("llm_api_key") is None
