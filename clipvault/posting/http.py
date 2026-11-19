"""A tiny ``requests``-style layer over httpx, so the uploaders stay simple and testable.

Uploaders take an ``http`` object with ``get``/``post``/``put``/``request``; tests pass a fake.
"""

from __future__ import annotations

import httpx

RequestException = httpx.HTTPError
Response = httpx.Response


def _fix(kw: dict) -> dict:
    kw = dict(kw)
    if isinstance(kw.get("data"), (bytes, bytearray)):
        kw["content"] = bytes(kw.pop("data"))
    return kw


def request(method: str, url: str, **kw) -> httpx.Response:
    kw.setdefault("timeout", 60)
    return httpx.request(method, url, follow_redirects=True, **_fix(kw))


def get(url: str, **kw) -> httpx.Response:
    return request("GET", url, **kw)


def post(url: str, **kw) -> httpx.Response:
    return request("POST", url, **kw)


def put(url: str, **kw) -> httpx.Response:
    return request("PUT", url, **kw)
