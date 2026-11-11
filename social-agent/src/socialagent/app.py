"""Composition root: builds every component from Settings. Used by the CLI and the channel adapters."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial

import httpx

from . import capcut
from . import media as media_mod
from .brain import Planner
from .config import BrandProfile, Settings
from .hosting import MediaHost
from .llm import LLM, ClaudeLLM
from .maintenance import make_tiktok_refresher, refresh_instagram, refresh_tiktok
from .pipeline import Notifier, Pipeline
from .publishers import DryRunPublisher, Publisher
from .publishers.instagram import InstagramPublisher
from .publishers.tiktok import TikTokPublisher
from .publishers.tokens import TokenBook
from .scheduler import Scheduler
from .store import Store
from .trends import TrendResearcher

log = logging.getLogger(__name__)


@dataclass
class App:
    settings: Settings
    brand: BrandProfile
    store: Store
    llm: LLM
    host: MediaHost
    tokens: TokenBook
    trends: TrendResearcher
    pipeline: Pipeline
    scheduler: Scheduler
    publishers: dict[str, Publisher]


def make_publishers(settings: Settings, tokens: TokenBook, http: httpx.Client) -> dict[str, Publisher]:
    pubs: dict[str, Publisher] = {}
    ig_ok = bool(settings.ig_user_id and tokens.ig_token())
    tt_ok = bool(tokens.tt_access() or (tokens.tt_refresh() and settings.tiktok_client_key))
    if settings.dry_run:
        pubs["instagram"] = DryRunPublisher("instagram", "DRY_RUN is on")
        pubs["tiktok"] = DryRunPublisher("tiktok", "DRY_RUN is on")
        return pubs
    pubs["instagram"] = (InstagramPublisher(user_id=settings.ig_user_id, token=tokens.ig_token,
                                            graph_base=settings.ig_graph_base, api_version=settings.ig_api_version, http=http)
                         if ig_ok else DryRunPublisher("instagram", "Instagram credentials missing"))
    pubs["tiktok"] = (TikTokPublisher(access_token=tokens.tt_access, refresh=make_tiktok_refresher(settings, tokens, http),
                                      mode=settings.tiktok_mode, privacy=settings.tiktok_privacy, http=http)
                      if tt_ok else DryRunPublisher("tiktok", "TikTok credentials missing"))
    return pubs


def build_app(settings: Settings, notifier: Notifier, *, llm: LLM | None = None, http: httpx.Client | None = None,
              publishers: dict[str, Publisher] | None = None) -> App:
    settings.ensure_dirs()
    media_mod.configure(settings.ffmpeg_bin, settings.ffprobe_bin)
    http = http or httpx.Client(timeout=60)
    store = Store(settings.db_path)
    brand = BrandProfile.load(settings.brand_file)
    llm = llm or ClaudeLLM(settings.claude_model, settings.claude_effort, settings.claude_fallbacks)
    tokens = TokenBook(store, settings)
    host = MediaHost(settings.public_dir, settings.public_base_url)
    trends = TrendResearcher(llm, store, brand)
    planner = Planner(llm, brand, settings.data_dir / "work")
    pubs = publishers if publishers is not None else make_publishers(settings, tokens, http)
    draft_maker = capcut.draft_maker_for(settings.jianying_drafts_dir) if settings.jianying_drafts_dir else None
    pipeline = Pipeline(settings=settings, brand=brand, store=store, planner=planner, trends=trends, publishers=pubs,
                        host=host, notifier=notifier, draft_maker=draft_maker)
    live = not settings.dry_run
    scheduler = Scheduler(
        pipeline=pipeline, trends=trends, store=store,
        refresh_instagram=partial(refresh_instagram, settings, tokens, http) if live and settings.ig_user_id else None,
        refresh_tiktok=partial(refresh_tiktok, settings, tokens, http) if live and settings.tiktok_client_key else None)
    return App(settings, brand, store, llm, host, tokens, trends, pipeline, scheduler, pubs)
