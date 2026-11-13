from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from clipvault import config as cfg
from clipvault import db, jobs
from clipvault.process import write
from tests.helpers import add_clip, add_streamer, make_config

FIXTURE = Path(__file__).parent / "fixtures" / "groq" / "chat_completion.json"
FACTS = write.ClipFacts(
    login="kaicenat",
    display_name="KaiCenat",
    title="kai vs the cops",
    game="Grand Theft Auto V",
    transcript=[(0.0, "no no no they found me"), (3.5, "chat I'm cooked")],
)
GOOD = {"headline": "Kai Loses It Over Cops", "post_caption": "It went badly.",
        "hashtags": ["gta", "twitch", "funny"]}  # fmt: skip


class Scripted:
    """A provider that answers from a list, recording what it was sent."""

    name = "scripted"

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.sent: list[list[dict]] = []

    def complete(self, messages):
        self.sent.append(messages)
        return self.answers.pop(0)


def test_a_good_answer_is_tidied_and_credited() -> None:
    data = {**GOOD, "hashtags": ["#GTA", "twitch", "Funny", "gta"]}
    copy, problems = write.check_copy(data, FACTS)
    assert problems == []
    assert copy.headline == "Kai Loses It Over Cops"
    assert copy.post_caption == "It went badly. @kaicenat on Twitch"
    assert copy.hashtags == ("gta", "twitch", "funny")


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"headline": "Kai completely loses it over the cops"}, "7 words"),
        ({"headline": "Kai Loses It 😂"}, "emoji"),
        ({"headline": "BREAKING: Kai Arrested"}, "BREAKING"),
        ({"hashtags": ["gta", "two words"]}, "hashtags"),
        ({"headline": ""}, "headline missing"),
        ({"hashtags": "gta"}, "hashtags"),
    ],
)
def test_rule_breaking_answers_are_refused(change, problem) -> None:
    copy, problems = write.check_copy({**GOOD, **change}, FACTS)
    assert copy is None and any(problem in p for p in problems)


def test_framing_words_are_fine_when_actually_said() -> None:
    said = write.ClipFacts("x", "X", "he got banned live", transcript=[(0, "I am banned")])
    copy, _ = write.check_copy({**GOOD, "headline": "Streamer Gets Banned Live"}, said)
    assert copy is not None


def test_blocked_words_headline_fit_and_disclosure() -> None:
    import re

    blocked = re.compile(r"\bcops\b", re.I)
    assert write.check_copy(GOOD, FACTS, blocked=blocked)[0] is None
    copy, problems = write.check_copy(GOOD, FACTS, fits=lambda text: False)
    assert copy is None and "too long to fit" in problems[0]
    campaign = write.ClipFacts("kai", "Kai", "t", disclosure="#ad Paid partnership with @kai")
    copy, _ = write.check_copy(GOOD, campaign)
    assert copy.post_caption.startswith("#ad Paid partnership with @kai\n")


def test_bad_answers_are_asked_again_with_the_reasons() -> None:
    provider = Scripted("not json", json.dumps({**GOOD, "headline": "a b c d e f g h"}),
                        json.dumps(GOOD))  # fmt: skip
    copy, problems = write.write_copy(FACTS, provider)
    assert copy.headline == GOOD["headline"] and copy.provider == "scripted"
    assert len(provider.sent) == 3
    assert "8 words" in provider.sent[2][-1]["content"]
    transcript_message = provider.sent[0][1]["content"]
    assert (
        "[0:03] chat I'm cooked" in transcript_message
        and "@kaicenat on Twitch" in transcript_message
    )


def test_three_bad_answers_give_up() -> None:
    provider = Scripted(*(["{}"] * 3))
    copy, problems = write.write_copy(FACTS, provider)
    assert copy is None and problems


def test_groq_request_and_answer_shape() -> None:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, content=FIXTURE.read_bytes())

    http = httpx.Client(transport=httpx.MockTransport(handle))
    provider = write.OpenAIStyleProvider(api_key="k", http=http)
    copy, _ = write.write_copy(FACTS, provider)
    assert copy.headline == "Kai Loses It Over Cops"
    assert copy.hashtags == ("gta", "kaicenat", "twitch", "funny")
    body = json.loads(sent[0].content)
    assert sent[0].url == "https://api.groq.com/openai/v1/chat/completions"
    assert sent[0].headers["Authorization"] == "Bearer k"
    assert body["model"] == "openai/gpt-oss-20b" and "max_completion_tokens" in body
    assert body["response_format"]["json_schema"]["strict"] is True
    other = write.OpenAIStyleProvider(api_key="k", model="llama-3.3-70b-versatile", http=http)
    other.complete([{"role": "user", "content": "x"}])
    assert json.loads(sent[-1].content)["response_format"] == {"type": "json_object"}


def test_rate_limits_are_waited_out_and_bad_keys_reported() -> None:
    answers = [httpx.Response(429, headers={"retry-after": "2"}),
               httpx.Response(200, content=FIXTURE.read_bytes())]  # fmt: skip
    waits: list[float] = []
    http = httpx.Client(transport=httpx.MockTransport(lambda r: answers.pop(0)))
    provider = write.OpenAIStyleProvider(api_key="k", http=http, sleep=waits.append)
    assert "headline" in provider.complete([])
    assert waits == [2.0]
    refused = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(write.LLMError, match="refused the API key"):
        write.OpenAIStyleProvider(api_key="bad", http=refused).complete([])


def test_no_key_means_no_provider() -> None:
    assert write.build_provider(cfg.Config()) is None
    cfg.set_secret("groq_api_key", "gsk_test")
    assert write.build_provider(cfg.Config()).model == "openai/gpt-oss-20b"


def test_written_copy_is_stored_on_the_clip(conn, home) -> None:
    add_streamer(conn, "alpha")
    add_clip(conn, "c1", state="transcribed", file_path="alpha/c1.mp4", speech_wps=2.0)
    add_clip(conn, "c2", state="transcribed", file_path="alpha/c2.mp4", speech_wps=2.0)
    config = make_config(home)
    summary = jobs.run_write(conn, config, Scripted(json.dumps(GOOD), *(["{}"] * 3)))
    assert summary.written == 1 and summary.unusable == 1
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM clips")}
    written = [r for r in rows.values() if r["headline"]][0]
    assert written["state"] == "written" and written["copy_provider"] == "scripted"
    assert json.loads(written["hashtags_json"]) == ["gta", "twitch", "funny"]
    other = [r for r in rows.values() if not r["headline"]][0]
    assert other["state"] == "transcribed"  # archive use: kept, with its stand-in headline


def test_posting_pipeline_rejects_clips_without_a_usable_headline(conn, home) -> None:
    add_streamer(conn, "alpha")
    add_clip(conn, "c1", state="transcribed", file_path="alpha/c1.mp4")
    copy = jobs.write_for_clip(conn, make_config(home), "c1", Scripted(*(["{}"] * 3)),
                               reject_on_failure=True)  # fmt: skip
    row = conn.execute("SELECT state, reject_reason FROM clips").fetchone()
    assert copy is None and tuple(row) == ("rejected", "headline")


def test_an_unreachable_model_stops_the_run(conn, home) -> None:
    class Down:
        name = "down"

        def complete(self, messages):
            raise write.LLMError("could not reach down")

    add_streamer(conn, "alpha")
    add_clip(conn, "c1", state="transcribed", file_path="alpha/c1.mp4")
    add_clip(conn, "c2", state="transcribed", file_path="alpha/c2.mp4")
    summary = jobs.run_write(conn, make_config(home), Down())
    assert summary.errors == ["could not reach down"] and summary.written == 0
    db.open_db(home / "clipvault.db").close()
