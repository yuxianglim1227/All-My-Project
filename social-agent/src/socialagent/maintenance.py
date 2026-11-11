"""Keeping credentials alive: Instagram long-lived tokens (~60 days) and TikTok access tokens (~24 h)."""

from __future__ import annotations

import logging
from typing import Callable

import httpx

from .config import Settings
from .publishers import PublishError
from .publishers.instagram import refresh_long_lived_token
from .publishers.tiktok import refresh_access_token
from .publishers.tokens import TokenBook

log = logging.getLogger(__name__)


def make_tiktok_refresher(settings: Settings, tokens: TokenBook, http: httpx.Client) -> Callable[[], str]:
    """Callable used by the TikTok publisher when TikTok says the access token is invalid."""

    def refresh() -> str:
        refresh_token = tokens.tt_refresh()
        if not (refresh_token and settings.tiktok_client_key and settings.tiktok_client_secret):
            raise PublishError("TikTok token expired and no refresh token / client credentials are configured. "
                               "Run `socialagent auth tiktok-url` to re-authorise.", retryable=False)
        data = refresh_access_token(http, settings.tiktok_client_key, settings.tiktok_client_secret, refresh_token)
        tokens.set_tt_tokens(data["access_token"], data.get("refresh_token"))  # TikTok may rotate the refresh token
        return data["access_token"]

    return refresh


def refresh_instagram(settings: Settings, tokens: TokenBook, http: httpx.Client) -> str:
    current = tokens.ig_token()
    if not current:
        return "instagram: no token configured"
    data = refresh_long_lived_token(http, current, settings.ig_graph_base)
    tokens.set_ig_token(data["access_token"])
    days = int(data.get("expires_in", 0)) // 86400
    return f"instagram: token refreshed (valid ~{days} more days)"


def refresh_tiktok(settings: Settings, tokens: TokenBook, http: httpx.Client) -> str:
    make_tiktok_refresher(settings, tokens, http)()
    return "tiktok: access token refreshed"
