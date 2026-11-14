"""Owner preferences that persist between sessions (default tone / language / how each platform is published).

Stored as one JSON blob in the key/value table so that ``/settings`` changes survive a restart.
"""

from __future__ import annotations

import json
from typing import Callable

from .config import BrandProfile
from .store import Store
from .styles import StyleCatalog

METHODS = ("api", "manual")  # api = the agent publishes via the official API; manual = it sends you a ready-to-post package
PLATFORMS = ("instagram", "tiktok")


class Prefs:
    def __init__(self, store: Store, brand: BrandProfile, catalog: StyleCatalog,
                 has_api_credentials: Callable[[str], bool] = lambda platform: False):
        self.store, self.brand, self.catalog = store, brand, catalog
        self._has_creds = has_api_credentials

    def _load(self) -> dict:
        try:
            data = json.loads(self.store.kv_get("prefs") or "{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _save(self, data: dict) -> None:
        self.store.kv_set("prefs", json.dumps(data))

    # -- style ---------------------------------------------------------------------
    @property
    def tone(self) -> str:
        key = self._load().get("tone") or self.brand.default_tone
        return key if self.catalog.has_tone(key) else self.catalog.tones[0].key

    @property
    def language(self) -> str:
        key = self._load().get("language") or self.brand.default_language
        return key if self.catalog.has_language(key) else self.catalog.languages[0].key

    def set_tone(self, key: str) -> None:
        if not self.catalog.has_tone(key):
            raise ValueError(f"Unknown tone {key!r}")
        self._save({**self._load(), "tone": key})

    def set_language(self, key: str) -> None:
        if not self.catalog.has_language(key):
            raise ValueError(f"Unknown language {key!r}")
        self._save({**self._load(), "language": key})

    # -- publishing method -----------------------------------------------------------
    def method(self, platform: str) -> str:
        """`api` when the platform's API credentials exist (a safe dry-run publisher stands in until DRY_RUN=false),
        otherwise `manual`, so the bot is useful from the very first message."""
        chosen = (self._load().get("methods") or {}).get(platform)
        if chosen in METHODS:
            return chosen
        return "api" if self._has_creds(platform) else "manual"

    def set_method(self, platform: str, method: str) -> None:
        if platform not in PLATFORMS or method not in METHODS:
            raise ValueError(f"Unknown platform/method {platform!r}/{method!r}")
        data = self._load()
        data["methods"] = {**(data.get("methods") or {}), platform: method}
        self._save(data)
