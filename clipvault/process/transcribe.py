"""Speech-to-text with word timings (SPEC §8).

Two providers behind one interface:

- ``GroqTranscriber``: Groq's hosted ``whisper-large-v3-turbo`` (paid, fast).
  Docs checked 2026-11-12: https://console.groq.com/docs/speech-to-text
- ``LocalTranscriber``: ``faster-whisper`` small/int8 on this computer (free, slower).

Providers are tried in order; when one fails the next takes over, so losing the
network or the Groq key never stops transcription.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from clipvault import config as cfg
from clipvault import procs
from clipvault.logging_setup import ctx

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
GROQ_MAX_BYTES = 25 * 1024 * 1024  # free-tier upload limit
LOCAL_MODEL = "small"
AUDIO_TIMEOUT_S = 120
MASK = "****"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Word:
    """One spoken word and when it is said, in seconds from the start of the clip."""

    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Transcript:
    """What was said in a clip."""

    text: str
    words: tuple[Word, ...]
    language: str | None
    provider: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "text": self.text,
                "language": self.language,
                "provider": self.provider,
                "words": [asdict(w) for w in self.words],
            },
            ensure_ascii=False,
            indent=1,
        )

    @classmethod
    def from_json(cls, raw: str) -> Transcript:
        data = json.loads(raw)
        words = tuple(Word(w["text"], float(w["start"]), float(w["end"])) for w in data["words"])
        return cls(data["text"], words, data.get("language"), data.get("provider", "unknown"))


class TranscribeError(Exception):
    """A provider could not transcribe the audio."""


class NoAudio(TranscribeError):
    """The clip has no audio track at all; there is nothing to transcribe."""


class Transcriber(Protocol):
    name: str

    def transcribe(self, audio: Path, language: str | None = None) -> Transcript: ...


# --- audio extraction ----------------------------------------------------------


def _run(*args: Any, **kwargs: Any) -> Any:
    """The one place this module starts a process (tests replace it).

    A timeout ends the whole tree the tool started (``procs.run``).
    """
    return procs.run(*args, **kwargs)


def extract_audio(video: Path, dest: Path) -> Path:
    """Write the clip's sound as 16 kHz mono FLAC (the format Groq recommends).

    Raises ``NoAudio`` if the file has no audio track, ``TranscribeError`` on
    any other FFmpeg failure.
    """
    command = [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-i", str(video), "-map", "0:a:0", "-ar", "16000", "-ac", "1", "-c:a", "flac", str(dest),
    ]  # fmt: skip
    try:
        result = _run(
            command,
            capture_output=True,
            text=True,
            timeout=AUDIO_TIMEOUT_S,
            encoding="utf-8",
            errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise TranscribeError(f"FFmpeg could not read the clip: {exc}") from exc
    if result.returncode != 0:
        message = (result.stderr or "").strip()[-300:]
        if "matches no streams" in message or "does not contain any stream" in message:
            raise NoAudio("the clip has no audio track")
        raise TranscribeError(f"FFmpeg could not extract audio: {message}")
    return dest


def load_samples(audio: Path) -> Any:
    """Decode an audio file to 16 kHz mono samples (a float32 numpy array) with FFmpeg.

    faster-whisper can decode files itself through PyAV, but that breaks whenever
    PyAV changes its API (it did with PyAV 19). FFmpeg is already required, so
    decoding here removes that dependency on PyAV's version.
    """
    import numpy as np

    command = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(audio),
        "-f", "s16le", "-ac", "1", "-ar", "16000", "-",
    ]  # fmt: skip
    try:
        result = _run(command, capture_output=True, timeout=AUDIO_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise TranscribeError(f"FFmpeg could not decode the audio: {exc}") from exc
    if result.returncode != 0:
        raise TranscribeError("FFmpeg could not decode the audio")
    return np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0


# --- providers -----------------------------------------------------------------


class GroqTranscriber:
    """Groq-hosted Whisper. One request per clip; billed per audio second (10 s minimum)."""

    name = "groq"

    def __init__(self, api_key: str, http: httpx.Client, model: str = GROQ_MODEL) -> None:
        self._api_key = api_key
        self._http = http
        self._model = model

    def transcribe(self, audio: Path, language: str | None = None) -> Transcript:
        if audio.stat().st_size > GROQ_MAX_BYTES:
            raise TranscribeError("audio file is over Groq's 25 MB upload limit")
        data = {
            "model": self._model,
            "response_format": "verbose_json",  # required for word timestamps
            "timestamp_granularities[]": "word",
            "temperature": "0",
        }
        if language:
            data["language"] = language
        try:
            with audio.open("rb") as handle:
                response = self._http.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    data=data,
                    files={"file": (audio.name, handle, "audio/flac")},
                    timeout=120,
                )
        except httpx.TransportError as exc:
            raise TranscribeError(f"Groq is unreachable ({type(exc).__name__})") from exc
        if response.status_code != 200:
            raise TranscribeError(f"Groq answered HTTP {response.status_code}: {_error(response)}")
        try:
            return parse_groq(response.json())
        except (ValueError, KeyError, TypeError) as exc:
            raise TranscribeError("Groq returned an unexpected response") from exc


def parse_groq(body: dict[str, Any]) -> Transcript:
    """Convert a Groq ``verbose_json`` response (words at the top level or inside segments)."""
    raw_words = body.get("words")
    if raw_words is None:
        raw_words = [w for seg in body.get("segments") or [] for w in seg.get("words") or []]
    words = tuple(
        Word(str(w["word"]).strip(), float(w["start"]), float(w["end"]))
        for w in raw_words
        if str(w.get("word", "")).strip()
    )
    return Transcript(str(body.get("text", "")).strip(), words, body.get("language"), "groq")


def _error(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["message"])[:200]
    except (ValueError, KeyError, TypeError):
        return ""


_models: dict[tuple[str, str], Any] = {}
_models_lock = threading.Lock()


def _whisper_model(size: str, compute_type: str) -> Any:
    """Load a faster-whisper model once per process, not on every scheduled round."""
    with _models_lock:
        key = (size, compute_type)
        if key not in _models:
            from faster_whisper import WhisperModel

            _models[key] = WhisperModel(size, device="cpu", compute_type=compute_type)
        return _models[key]


class LocalTranscriber:
    """faster-whisper on this computer's CPU. The model (about 500 MB) downloads on first use."""

    name = "local"

    def __init__(self, model_size: str = LOCAL_MODEL, compute_type: str = "int8") -> None:
        self._model_size = model_size
        self._compute_type = compute_type
        self._model: Any = None

    def transcribe(self, audio: Path, language: str | None = None) -> Transcript:
        try:
            if self._model is None:
                self._model = _whisper_model(self._model_size, self._compute_type)
            segments, info = self._model.transcribe(
                load_samples(audio),
                language=language,
                word_timestamps=True,
                vad_filter=True,
                beam_size=5,
            )
            words: list[Word] = []
            texts: list[str] = []
            for segment in segments:  # the work happens while iterating
                texts.append(segment.text.strip())
                for word in segment.words or []:
                    if word.word.strip():
                        words.append(Word(word.word.strip(), float(word.start), float(word.end)))
        except ImportError as exc:
            raise TranscribeError(
                'local speech-to-text is not installed; run: pip install -e ".[transcribe]"'
            ) from exc
        except Exception as exc:  # model download, decoding or audio errors
            raise TranscribeError(f"local speech-to-text failed: {exc}") from exc
        return Transcript(" ".join(t for t in texts if t), tuple(words), info.language, "local")


def build_providers(config: cfg.Config, http: httpx.Client | None = None) -> list[Transcriber]:
    """Providers to try, in order, for the ``transcription.provider`` setting.

    ``auto`` uses Groq when a key is stored and always keeps the local model as
    the fallback; ``groq`` and ``local`` force one provider.
    """
    choice = config.transcription.provider
    providers: list[Transcriber] = []
    key = cfg.get_secret("groq_api_key") if choice in ("auto", "groq") else None
    if key:
        providers.append(GroqTranscriber(key, http or httpx.Client()))
    if choice in ("auto", "local"):
        providers.append(LocalTranscriber())
    return providers


def transcribe_audio(
    audio: Path, providers: list[Transcriber], language: str | None = None
) -> Transcript:
    """Try each provider in turn; raise the last error if all of them fail."""
    if not providers:
        raise TranscribeError(
            "no speech-to-text provider is available: store a Groq key with "
            "'python -m clipvault secrets set groq_api_key --window' or set "
            "transcription.provider to 'local' or 'auto' in config.yaml"
        )
    last: TranscribeError | None = None
    for provider in providers:
        try:
            return provider.transcribe(audio, language)
        except TranscribeError as exc:
            last = exc
            log.warning(
                "speech-to-text provider failed, trying the next: %s",
                exc,
                extra=ctx(job="transcribe", provider=provider.name),
            )
    assert last is not None
    raise last


# --- captions ------------------------------------------------------------------


def load_mask_words(home: Path | None = None) -> list[str]:
    """Words to hide in captions: the bundled list plus ``mask_words.txt`` in the home folder."""
    from importlib import resources

    text = resources.files("clipvault.data").joinpath("mask_words.txt").read_text(encoding="utf-8")
    extra = (home or cfg.home_dir()) / "mask_words.txt"
    if extra.is_file():
        text += "\n" + extra.read_text(encoding="utf-8-sig")
    words = {line.split("#", 1)[0].strip().lower() for line in text.splitlines()}
    return sorted(w for w in words if w)


def mask_pattern(mask_words: list[str]) -> re.Pattern[str] | None:
    """Compile the mask list; a trailing ``*`` on an entry also matches longer forms."""
    if not mask_words:
        return None
    parts = [
        re.escape(w[:-1]) + r"\w*" if w.endswith("*") else re.escape(w) + r"(?:s|es)?"
        for w in mask_words
    ]
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)


def mask_text(text: str, pattern: re.Pattern[str] | None) -> str:
    """Replace masked words with ``****`` (caption text only; transcripts stay as spoken)."""
    return pattern.sub(MASK, text) if pattern else text


@dataclass(frozen=True)
class Cue:
    """One subtitle line."""

    start: float
    end: float
    text: str


def cues_from_words(
    words: tuple[Word, ...], max_chars: int = 42, max_seconds: float = 4.0, max_gap: float = 0.6
) -> list[Cue]:
    """Group words into readable subtitle lines for the ``.srt`` file.

    A new line starts after a pause, at the end of a sentence, or when the line
    would get too long. (The big word-by-word captions burned into vertical
    videos use their own, shorter blocks: SPEC §10.5.)
    """
    cues: list[Cue] = []
    current: list[Word] = []
    for word in words:
        if current:
            line = " ".join(w.text for w in current)
            too_long = len(line) + 1 + len(word.text) > max_chars
            too_slow = word.end - current[0].start > max_seconds
            paused = word.start - current[-1].end > max_gap
            sentence_end = current[-1].text[-1:] in ".?!"
            if too_long or too_slow or paused or sentence_end:
                cues.append(Cue(current[0].start, current[-1].end, line))
                current = []
        current.append(word)
    if current:
        cues.append(Cue(current[0].start, current[-1].end, " ".join(w.text for w in current)))
    return cues


def to_srt(cues: list[Cue], pattern: re.Pattern[str] | None = None) -> str:
    """Render subtitle lines as SubRip text, masking listed words."""
    blocks = []
    for number, cue in enumerate(cues, start=1):
        end = max(cue.end, cue.start + 0.2)
        blocks.append(
            f"{number}\n{_stamp(cue.start)} --> {_stamp(end)}\n{mask_text(cue.text, pattern)}\n"
        )
    return "\n".join(blocks)


def _stamp(seconds: float) -> str:
    millis = max(int(round(seconds * 1000)), 0)
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"
