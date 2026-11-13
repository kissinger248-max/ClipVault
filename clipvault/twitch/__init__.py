"""Twitch Helix API client, auth and chat replay."""

from __future__ import annotations

import httpx

from clipvault import config as cfg
from clipvault.twitch.api import TwitchAPIError, TwitchClient
from clipvault.twitch.auth import AppTokenProvider, TwitchAuthError

__all__ = [
    "MissingCredentials",
    "TwitchAPIError",
    "TwitchAuthError",
    "TwitchClient",
    "build_client",
    "connected",
]

HTTP_TIMEOUT_S = 20.0

SIGN_IN_HINT = (
    "Sign in with Twitch in the control panel (Settings, Twitch), or run "
    "'python -m clipvault twitch login'."
)
OWN_KEYS_HINT = (
    "Twitch keys are not set. Create an application at https://dev.twitch.tv/console "
    "(redirect URL http://localhost:8765/auth/twitch/callback), then run:\n"
    "  python -m clipvault secrets set twitch_client_id\n"
    "  python -m clipvault secrets set twitch_client_secret"
)


class MissingCredentials(Exception):
    """Neither developer keys nor a Twitch sign-in are available yet."""


class _SignedInTokens:
    """Adapts the user's Twitch sign-in to the token interface ``TwitchClient`` uses."""

    def __init__(self, provider) -> None:
        self._provider = provider

    def get(self) -> str:
        from clipvault.twitch import user_auth

        try:
            return self._provider.get()
        except user_auth.LoginNeeded as exc:
            raise TwitchAuthError(f"{exc}. {SIGN_IN_HINT}") from exc

    def invalidate(self) -> None:
        self._provider.invalidate()


def connected() -> bool:
    """Whether ClipVault can talk to Twitch: own keys stored, or signed in."""
    from clipvault.twitch import user_auth

    return bool(cfg.own_twitch_keys()) or (user_auth.available() and user_auth.stored() is not None)


def build_client(http: httpx.Client | None = None) -> TwitchClient:
    """Build a Helix client.

    With the user's own developer keys (client ID and secret in the keychain) an app
    token is used, as before. Otherwise the user's Twitch sign-in is used, with
    ClipVault's built-in public app ID. The caller should ``close()`` the client.
    """
    from clipvault.twitch import user_auth

    own = cfg.own_twitch_keys()
    if own:
        client_id, client_secret = own
        http = http or httpx.Client(timeout=HTTP_TIMEOUT_S)
        return TwitchClient(client_id, AppTokenProvider(client_id, client_secret, http), http)
    if not user_auth.available():
        raise MissingCredentials(OWN_KEYS_HINT)
    if user_auth.stored() is None:
        raise MissingCredentials(f"Not signed in to Twitch. {SIGN_IN_HINT}")
    client_id = user_auth.client_id()
    http = http or httpx.Client(timeout=HTTP_TIMEOUT_S)
    tokens = _SignedInTokens(user_auth.UserTokenProvider(http))
    return TwitchClient(client_id, tokens, http)
