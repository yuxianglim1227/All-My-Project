"""Background jobs. ``tick`` is a pure function of (state, now) so it is easy to test without sleeping."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable

from .llm import LLMError
from .pipeline import Pipeline
from .publishers import PublishError
from .store import Store
from .timeutil import iso, parse_iso, utcnow
from .trends import TrendResearcher

log = logging.getLogger(__name__)

TRENDS_RETRY_HOURS = 3
IG_TOKEN_DAYS = 7
TT_TOKEN_HOURS = 20


class Scheduler:
    def __init__(self, *, pipeline: Pipeline, trends: TrendResearcher, store: Store,
                 refresh_instagram: Callable[[], str] | None = None, refresh_tiktok: Callable[[], str] | None = None,
                 clock: Callable[[], datetime] = utcnow):
        self.pipeline, self.trends, self.store = pipeline, trends, store
        self.refresh_instagram, self.refresh_tiktok = refresh_instagram, refresh_tiktok
        self.clock = clock

    def _due(self, key: str, now: datetime, every: timedelta) -> bool:
        last = self.store.kv_get(key)
        return last is None or now - parse_iso(last) >= every

    def _mark(self, key: str, now: datetime) -> None:
        self.store.kv_set(key, iso(now))

    def tick(self, now: datetime | None = None) -> dict[str, object]:
        now = now or self.clock()
        local = now.astimezone(self.pipeline.zone)
        out: dict[str, object] = {"published": self.pipeline.tick(now)}

        # Trend research: daily, after 06:00 local; a failed attempt waits a few hours (don't hammer the API).
        if local.hour >= 6 and self.trends.is_stale(now) and self._due("trends_last_attempt", now, timedelta(hours=TRENDS_RETRY_HOURS)):
            self._mark("trends_last_attempt", now)
            try:
                out["trends"] = len(self.trends.refresh(now))
                if self.store.kv_get("trends_live") == "0":
                    self.pipeline.notifier.notify("ℹ️ Live web search wasn't available, so I used evergreen formats "
                                                  "instead of today's trends.")
            except LLMError as exc:
                log.warning("Trend refresh failed: %s", exc)
                out["trends_error"] = str(exc)

        # Weekly review: Sunday evening local time.
        if local.weekday() == 6 and local.hour >= 18 and self._due("last_review_at", now, timedelta(days=6)):
            self._mark("last_review_at", now)
            self.pipeline.notifier.notify(self.pipeline.weekly_review())
            out["review"] = True

        # Token upkeep.
        if self.refresh_instagram and self._due("tokens_ig_at", now, timedelta(days=IG_TOKEN_DAYS)):
            self._mark("tokens_ig_at", now)
            self._guarded(self.refresh_instagram, "Instagram", out)
        if self.refresh_tiktok and self._due("tokens_tt_at", now, timedelta(hours=TT_TOKEN_HOURS)):
            self._mark("tokens_tt_at", now)
            self._guarded(self.refresh_tiktok, "TikTok", out)

        # Housekeeping once a day.
        if self._due("cleanup_at", now, timedelta(days=1)):
            self._mark("cleanup_at", now)
            self.pipeline.cleanup()
        return out

    def _guarded(self, fn: Callable[[], str], name: str, out: dict[str, object]) -> None:
        try:
            log.info(fn())
        except PublishError as exc:
            out[f"{name.lower()}_token_error"] = str(exc)
            self.pipeline.notifier.notify(f"⚠️ Couldn't refresh the {name} token: {exc}")
        except Exception as exc:  # network etc.: try again next time round
            log.warning("%s token refresh failed: %s", name, exc)

    async def run(self, interval: float = 30.0) -> None:
        self.pipeline.recover()
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:  # the loop must never die
                log.exception("Scheduler tick failed")
            await asyncio.sleep(interval)
