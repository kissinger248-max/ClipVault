"""Control-panel security: password, login cookie and form tokens (SPEC §15).

- The password is chosen on first open and kept only as a salted scrypt hash in
  the OS keychain (``panel_password_hash``).
- Logging in sets an HttpOnly, SameSite=Strict cookie holding an expiry time and
  an HMAC signature made with a random key from the keychain
  (``panel_session_key``). Changing the password changes the key, which logs
  every device out.
- Every form carries a CSRF token derived from the session, checked on POST.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from clipvault import config as cfg

COOKIE = "clipvault_session"
SESSION_DAYS = 30
MIN_PASSWORD = 8
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    expected = hash_password(password, _unb64(salt)).split("$")[2]
    return hmac.compare_digest(expected, digest)


def has_password() -> bool:
    return bool(cfg.get_secret("panel_password_hash"))


def set_password(password: str) -> None:
    """Store a new password and a new session key (logging every device out)."""
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"the password needs at least {MIN_PASSWORD} characters")
    cfg.set_secret("panel_password_hash", hash_password(password))
    cfg.set_secret("panel_session_key", secrets.token_urlsafe(32))


def check_password(password: str) -> bool:
    stored = cfg.get_secret("panel_password_hash")
    return bool(stored) and verify_password(password, stored)


def _key() -> bytes:
    key = cfg.get_secret("panel_session_key")
    if not key:
        key = secrets.token_urlsafe(32)
        cfg.set_secret("panel_session_key", key)
    return key.encode()


def _sign(text: str) -> str:
    return _b64(hmac.new(_key(), text.encode(), hashlib.sha256).digest())


def make_session(now: float | None = None) -> str:
    """A new login cookie value, valid for 30 days."""
    expires = int((now or time.time()) + SESSION_DAYS * 86400)
    nonce = secrets.token_urlsafe(8)
    body = f"{expires}.{nonce}"
    return f"{body}.{_sign(body)}"


def session_valid(value: str | None, now: float | None = None) -> bool:
    if not value or value.count(".") != 2:
        return False
    expires, nonce, signature = value.split(".")
    if not hmac.compare_digest(_sign(f"{expires}.{nonce}"), signature):
        return False
    return expires.isdigit() and int(expires) > (now or time.time())


def csrf_token(session: str | None) -> str:
    """The form token that belongs to a session ("" without one)."""
    return _sign(f"csrf:{session}") if session else ""


def csrf_valid(session: str | None, token: str | None) -> bool:
    return bool(session and token) and hmac.compare_digest(csrf_token(session), token or "")
