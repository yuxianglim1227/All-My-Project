"""Builders shared by pipeline/scheduler/channel tests."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fakes import FakeLLM
from socialagent.brain import Understander, Writer
from socialagent.drafts import DraftService
from socialagent.config import BrandProfile, Settings
from socialagent.hosting import MediaHost
from socialagent.pipeline import ListNotifier, Pipeline
from socialagent.prefs import Prefs
from socialagent.publishers import ManualPublisher, PublishError, PublishRequest, PublishResult
from socialagent.styles import StyleCatalog
from socialagent.scheduler import Scheduler
from socialagent.store import Store
from socialagent.trends import TrendResearcher

ROOT = Path(__file__).parents[1]
T0 = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)   # a Wednesday, 10:00 UTC


class Clock:
    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ZeroRng:
    def randint(self, a, b):
        return 0


class ScriptedPublisher:
    """Publisher whose outcomes are scripted: each entry is a PublishResult or an Exception."""

    def __init__(self, platform="instagram", outcomes: list[Any] | None = None, needs_url: bool = False, delay: float = 0.0):
        self.platform = platform
        self.outcomes = list(outcomes or [])
        self.needs = needs_url
        self.delay = delay
        self.requests: list[PublishRequest] = []

    def needs_urls(self, kind: str) -> bool:
        return self.needs

    def publish(self, req: PublishRequest) -> PublishResult:
        import time
        self.requests.append(req)
        if self.delay:
            time.sleep(self.delay)
        out = self.outcomes.pop(0) if self.outcomes else PublishResult(remote_id=f"remote-{req.post_id}", permalink="https://x/p")
        if isinstance(out, Exception):
            raise out
        return out

    def selfcheck(self) -> list[str]:
        return ["scripted ok"]


def retryable(msg="temporary"):
    return PublishError(msg, retryable=True)


def fatal(msg="nope"):
    return PublishError(msg, retryable=False)


@dataclass
class Rig:
    pipeline: Pipeline
    drafts: DraftService
    prefs: Prefs
    notifier: ListNotifier
    store: Store
    llm: FakeLLM
    clock: Clock
    settings: Settings
    scheduler: Scheduler
    host: MediaHost
    publishers: dict[str, Any] = field(default_factory=dict)

    def submit(self, paths, instruction: str = "", *, approve: bool = True, now: bool = False, confirmed: bool = False):
        """Send media through the draft flow; by default also tap Approve. Returns the draft id."""
        did = self.drafts.create(list(paths), instruction, "test")
        if did and approve:
            self.drafts.approve(did, now=now, confirmed=confirmed)
        return did

    @property
    def sid(self) -> str:
        return self.store.list_drafts(limit=1)[0]["submission_id"]


def make_rig(tmp_path: Path, *, env: dict[str, str] | None = None, publishers: dict[str, Any] | None = None,
             brand: BrandProfile | None = None, draft_maker=None) -> Rig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    brand_file = tmp_path / "brand.yaml"
    shutil.copyfile(ROOT / "config" / "brand.yaml", brand_file)
    settings = Settings.from_env({"DATA_DIR": str(tmp_path / "data"), "BRAND_FILE": str(brand_file), "TIMEZONE": "UTC",
                                  "PUBLIC_BASE_URL": "https://agent.example", **(env or {})})
    settings.ensure_dirs()
    brand = brand or BrandProfile.load(brand_file)
    store, llm, clock = Store(":memory:"), FakeLLM(), Clock()
    trends = TrendResearcher(llm, store, brand)
    host = MediaHost(settings.public_dir, settings.public_base_url)
    notifier = ListNotifier()
    pubs = publishers if publishers is not None else {"instagram": ScriptedPublisher("instagram"), "tiktok": ScriptedPublisher("tiktok")}
    pipeline = Pipeline(settings=settings, brand=brand, store=store, trends=trends, publishers=pubs, host=host, notifier=notifier,
                        manual=ManualPublisher(notifier), clock=clock, rng=ZeroRng(), draft_maker=draft_maker)
    catalog = StyleCatalog.from_brand(brand.custom_tones)
    prefs = Prefs(store, brand, catalog, lambda platform: True)   # tests default to the API method
    drafts = DraftService(settings=settings, store=store, pipeline=pipeline, understander=Understander(llm, brand, settings.data_dir / "work"),
                          writer=Writer(llm, brand), prefs=prefs, catalog=catalog, notifier=notifier)
    scheduler = Scheduler(pipeline=pipeline, trends=trends, store=store, clock=clock)
    return Rig(pipeline, drafts, prefs, notifier, store, llm, clock, settings, scheduler, host, pubs)


def one_post(platform="instagram", kind="photo", **kw) -> dict[str, Any]:
    base = {"platform": platform, "kind": kind, "caption": "Coffee o'clock", "hashtags": ["#coffee"], "on_screen_text": "",
            "when": "next_slot", "highlight": "", "media_indexes": [0], "trim_start": 0, "trim_end": 0, "why": "test"}
    base.update(kw)
    return base


def set_plan(llm: FakeLLM, *posts: dict[str, Any], **kw) -> None:
    llm.plan_response = {"pillar": "Daily life", "summary": "A test post", "edit_brief": "", "trend_used": "",
                         "risk_flags": [], "message_to_owner": "", "posts": list(posts), **kw}


def make_app(tmp_path: Path, *, env: dict[str, str] | None = None, notifier=None):
    """A real App (dry-run publishers, FakeLLM) for channel tests."""
    from socialagent.app import build_app
    tmp_path.mkdir(parents=True, exist_ok=True)
    brand_file = tmp_path / "brand.yaml"
    shutil.copyfile(ROOT / "config" / "brand.yaml", brand_file)
    settings = Settings.from_env({
        "DATA_DIR": str(tmp_path / "data"), "BRAND_FILE": str(brand_file), "TIMEZONE": "UTC", "DRY_RUN": "true",
        "API_TOKEN": "s3cret", "OWNER_TELEGRAM_ID": "42", "TELEGRAM_BOT_TOKEN": "123:abc",
        "PUBLIC_BASE_URL": "https://agent.example", **(env or {})})
    notifier = notifier or ListNotifier()
    llm = FakeLLM()
    return build_app(settings, notifier, llm=llm), notifier, llm
