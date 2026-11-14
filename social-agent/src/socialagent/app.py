"""Composition root: builds every component from Settings. Used by the CLI and the channel adapters."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial

import httpx

from . import capcut
from . import media as media_mod
from .brain import Understander, Writer
from .config import BrandProfile, Settings
from .drafts import DraftService
from .hosting import MediaHost
from .llm import LLM, ClaudeLLM
from .maintenance import make_tiktok_refresher, refresh_instagram, refresh_tiktok
from .notify import Notifier
from .pipeline import Pipeline
from .prefs import Prefs
from .publishers import DryRunPublisher, ManualPublisher, Publisher
from .publishers.instagram import InstagramPublisher
from .publishers.tiktok import TikTokPublisher
from .publishers.tokens import TokenBook
from .scheduler import Scheduler
from .store import Store
from .styles import StyleCatalog
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
    drafts: DraftService
    prefs: Prefs
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
    # Looking at media needs the strongest vision model; writing captions is text-only and can use a cheaper one.
    writer_llm = llm if not settings.writer_model or settings.writer_model == settings.claude_model else \
        ClaudeLLM(settings.writer_model, settings.claude_effort, settings.claude_fallbacks)
    tokens = TokenBook(store, settings)
    host = MediaHost(settings.public_dir, settings.public_base_url)
    trends = TrendResearcher(llm, store, brand)
    pubs = publishers if publishers is not None else make_publishers(settings, tokens, http)
    draft_maker = capcut.draft_maker_for(settings.jianying_drafts_dir) if settings.jianying_drafts_dir else None
    pipeline = Pipeline(settings=settings, brand=brand, store=store, trends=trends, publishers=pubs, host=host,
                        notifier=notifier, manual=ManualPublisher(notifier, host), draft_maker=draft_maker)
    catalog = StyleCatalog.from_brand(brand.custom_tones)

    def has_api_credentials(platform: str) -> bool:
        if platform == "instagram":
            return bool(settings.ig_user_id and tokens.ig_token())
        return bool(tokens.tt_access() or (tokens.tt_refresh() and settings.tiktok_client_key))

    prefs = Prefs(store, brand, catalog, has_api_credentials)
    drafts = DraftService(settings=settings, store=store, pipeline=pipeline,
                          understander=Understander(llm, brand, settings.data_dir / "work"),
                          writer=Writer(writer_llm, brand), prefs=prefs, catalog=catalog, notifier=notifier)
    live = not settings.dry_run
    scheduler = Scheduler(
        pipeline=pipeline, trends=trends, store=store,
        refresh_instagram=partial(refresh_instagram, settings, tokens, http) if live and settings.ig_user_id else None,
        refresh_tiktok=partial(refresh_tiktok, settings, tokens, http) if live and settings.tiktok_client_key else None)
    return App(settings, brand, store, llm, host, tokens, trends, pipeline, drafts, prefs, scheduler, pubs)
