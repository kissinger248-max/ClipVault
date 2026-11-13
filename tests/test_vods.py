from __future__ import annotations

import gzip
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clipvault import jobs
from clipvault.discover import vods
from clipvault.twitch.api import TwitchClient
from clipvault.twitch.auth import AppTokenProvider
from clipvault.twitch.chat_replay import ChatUnavailable
from tests.helpers import add_streamer, make_config
from tests.twitch_fake import FakeTwitch, make_clip

NOW = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "chat" / "vod_chat.json"
TOOL = Path("TwitchDownloaderCLI.exe")


@pytest.fixture
def fake() -> FakeTwitch:
    twitch = FakeTwitch()
    twitch.add_user("1001", "alpha")
    return twitch


@pytest.fixture
def client(fake) -> TwitchClient:
    http = fake.client()
    return TwitchClient("cid", AppTokenProvider("cid", "secret", http), http, sleep=lambda s: None)


@pytest.fixture
def library(home) -> Path:
    root = home / "library"
    root.mkdir()
    (root / ".clipvault-library").write_text("ClipVault library\n")
    return root


def streamer(conn, **kw) -> int:
    return add_streamer(conn, "alpha", twitch_user_id="1001", **kw)


class ChatFiles:
    """Stands in for TwitchDownloaderCLI: copies the fixture chat, or fails as told."""

    def __init__(self, error: ChatUnavailable | None = None) -> None:
        self.error = error
        self.calls: list[str] = []
        self.pieces: list[tuple] = []

    def __call__(self, tool: Path, vod_id: str, dest: Path, begin=None, end=None) -> Path:
        self.calls.append(vod_id)
        self.pieces.append((dest.name, begin, end))
        if self.error:
            raise self.error
        dest.parent.mkdir(parents=True, exist_ok=True)
        with FIXTURE.open("rb") as source, gzip.open(dest, "wb") as target:
            shutil.copyfileobj(source, target)
        return dest


def test_finds_recent_finished_streams_only(conn, fake, client) -> None:
    streamer(conn)
    fake.add_video("v-old", "1001", "2026-11-01T20:00:00Z", "4h0m0s")
    fake.add_video("v-done", "1001", "2026-11-10T20:00:00Z", "40m0s")
    fake.add_video("v-live", "1001", "2026-11-12T16:00:00Z", "2h0m0s", stream_id="s-now")
    fake.live = {"1001": "s-now"}
    assert vods.find_vods(conn, client, now=NOW) == 1
    row = conn.execute("SELECT id, duration_s, state FROM vods").fetchone()
    assert tuple(row) == ("v-done", 2400, "found")
    assert vods.find_vods(conn, client, now=NOW) == 0  # already known
    fake.live = {}
    assert vods.find_vods(conn, client, now=NOW) == 1  # the stream ended


def test_streamers_with_vod_moments_off_are_left_out(conn, fake, client) -> None:
    streamer(conn, vod_moments=0)
    fake.add_video("v1", "1001", "2026-11-10T20:00:00Z", "40m0s")
    assert vods.find_vods(conn, client, now=NOW) == 0


def test_chat_moments_become_clips_and_clipped_ones_are_skipped(
    conn, fake, client, home, library
) -> None:
    streamer(conn)
    fake.add_video("v1", "1001", "2026-11-10T20:00:00Z", "40m0s")
    # A viewer clip with only 3 views covers the 35:08 burst: it still counts as clipped.
    fake.clips.append(
        make_clip("seen", "1001", "2026-11-10T20:35:10Z", 3, video_id="v1", vod_offset=2085)
    )
    vods.find_vods(conn, client, now=NOW)
    chat = ChatFiles()
    config = make_config(home)
    summary = vods.process_vods(conn, config, TOOL, client=client, now=NOW, download_chat=chat)
    assert (summary.processed, summary.moments) == (1, 2)
    clips = conn.execute("SELECT * FROM clips ORDER BY vod_offset").fetchall()
    assert [c["id"] for c in clips] == ["vod:v1:885", "vod:v1:1485"]
    first = clips[0]
    assert first["source"] == "vod_spike" and first["duration"] == 40
    assert first["view_count"] is None and first["state"] == "found"
    assert first["url"] == "https://www.twitch.tv/videos/v1"
    assert first["created_at"] == "2026-11-10T20:14:45Z"
    assert first["title"].startswith("Chat moment at 0:15:")
    vod = conn.execute("SELECT * FROM vods").fetchone()
    assert vod["state"] == "done" and vod["spikes"] == 3 and vod["moments"] == 2
    assert (library / vod["chat_path"]).is_file() and vod["chat_path"].startswith("alpha/chat/")
    # Running again changes nothing and downloads nothing.
    again = vods.process_vods(conn, config, TOOL, client=client, now=NOW, download_chat=chat)
    assert again.processed == 0 and chat.calls == ["v1"]


def test_keeps_only_the_streamers_number_of_moments(conn, fake, client, home, library) -> None:
    streamer(conn, vod_moments=1)
    fake.add_video("v1", "1001", "2026-11-10T20:00:00Z", "40m0s")
    vods.find_vods(conn, client, now=NOW)
    config = make_config(home)
    vods.process_vods(conn, config, TOOL, client=client, now=NOW, download_chat=ChatFiles())
    assert conn.execute("SELECT count(*) FROM clips").fetchone()[0] == 1


def test_subscriber_only_vods_are_skipped_with_the_reason(
    conn, fake, client, home, library
) -> None:
    streamer(conn)
    fake.add_video("v1", "1001", "2026-11-10T20:00:00Z", "40m0s")
    vods.find_vods(conn, client, now=NOW)
    gone = ChatFiles(ChatUnavailable("This video is subscriber-only", permanent=True))
    summary = vods.process_vods(conn, make_config(home), TOOL, now=NOW, download_chat=gone)
    assert summary.skipped == 1
    row = conn.execute("SELECT state, skip_reason FROM vods").fetchone()
    assert row["state"] == "skipped" and "subscriber-only" in row["skip_reason"]


def test_failed_chat_downloads_are_retried_then_given_up(conn, fake, client, home, library) -> None:
    streamer(conn)
    fake.add_video("v1", "1001", "2026-11-10T20:00:00Z", "40m0s")
    vods.find_vods(conn, client, now=NOW)
    flaky = ChatFiles(ChatUnavailable("connection reset"))
    config = make_config(home)
    when = NOW
    for _ in range(3):
        assert vods.process_vods(conn, config, TOOL, now=when, download_chat=flaky).failed == 1
        assert vods.process_vods(conn, config, TOOL, now=when, download_chat=flaky).waiting == 1
        when += timedelta(hours=9)
    vods.process_vods(conn, config, TOOL, now=when, download_chat=flaky)
    row = conn.execute("SELECT state, skip_reason, fail_count FROM vods").fetchone()
    assert row["state"] == "skipped" and row["fail_count"] == 4
    assert "gave up" in row["skip_reason"]


def test_fetch_cuts_moments_from_the_vod_and_flags_silence(conn, home, library) -> None:
    streamer(conn)
    conn.execute(
        "INSERT INTO clips (id, streamer_id, source, title, created_at, duration, vod_id, "
        "vod_offset, url, state) VALUES ('vod:v1:883', 1, 'vod_spike', 'Chat moment at 0:15:08', "
        "'2026-11-10T20:14:43Z', 40, 'v1', 883, 'https://www.twitch.tv/videos/v1', 'found')"
    )
    cuts: list[tuple] = []

    def cut(url, dest, quality, start, end):
        cuts.append((url, start, end))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 100)
        return dest

    def never(*a, **k):
        raise AssertionError("a VOD moment must not go through the clip downloader")

    config = make_config(home)
    waiting = jobs.run_fetch(conn, config, download=never, cut=cut, allow_vod_cuts=False, now=NOW)
    assert cuts == [] and waiting.downloaded == 0
    jobs.run_fetch(conn, config, download=never, cut=cut, is_silent=lambda p: True, now=NOW)
    assert cuts == [("https://www.twitch.tv/videos/v1", 883, 923)]
    row = conn.execute("SELECT state, reject_reason, file_path FROM clips").fetchone()
    assert row["state"] == "fetched" and row["reject_reason"] == "muted"
    assert "_chat_" in row["file_path"] and row["file_path"].endswith("vod-v1-883.mp4")


class FakeProcess:
    def __init__(self, returncode: int = 0, stderr: str = "", write: Path | None = None) -> None:
        self.returncode, self.stderr, self.stdout, self.write = returncode, stderr, "", write
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if self.write is not None:
            self.write.write_bytes(FIXTURE.read_bytes())
        return self


def test_section_download_asks_ytdlp_for_just_the_moment(tmp_path) -> None:
    from clipvault.fetch import ytdlp

    dest = tmp_path / "cut.mp4"
    runner = FakeProcess(write=dest)
    ytdlp.download_section("https://www.twitch.tv/videos/v1", dest, "720p", 885, 925, runner=runner)
    command = runner.commands[0]
    assert command[command.index("--download-sections") + 1] == "*885-925"
    gone = FakeProcess(returncode=1, stderr="ERROR: [twitch:vod] v1: Video does not exist")
    with pytest.raises(ytdlp.DownloadError) as caught:
        ytdlp.download_section("u", tmp_path / "x.mp4", "720p", 0, 40, runner=gone)
    assert caught.value.permanent


def test_chat_download_is_gzipped_into_place(tmp_path) -> None:
    from clipvault.twitch import chat_replay

    dest = tmp_path / "alpha" / "chat" / "2026-11-10_v1.json.gz"
    raw = dest.with_name("2026-11-10_v1.part.json")
    runner = FakeProcess(write=raw)
    dest.parent.mkdir(parents=True)
    assert chat_replay.download_chat(TOOL, "v1", dest, runner=runner) == dest
    assert not raw.exists() and len(chat_replay.load_chat(dest)) == 1119
    assert runner.commands[0][:4] == [str(TOOL), "chatdownload", "--id", "v1"]
    sub_only = FakeProcess(returncode=1, stderr="Error: video is subscriber-only")
    with pytest.raises(ChatUnavailable) as caught:
        chat_replay.download_chat(TOOL, "v2", tmp_path / "v2.json.gz", runner=sub_only)
    assert caught.value.permanent


def test_long_streams_are_read_in_four_hour_pieces(conn, fake, client, home, library) -> None:
    streamer(conn)
    fake.add_video("v9", "1001", "2026-11-10T20:00:00Z", "9h0m0s")
    vods.find_vods(conn, client, now=NOW)
    chat = ChatFiles()
    config = make_config(home)
    vods.process_vods(conn, config, TOOL, client=client, now=NOW, download_chat=chat)
    assert [(b, e) for _, b, e in chat.pieces] == [(0, 14400), (14400, 28800), (28800, 32400)]
    assert chat.pieces[1][0] == "2026-11-10_v9_part01.json.gz"
    vod = conn.execute("SELECT state, messages FROM vods").fetchone()
    assert vod["state"] == "done" and vod["messages"] == 3 * 1119
    assert vods.chat_pieces(3600) == [(None, None)]


def test_piece_offsets_counted_from_the_piece_are_moved_to_vod_time(tmp_path) -> None:
    from clipvault.twitch import chat_replay

    piece = tmp_path / "piece.json"
    piece.write_text('{"comments": [{"content_offset_seconds": 12, "message": {"body": "hi"}}]}')
    assert chat_replay.load_chat(piece, 14400)[0].offset == 14412
    absolute = tmp_path / "abs.json"
    absolute.write_text(
        '{"comments": [{"content_offset_seconds": 14412, "message": {"body": "x"}}]}'
    )
    assert chat_replay.load_chat(absolute, 14400)[0].offset == 14412


def test_chat_tool_progress_lines_are_not_used_as_the_error(tmp_path) -> None:
    from clipvault.twitch import chat_replay

    stopped = FakeProcess(returncode=1)
    stopped.stdout = "[STATUS] - Downloading 10%\r[STATUS] - Downloading 55%"
    with pytest.raises(ChatUnavailable) as caught:
        chat_replay.download_chat(TOOL, "v3", tmp_path / "v3.json.gz", runner=stopped)
    assert str(caught.value) == "TwitchDownloaderCLI stopped (exit code 1)"
