"""What ClipVault is doing right now, for the panel's live view.

Kept in memory in the running app (the panel and the scheduler share one process), so
the dashboard can show work as it happens instead of only the finished clips:

- **work:** the clip being downloaded, cut, subtitled or edited right now, with when it
  started and how long that usually takes (learned from the last few);
- **steps:** the scheduled steps running right now ("Looking for new clips");
- **live:** each live channel whose chat is being read: the last few minutes of chat
  activity in 10-second windows, the usual level, spikes and moments so far;
- **events:** a short feed of what just happened ("moment caught", "chat spiked").

Nothing here is saved; after a restart it starts empty. Everything is thread-safe.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

# Seconds each kind of work usually takes before any has been measured.
TYPICAL_S = {"downloading": 6.0, "cutting": 12.0, "subtitles": 20.0, "editing": 30.0}
STEP_LABELS = {
    "discover": "Looking for new clips",
    "worker": "Working through new clips",
    "vods": "Searching finished streams' chat",
    "cleanup": "Tidying the library",
}
EVENTS_KEPT = 40
METER_WINDOWS = 30  # 5 minutes of 10-second chat windows

_lock = threading.Lock()


@dataclass
class Work:
    clip_id: str
    stage: str  # downloading | cutting | subtitles | editing
    started: float
    expected_s: float
    login: str = ""


@dataclass
class LiveChannel:
    user_id: str
    login: str
    stream_started: float | None = None
    recording: bool = False
    windows: list[float] = field(default_factory=list)  # closed 10-second windows, oldest first
    current: float = 0.0  # the window still filling
    baseline: float = 0.0  # usual level: mean of the last 10 minutes
    spikes: int = 0
    moments: int = 0
    updated: float = 0.0


@dataclass
class _State:
    work: dict[str, Work] = field(default_factory=dict)
    steps: dict[str, float] = field(default_factory=dict)
    live: dict[str, LiveChannel] = field(default_factory=dict)
    live_status: str = "starting"  # starting | watching | idle | no_login | paused | off
    live_checked: float = 0.0
    events: deque = field(default_factory=lambda: deque(maxlen=EVENTS_KEPT))
    seq: int = 0
    next_runs: dict[str, float] = field(default_factory=dict)
    typical: dict[str, float] = field(default_factory=lambda: dict(TYPICAL_S))


_state = _State()


def reset() -> None:
    """Forget everything (tests, and a fresh start)."""
    global _state
    with _lock:
        _state = _State()


# --- work on one clip --------------------------------------------------------------------


@contextmanager
def working(clip_id: str, stage: str, login: str = "") -> Iterator[None]:
    """Mark a clip as being worked on for the duration of the block."""
    started = time.time()
    with _lock:
        expected = _state.typical.get(stage, 15.0)
        _state.work[clip_id] = Work(clip_id, stage, started, expected, login)
    ok = False
    try:
        yield
        ok = True
    finally:
        took = time.time() - started
        with _lock:
            _state.work.pop(clip_id, None)
            if ok:  # learn how long this usually takes (smoothed)
                old = _state.typical.get(stage, took)
                _state.typical[stage] = round(0.7 * old + 0.3 * took, 1)


def step_started(name: str) -> None:
    if name in STEP_LABELS:
        with _lock:
            _state.steps[name] = time.time()


def step_finished(name: str) -> None:
    with _lock:
        _state.steps.pop(name, None)


def next_run(name: str, when: float | None) -> None:
    """When a scheduled step runs next (UTC seconds), for "next look in 12 min"."""
    with _lock:
        if when is None:
            _state.next_runs.pop(name, None)
        else:
            _state.next_runs[name] = when


# --- live chat ---------------------------------------------------------------------------


def live_status(status: str) -> None:
    """What the live watcher is doing overall (see ``_State.live_status``)."""
    with _lock:
        _state.live_status = status
        _state.live_checked = time.time()
        if status != "watching":
            _state.live.clear()


def live_channel(channel: LiveChannel) -> None:
    """Publish one live channel's chat activity (the watcher calls this every second)."""
    channel.windows = channel.windows[-METER_WINDOWS:]
    channel.updated = time.time()
    with _lock:
        _state.live[channel.user_id] = channel
        _state.live_status = "watching"


def live_ended(user_id: str) -> None:
    with _lock:
        _state.live.pop(user_id, None)


# --- events --------------------------------------------------------------------------------


def event(kind: str, text: str, **details: Any) -> None:
    """Something worth showing happened (``kind``: moment, spike, saved, edited, ...)."""
    with _lock:
        _state.seq += 1
        _state.events.append({"seq": _state.seq, "at": time.time(), "kind": kind,
                              "text": text, **details})  # fmt: skip


# --- reading ---------------------------------------------------------------------------------


def snapshot(since: int = 0) -> dict[str, Any]:
    """Everything above as plain data; ``events`` only those newer than ``since``."""
    with _lock:
        return {
            "now": time.time(),
            "work": [asdict(w) for w in _state.work.values()],
            "steps": [
                {"name": n, "label": STEP_LABELS[n], "since": t} for n, t in _state.steps.items()
            ],  # fmt: skip
            "live": {
                "status": _state.live_status,
                "checked": _state.live_checked,
                "channels": [asdict(c) for c in _state.live.values()],
            },
            "next": dict(_state.next_runs),
            "events": [e for e in _state.events if e["seq"] > since],
            "seq": _state.seq,
        }


def working_on() -> dict[str, str]:
    """Clip id -> stage, for everything being worked on right now."""
    with _lock:
        return {w.clip_id: w.stage for w in _state.work.values()}
