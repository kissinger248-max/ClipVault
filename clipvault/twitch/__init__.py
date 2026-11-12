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
]

HTTP_TIMEOUT_S = 20.0


class MissingCredentials(Exception):
    """The Twitch client ID or secret has not been stored yet."""


def build_client(http: httpx.Client | None = None) -> TwitchClient:
    """Build a Helix client from the credentials in the OS keychain.

    The caller should ``close()`` the client when done.
    """
    client_id = cfg.get_secret("twitch_client_id")
    client_secret = cfg.get_secret("twitch_client_secret")
    if not client_id or not client_secret:
        raise MissingCredentials(
            "Twitch keys are not set. Create an application at https://dev.twitch.tv/console "
            "(redirect URL http://localhost:8765/auth/twitch/callback), then run:\n"
            "  python -m clipvault secrets set twitch_client_id\n"
            "  python -m clipvault secrets set twitch_client_secret"
        )
    http = http or httpx.Client(timeout=HTTP_TIMEOUT_S)
    return TwitchClient(client_id, AppTokenProvider(client_id, client_secret, http), http)
