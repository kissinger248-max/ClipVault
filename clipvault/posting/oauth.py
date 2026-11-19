"""OAuth for TikTok, YouTube and Instagram. The app only ever sees the authorization code, never a password.
Tokens go in the secret store (key token:<account id>) and are refreshed before every upload."""

from __future__ import annotations

import hashlib
import secrets as _rand
import sqlite3
import time
from urllib.parse import urlencode

from clipvault.posting import http as requests

from clipvault.posting import store
from clipvault.posting.platforms import PLATFORMS, redirect_uri
from clipvault.posting.secrets import Secrets

STATE_TTL = 600
REFRESH_MARGIN = 600


class OAuthError(RuntimeError):
    pass


def credentials(secrets: Secrets, platform: str) -> dict[str, str]:
    out = {}
    for key, _label in PLATFORMS[platform]["fields"]:
        v = secrets.get(f"cred:{platform}:{key}")
        if not v:
            raise OAuthError(f"{PLATFORMS[platform]['label']} credentials aren't saved yet (Platform Setup).")
        out[key] = v
    return out


def new_code_verifier() -> str:
    """A fresh PKCE code verifier: 86 random characters from TikTok's allowed set (A-Z a-z 0-9 - _), 43-128 long."""
    return _rand.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    """TikTok's challenge is the SHA-256 of the verifier written in HEX (not base64url as in RFC 7636);
    see https://developers.tiktok.com/doc/login-kit-desktop."""
    return hashlib.sha256(verifier.encode("ascii")).hexdigest()


def begin(c: sqlite3.Connection, secrets: Secrets, platform: str, label: str = "", port: int | None = None) -> str:
    spec = PLATFORMS.get(platform)
    if not spec or not spec["oauth"]:
        raise OAuthError("That platform has nothing to connect.")
    cred = credentials(secrets, platform)
    state = _rand.token_urlsafe(24)
    c.execute("DELETE FROM oauth_states WHERE created_at < ?", (time.time() - STATE_TTL,))
    verifier = new_code_verifier() if platform == "tiktok" else None      # a new one for every attempt
    c.execute("INSERT INTO oauth_states (state, platform, label, created_at, code_verifier) VALUES (?,?,?,?,?)",
              (state, platform, label, time.time(), verifier))
    c.commit()
    ruri = redirect_uri(platform, port)
    if platform == "tiktok":
        q = {"client_key": cred["client_key"], "response_type": "code", "scope": ",".join(spec["scopes"]),
             "redirect_uri": ruri, "state": state, "code_challenge": code_challenge(verifier),
             "code_challenge_method": "S256"}
    elif platform == "youtube":
        q = {"client_id": cred["client_id"], "response_type": "code", "scope": " ".join(spec["scopes"]),
             "redirect_uri": ruri, "state": state, "access_type": "offline", "prompt": "consent select_account"}
    else:
        q = {"client_id": cred["app_id"], "response_type": "code", "scope": ",".join(spec["scopes"]),
             "redirect_uri": ruri, "state": state, "force_reauth": "true"}
    return spec["auth_url"] + "?" + urlencode(q)


def _json(r: requests.Response, what: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        raise OAuthError(f"{what}: unreadable answer ({r.status_code})")
    err = data.get("error")
    if isinstance(err, dict):                       # TikTok: success carries {"error": {"code": "ok"}}
        failed = err.get("code") not in (None, "", "ok")
        detail = f"{err.get('code')}: {err.get('message') or ''}".strip()
    else:
        failed, detail = bool(err), str(data.get("error_description") or err)
    if r.status_code >= 400 or failed:
        raise OAuthError(f"{what}: {detail or str(data)}"[:240])
    return data


def finish(c: sqlite3.Connection, secrets: Secrets, platform: str, code: str, state: str,
           port: int | None = None, http=requests) -> int:
    row = c.execute("SELECT * FROM oauth_states WHERE state=? AND platform=?", (state, platform)).fetchone()
    if not row or time.time() - row["created_at"] > STATE_TTL:
        raise OAuthError("This sign-in link expired or wasn't started here. Press Connect again.")
    c.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    c.commit()
    cred = credentials(secrets, platform)
    ruri = redirect_uri(platform, port)
    spec = PLATFORMS[platform]
    if platform == "tiktok":
        tok = _json(http.post(spec["token_url"], data={"client_key": cred["client_key"], "client_secret": cred["client_secret"],
                    "code": code, "grant_type": "authorization_code", "redirect_uri": ruri,
                    "code_verifier": row["code_verifier"] or ""}, timeout=30), "TikTok sign-in")
        info = _json(http.get("https://open.tiktokapis.com/v2/user/info/", params={"fields": "open_id,display_name"},
                     headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30), "TikTok profile")
        u = (info.get("data") or {}).get("user") or {}
        ext, handle, atype = tok.get("open_id") or u.get("open_id", ""), u.get("display_name") or "tiktok", None
    elif platform == "youtube":
        tok = _json(http.post(spec["token_url"], data={"client_id": cred["client_id"], "client_secret": cred["client_secret"],
                    "code": code, "grant_type": "authorization_code", "redirect_uri": ruri}, timeout=30), "Google sign-in")
        ch = _json(http.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                   headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30), "YouTube channel")
        items = ch.get("items") or []
        if not items:
            raise OAuthError("That Google account has no YouTube channel.")
        ext, handle, atype = items[0]["id"], items[0]["snippet"].get("title", "channel"), None
    else:
        short = _json(http.post(spec["token_url"], data={"client_id": cred["app_id"], "client_secret": cred["app_secret"],
                      "grant_type": "authorization_code", "redirect_uri": ruri, "code": code.split("#")[0]}, timeout=30),
                      "Instagram sign-in")
        lt = _json(http.get("https://graph.instagram.com/access_token", params={"grant_type": "ig_exchange_token",
                   "client_secret": cred["app_secret"], "access_token": short["access_token"]}, timeout=30), "Instagram token")
        tok = {"access_token": lt["access_token"], "expires_in": lt.get("expires_in", 5184000)}
        me = _json(http.get("https://graph.instagram.com/v21.0/me", params={"fields": "user_id,username,account_type",
                   "access_token": tok["access_token"]}, timeout=30), "Instagram profile")
        ext, handle, atype = str(me.get("user_id") or me.get("id")), me.get("username", "instagram"), me.get("account_type")
    expires = time.time() + float(tok.get("expires_in") or 3600)
    aid = store.add_account(c, platform, row["label"] or handle, handle, ext, atype, spec["scopes"], expires)
    secrets.set_json(f"token:{aid}", {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                                       "expires_at": expires, "created_at": time.time()})
    return aid


def ensure_fresh(c: sqlite3.Connection, secrets: Secrets, account_id: int, http=requests) -> str:
    """A valid access token for the account, refreshing it first when it is about to expire."""
    acc = c.execute("SELECT * FROM social_accounts WHERE id=?", (account_id,)).fetchone()
    tok = secrets.get_json(f"token:{account_id}")
    if not acc or not tok:
        raise OAuthError("This account isn't connected. Reconnect it.")
    if tok["expires_at"] - time.time() > REFRESH_MARGIN:
        return tok["access_token"]
    platform, cred = acc["platform"], None
    try:
        cred = credentials(secrets, platform)
        if platform == "tiktok":
            new = _json(http.post(PLATFORMS[platform]["token_url"], data={"client_key": cred["client_key"],
                        "client_secret": cred["client_secret"], "grant_type": "refresh_token",
                        "refresh_token": tok.get("refresh_token")}, timeout=30), "TikTok refresh")
        elif platform == "youtube":
            new = _json(http.post(PLATFORMS[platform]["token_url"], data={"client_id": cred["client_id"],
                        "client_secret": cred["client_secret"], "grant_type": "refresh_token",
                        "refresh_token": tok.get("refresh_token")}, timeout=30), "Google refresh")
            new.setdefault("refresh_token", tok.get("refresh_token"))
        else:
            new = _json(http.get("https://graph.instagram.com/refresh_access_token", params={"grant_type": "ig_refresh_token",
                        "access_token": tok["access_token"]}, timeout=30), "Instagram refresh")
    except (OAuthError, requests.RequestException) as e:
        c.execute("UPDATE social_accounts SET status='expired', error=? WHERE id=?", (str(e)[:200], account_id))
        c.commit()
        raise OAuthError(f"Token refresh failed: {e}") from e
    expires = time.time() + float(new.get("expires_in") or 3600)
    secrets.set_json(f"token:{account_id}", {"access_token": new["access_token"],
                     "refresh_token": new.get("refresh_token") or tok.get("refresh_token"), "expires_at": expires,
                     "created_at": time.time()})
    c.execute("UPDATE social_accounts SET status='connected', error=NULL, token_expires_at=? WHERE id=?", (expires, account_id))
    c.commit()
    return new["access_token"]


def disconnect(c: sqlite3.Connection, secrets: Secrets, account_id: int) -> None:
    secrets.delete(f"token:{account_id}")
    c.execute("DELETE FROM social_accounts WHERE id=?", (account_id,))
    c.execute("DELETE FROM routing_rules WHERE account_id=?", (account_id,))
    c.commit()
