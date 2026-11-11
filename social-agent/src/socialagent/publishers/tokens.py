"""Access tokens expire (Instagram ~60 days, TikTok ~24 h). Refreshed values live in the DB and win over .env."""

from __future__ import annotations

from ..config import Settings
from ..store import Store


class TokenBook:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings

    # Instagram -------------------------------------------------------------------
    def ig_token(self) -> str:
        return self.store.kv_get("ig_access_token") or self.settings.ig_access_token

    def set_ig_token(self, token: str) -> None:
        self.store.kv_set("ig_access_token", token)

    # TikTok ----------------------------------------------------------------------
    def tt_access(self) -> str:
        return self.store.kv_get("tt_access_token") or self.settings.tiktok_access_token

    def tt_refresh(self) -> str:
        return self.store.kv_get("tt_refresh_token") or self.settings.tiktok_refresh_token

    def set_tt_tokens(self, access: str, refresh: str | None = None) -> None:
        self.store.kv_set("tt_access_token", access)
        if refresh:
            self.store.kv_set("tt_refresh_token", refresh)
