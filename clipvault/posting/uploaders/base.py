"""Shared uploader interface: upload(clip, caption, account, token, settings, dry_run) -> Result."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from clipvault.posting import http as requests


@dataclass
class ClipFile:
    clip_id: str
    path: Path
    duration: float = 0.0
    width: int = 1080
    height: int = 1920

    @property
    def size(self) -> int:
        return self.path.stat().st_size


@dataclass
class Result:
    ok: bool
    url: str = ""
    detail: str = ""
    dry_run: bool = False
    extra: dict = field(default_factory=dict)


class UploadError(RuntimeError):
    """code: 'temporary' (retry later), 'quota' (retry tomorrow), 'permanent' (needs a person)."""

    def __init__(self, message: str, code: str = "permanent"):
        super().__init__(message)
        self.code = code


def request(http, method: str, url: str, *, tries: int = 4, sleep=time.sleep, timeout: int = 120, **kw) -> requests.Response:
    """HTTP with exponential backoff on network errors, 429 and 5xx. Other errors are returned for the caller to read."""
    last: Exception | None = None
    for i in range(tries):
        try:
            r = http.request(method, url, timeout=timeout, **kw)
        except requests.RequestException as e:
            last = e
            sleep(min(30, 2 ** i))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = UploadError(f"{url.split('?')[0]} answered {r.status_code}", "temporary")
            sleep(min(60, 2 ** i * (3 if r.status_code == 429 else 1)))
            continue
        return r
    raise UploadError(f"Network trouble: {last}", "temporary")


class Uploader:
    platform = ""

    def upload(self, clip: ClipFile, caption: dict, account: dict, token: str, settings: dict, *,
               dry_run: bool = False, http=requests, sleep=time.sleep) -> Result:  # pragma: no cover - interface
        raise NotImplementedError
