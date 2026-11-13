from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from clipvault import config as cfg
from clipvault import jobs
from clipvault.__main__ import main
from clipvault.library.search import search_transcripts
from clipvault.process import transcribe as tr
from clipvault.process.transcribe import Transcript, Word
from tests.helpers import FakeDownloader, add_clip, add_streamer, clip_row, make_config

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "groq" / "transcription_verbose.json"


def words(*items: tuple[str, float, float]) -> tuple[Word, ...]:
    return tuple(Word(*item) for item in items)


class FakeProvider:
    def __init__(self, name: str, text: str = "hello there general", fail: str | None = None):
        self.name, self.text, self.fail, self.calls = name, text, fail, 0

    def transcribe(self, audio: Path, language: str | None = None) -> Transcript:
        self.calls += 1
        if self.fail:
            raise tr.TranscribeError(self.fail)
        spoken = words(*[(w, i * 0.5, i * 0.5 + 0.4) for i, w in enumerate(self.text.split())])
        return Transcript(self.text, spoken, "en", self.name)


@pytest.fixture
def ffmpeg(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Stand-in for FFmpeg: writes an audio file and records the commands."""
    commands: list[list[str]] = []

    def run(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"flac")
        return SimpleNamespace(returncode=0, stderr="", stdout=b"")

    monkeypatch.setattr(tr, "_run", run)
    return commands


def saved(conn: sqlite3.Connection, home: Path, clips: dict[str, dict]):
    config = make_config(home)
    add_streamer(conn, "alpha")
    for clip_id, columns in clips.items():
        add_clip(conn, clip_id, **columns)
    jobs.run_fetch(conn, config, now=NOW, download=FakeDownloader())
    return config


# --- parsing and captions ------------------------------------------------------


def test_parse_groq_fixture_and_words_inside_segments() -> None:
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    parsed = tr.parse_groq(body)
    assert parsed.text == "No way, that was insane! Clip it."
    assert parsed.words[0] == Word("No", 0.32, 0.5) and len(parsed.words) == 7
    assert (parsed.language, parsed.provider) == ("English", "groq")

    nested = {"text": "hi", "segments": [{"words": [{"word": " hi", "start": 0, "end": 0.2}]}]}
    assert tr.parse_groq(nested).words == (Word("hi", 0.0, 0.2),)


def test_transcript_json_roundtrip() -> None:
    original = Transcript("café time", words(("café", 0.0, 0.4), ("time", 0.5, 0.9)), "en", "x")
    assert Transcript.from_json(original.to_json()) == original


def test_cues_break_on_pauses_sentences_and_length() -> None:
    spoken = words(
        ("No", 0.3, 0.5), ("way,", 0.5, 0.8), ("that", 0.9, 1.0), ("was", 1.0, 1.2),
        ("insane!", 1.2, 1.9), ("Clip", 1.95, 2.2), ("it", 2.2, 2.4),
        ("later", 3.5, 3.9),  # after a pause
    )  # fmt: skip
    cues = tr.cues_from_words(spoken)
    assert [c.text for c in cues] == ["No way, that was insane!", "Clip it", "later"]
    assert (cues[0].start, cues[0].end) == (0.3, 1.9)
    long_run = words(*[(f"word{i}", i * 0.3, i * 0.3 + 0.25) for i in range(30)])
    assert all(len(c.text) <= 42 for c in tr.cues_from_words(long_run))
    assert tr.cues_from_words(()) == []


def test_srt_format_and_masking() -> None:
    pattern = tr.mask_pattern(["darn", "heck*"])
    cues = [tr.Cue(0.0, 1.5, "well Darn it"), tr.Cue(3661.25, 3662.0, "heckin darns, darndest")]
    assert tr.to_srt(cues, pattern) == (
        "1\n00:00:00,000 --> 00:00:01,500\nwell **** it\n\n"
        "2\n01:01:01,250 --> 01:01:02,000\n**** ****, darndest\n"
    )
    assert tr.mask_text("unchanged", None) == "unchanged"


def test_mask_list_merges_the_user_file(home: Path) -> None:
    (home / "mask_words.txt").write_text("# mine\nFrick\n\n", encoding="utf-8")
    listed = tr.load_mask_words()
    assert "frick" in listed and len(listed) > 5
    assert tr.mask_text("oh frick", tr.mask_pattern(listed)) == "oh ****"


# --- audio and providers ---------------------------------------------------------


def test_extract_audio_command_and_errors(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    result = SimpleNamespace(returncode=0, stderr="")

    def run(command, **kwargs):
        seen.append(command)
        return result

    monkeypatch.setattr(tr, "_run", run)
    tr.extract_audio(home / "clip.mp4", home / "a.flac")
    command = seen[0]
    assert command[0] == "ffmpeg" and "16000" in command and "flac" in command
    assert command[command.index("-ac") + 1] == "1"

    result.returncode, result.stderr = 1, "Stream map '0:a:0' matches no streams."
    with pytest.raises(tr.NoAudio):
        tr.extract_audio(home / "clip.mp4", home / "a.flac")
    result.stderr = "Invalid data found when processing input"
    with pytest.raises(tr.TranscribeError, match="Invalid data"):
        tr.extract_audio(home / "clip.mp4", home / "a.flac")

    def boom(command, **kwargs):
        raise subprocess.TimeoutExpired("ffmpeg", 120)

    monkeypatch.setattr(tr, "_run", boom)
    with pytest.raises(tr.TranscribeError):
        tr.extract_audio(home / "clip.mp4", home / "a.flac")


def test_groq_request_and_error_handling(home: Path) -> None:
    audio = home / "audio.flac"
    audio.write_bytes(b"flac-bytes")
    seen: list[httpx.Request] = []
    answers = [
        httpx.Response(200, json=json.loads(FIXTURE.read_text(encoding="utf-8"))),
        httpx.Response(401, json={"error": {"message": "Invalid API Key", "type": "x"}}),
        httpx.ConnectError("offline"),
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    groq = tr.GroqTranscriber("key-123", httpx.Client(transport=httpx.MockTransport(handle)))
    assert len(groq.transcribe(audio, "en").words) == 7
    request = seen[0]
    assert str(request.url) == tr.GROQ_URL
    assert request.headers["Authorization"] == "Bearer key-123"
    body = request.read()
    for expected in (b"whisper-large-v3-turbo", b"verbose_json", b"timestamp_granularities[]"):
        assert expected in body
    assert b'name="language"' in body and b"flac-bytes" in body

    with pytest.raises(tr.TranscribeError, match="HTTP 401: Invalid API Key"):
        groq.transcribe(audio)
    with pytest.raises(tr.TranscribeError, match="unreachable"):
        groq.transcribe(audio)


def test_provider_choice_follows_the_setting_and_the_stored_key(memory_keyring, home: Path) -> None:
    def names(provider: str) -> list[str]:
        config = cfg.Config(transcription=cfg.TranscriptionConfig(provider=provider))
        return [p.name for p in tr.build_providers(config)]

    assert names("auto") == ["local"] and names("groq") == [] and names("local") == ["local"]
    memory_keyring.set_password("clipvault", "groq_api_key", "k")
    assert names("auto") == ["groq", "local"]
    assert names("groq") == ["groq"] and names("local") == ["local"]


def test_fallback_to_the_next_provider(home: Path) -> None:
    audio = home / "a.flac"
    down = FakeProvider("groq", fail="Groq is unreachable (ConnectError)")
    local = FakeProvider("local")
    assert tr.transcribe_audio(audio, [down, local]).provider == "local"
    assert (down.calls, local.calls) == (1, 1)
    with pytest.raises(tr.TranscribeError, match="unreachable"):
        tr.transcribe_audio(audio, [down])
    with pytest.raises(tr.TranscribeError, match="no speech-to-text provider"):
        tr.transcribe_audio(audio, [])


# --- the job and search ------------------------------------------------------------


def test_transcribe_job_writes_files_and_indexes(
    conn: sqlite3.Connection, home: Path, ffmpeg
) -> None:
    config = saved(conn, home, {"c1": {"duration": 10.0, "title": "Big play"}})
    provider = FakeProvider("local", "what a darn play that was")
    lines: list[str] = []
    summary = jobs.run_transcribe(
        conn, config, now=NOW, providers=[provider], on_progress=lines.append
    )
    assert (summary.done, summary.failed, summary.words) == (1, 0, 6)
    assert summary.providers_used == {"local": 1}

    row = clip_row(conn, "c1")
    assert (row["state"], row["speech_wps"], row["language"]) == ("transcribed", 0.6, "en")
    assert row["transcript_provider"] == "local"
    video = config.library_root / row["file_path"]
    srt = config.library_root / row["transcript_path"]
    assert srt == video.with_suffix(".srt") and "what a darn play that was" in srt.read_text(
        "utf-8"
    )
    stored = Transcript.from_json(video.with_suffix(".words.json").read_text("utf-8"))
    assert [w.text for w in stored.words] == "what a darn play that was".split()
    assert "6 words (local)" in lines[0]

    assert [r["id"] for r in search_transcripts(conn, "darn play")] == ["c1"]
    again = jobs.run_transcribe(conn, config, now=NOW, providers=[provider])
    assert (again.done, again.already_there, provider.calls) == (0, 0, 1)  # nothing left to do


def test_masked_words_are_hidden_in_subtitles_but_searchable(
    conn: sqlite3.Connection, home: Path, ffmpeg
) -> None:
    (home / "mask_words.txt").write_text("frick\n", encoding="utf-8")
    config = saved(conn, home, {"c1": {}})
    jobs.run_transcribe(conn, config, now=NOW, providers=[FakeProvider("local", "oh frick no")])
    row = clip_row(conn, "c1")
    assert "oh **** no" in (config.library_root / row["transcript_path"]).read_text("utf-8")
    assert len(search_transcripts(conn, "frick")) == 1


def test_existing_word_file_is_reused_after_a_crash(
    conn: sqlite3.Connection, home: Path, ffmpeg
) -> None:
    config = saved(conn, home, {"c1": {}})
    video = config.library_root / clip_row(conn, "c1")["file_path"]
    video.with_suffix(".words.json").write_text(
        Transcript(
            "saved earlier", words(("saved", 0, 0.3), ("earlier", 0.4, 0.8)), "en", "groq"
        ).to_json(),
        encoding="utf-8",
    )
    provider = FakeProvider("local")
    summary = jobs.run_transcribe(conn, config, now=NOW, providers=[provider])
    assert (summary.already_there, summary.done, provider.calls) == (1, 0, 0)
    assert clip_row(conn, "c1")["transcript_provider"] == "groq"
    assert len(search_transcripts(conn, "saved earlier")) == 1


def test_failures_retry_then_stop_the_run(conn: sqlite3.Connection, home: Path, ffmpeg) -> None:
    config = saved(conn, home, {f"c{i}": {"view_count": 100 - i} for i in range(5)})
    broken = FakeProvider("local", fail="model exploded")
    summary = jobs.run_transcribe(conn, config, now=NOW, providers=[broken])
    assert summary.stopped_on_failures and summary.failed == 3 and summary.remaining == 2
    row = clip_row(conn, "c0")
    assert (row["state"], row["fail_count"], row["last_error"]) == ("fetched", 1, "model exploded")
    waiting = jobs.run_transcribe(conn, config, now=NOW, providers=[FakeProvider("local")])
    assert (waiting.waiting, waiting.done) == (3, 2)
    later = jobs.run_transcribe(
        conn, config, now=NOW + timedelta(minutes=2), providers=[FakeProvider("local")]
    )
    assert later.done == 3 and clip_row(conn, "c0")["fail_count"] == 0


def test_clip_without_audio_and_missing_file(
    conn: sqlite3.Connection, home: Path, monkeypatch
) -> None:
    config = saved(conn, home, {"silent": {"view_count": 9}, "gone": {"view_count": 1}})
    (config.library_root / clip_row(conn, "gone")["file_path"]).unlink()

    def no_audio(command, **kwargs):
        return SimpleNamespace(returncode=1, stderr="Stream map '0:a:0' matches no streams.")

    monkeypatch.setattr(tr, "_run", no_audio)
    summary = jobs.run_transcribe(conn, config, now=NOW, providers=[FakeProvider("local")])
    assert (summary.done, summary.no_speech, summary.failed) == (1, 1, 0)
    row = clip_row(conn, "silent")
    assert (row["state"], row["speech_wps"], row["transcript_provider"]) == (
        "transcribed",
        0.0,
        "none",
    )
    assert clip_row(conn, "gone")["state"] == "fetched"  # skipped, not failed


def test_search_phrase_first_then_all_words_and_prefix(
    conn: sqlite3.Connection, home: Path
) -> None:
    saved(conn, home, {"a": {"title": "A"}, "b": {"title": "B"}, "c": {"title": "C"}})
    texts = {"a": "we won the game finally", "b": "the game we finally won", "c": "nothing here"}
    for clip_id, text in texts.items():
        conn.execute("INSERT INTO transcripts (clip_id, text) VALUES (?, ?)", (clip_id, text))

    exact = search_transcripts(conn, "won the game")
    assert [r["id"] for r in exact] == ["a"] and "[won the game]" in exact[0]["snippet"]
    assert len(search_transcripts(conn, "game, WON!")) == 2  # no exact phrase: all words
    assert len(search_transcripts(conn, "final")) == 2  # unfinished last word
    assert search_transcripts(conn, "absent") == [] and search_transcripts(conn, "  ?! ") == []
    conn.execute("UPDATE clips SET removed_at = '2026-11-12T00:00:00Z' WHERE id = 'a'")
    assert search_transcripts(conn, "won the game") != exact  # removed clips are not offered


def test_cli_transcribe_and_search(home: Path, capsys, monkeypatch, ffmpeg) -> None:
    library = home / "lib"
    (home / "config.yaml").write_text(f"library_root: '{library.as_posix()}'\n", encoding="utf-8")
    from clipvault import db

    conn = db.open_db(cfg.db_path())
    add_streamer(conn, "alpha")
    add_clip(conn, "c1", title="Big play")
    jobs.run_fetch(conn, cfg.load_config(), now=NOW, download=FakeDownloader())
    conn.close()
    monkeypatch.setattr(
        tr, "build_providers", lambda config: [FakeProvider("local", "that was a huge play")]
    )
    assert main(["transcribe"]) == 0
    out = capsys.readouterr().out
    assert "1 clip(s) transcribed (5 words; 1 by local)" in out
    assert main(["search", "huge", "play"]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "[huge play]" in out and "Big play" in out
    assert main(["search", "nothing like this"]) == 1

    (home / "config.yaml").write_text("transcription:\n  enabled: false\n", encoding="utf-8")
    assert main(["transcribe"]) == 0
    assert "switched off" in capsys.readouterr().out
