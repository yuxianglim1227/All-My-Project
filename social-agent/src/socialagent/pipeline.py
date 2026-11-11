"""Orchestration: media in -> plan -> edit -> schedule -> (undo window / approval) -> publish -> report.

Safety properties enforced here (and covered by tests):
* nothing is published before its scheduled time, and "post now" still waits the undo window;
* a post is claimed atomically, so it can never be published twice by concurrent ticks;
* an ambiguous failure is never retried automatically (could create a duplicate post);
* posts left "publishing" by a crash are marked failed for review, never silently re-sent;
* any risk flag from the planner, or REQUIRE_APPROVAL, parks the post until the owner approves.
"""

from __future__ import annotations

import logging
import os
import random
import shutil
import threading
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol

from . import feed
from . import media as media_mod
from .brain import PlanContext, PlanError, Planner
from .config import BrandProfile, Settings
from .hosting import MediaHost
from .llm import LLMError
from .media import MediaError
from .models import FAILED, MediaItem, PENDING_APPROVAL, PUBLISHED, PUBLISHING, SCHEDULED, PlannedPost
from .publishers import DryRunPublisher, PreparedMedia, PublishError, PublishRequest, Publisher
from .store import Store
from .textutil import IG_CAPTION_MAX, TIKTOK_TITLE_MAX, compose_caption
from .timeutil import parse_iso, parse_when, tz, utcnow
from .trends import TrendResearcher

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_MINUTES = (2, 5, 15)
STORY_MAX_SECONDS = 60
TIKTOK_MAX_SECONDS = 180
FEED_KINDS = {"instagram": ("photo", "carousel", "reel"), "tiktok": ("video", "photo")}
RETENTION_DAYS = 14


class Notifier(Protocol):
    def notify(self, text: str, *, actions: list[tuple[str, str]] | None = None) -> None: ...


class ListNotifier:
    """Collects messages (tests, CLI)."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, list[tuple[str, str]]]] = []

    def notify(self, text: str, *, actions: list[tuple[str, str]] | None = None) -> None:
        self.messages.append((text, actions or []))
        log.info("NOTIFY: %s", text.replace("\n", " | ")[:200])

    @property
    def texts(self) -> list[str]:
        return [t for t, _ in self.messages]


class Pipeline:
    def __init__(self, *, settings: Settings, brand: BrandProfile, store: Store, planner: Planner,
                 trends: TrendResearcher, publishers: dict[str, Publisher], host: MediaHost, notifier: Notifier,
                 clock: Callable[[], datetime] = utcnow, rng: random.Random | None = None,
                 draft_maker: Callable[[str, int, PlannedPost, list[MediaItem]], str | None] | None = None):
        self.settings, self.brand, self.store = settings, brand, store
        self.planner, self.trends = planner, trends
        self.publishers, self.host, self.notifier = publishers, host, notifier
        self.clock = clock
        self.rng = rng or random.Random()
        self.draft_maker = draft_maker
        self.zone = tz(settings.timezone)
        self._lock = threading.Lock()
        self._brand_mtime = self._mtime()

    # ------------------------------------------------------------------ brand hot-reload
    def _mtime(self) -> float:
        try:
            return os.path.getmtime(self.settings.brand_file)
        except OSError:
            return 0.0

    def _reload_brand(self) -> None:
        m = self._mtime()
        if m and m != self._brand_mtime:
            self.brand = self.planner.brand = self.trends.brand = BrandProfile.load(self.settings.brand_file)
            self._brand_mtime = m
            log.info("Brand profile reloaded")

    # ------------------------------------------------------------------ intake
    def submit(self, media_paths: list[Path | str], instruction: str = "", source: str = "cli") -> str | None:
        """Plan, prepare and schedule everything for one batch of media. Returns the submission id."""
        self._reload_brand()
        try:
            items = [media_mod.probe(p) for p in media_paths]
        except MediaError as exc:
            self.notifier.notify(f"❌ I couldn't read that file: {exc}")
            return None
        if not items:
            self.notifier.notify("Send me a photo or video (with an instruction if you like) and I'll take it from there.")
            return None
        sid = self.store.add_submission(source=source, instruction=instruction, media=[i.to_dict() for i in items])
        kinds = [i.kind for i in items]
        self.notifier.notify(f"📥 Got {kinds.count('image')} photo(s) and {kinds.count('video')} video(s). Planning…")
        try:
            self._plan_and_schedule(sid, items, instruction)
        except (LLMError, PlanError) as exc:
            self.store.update_submission(sid, status="failed", error=str(exc))
            self.notifier.notify(f"❌ I couldn't plan this one: {exc}\nYour files are saved - send them again or give me a hint.")
        except Exception as exc:  # never lose the owner's media silently
            log.exception("Unexpected error while handling submission %s", sid)
            self.store.update_submission(sid, status="failed", error=repr(exc))
            self.notifier.notify(f"❌ Something went wrong while handling that ({type(exc).__name__}: {exc}).")
        return sid

    def _plan_and_schedule(self, sid: str, items: list[MediaItem], instruction: str) -> None:
        now = self.clock()
        recent = self.store.list_posts(feed.LIVE_STATUSES, limit=30, newest_first=True)
        ctx = PlanContext(
            now_local=f"{now.astimezone(self.zone):%Y-%m-%d %H:%M (%A)} {self.settings.timezone}",
            feed_summary=feed.feed_summary(recent, now, self.zone), pillar_status=feed.pillar_status(self.brand, recent),
            trends=self.trends.trends_for_prompt(), inspiration=self.trends.inspiration_for_prompt())
        plan, notes = self.planner.plan(instruction, items, ctx)
        self.store.update_submission(sid, status="planned", plan=plan.to_dict())
        needs_approval = self.settings.require_approval or bool(plan.risk_flags)

        prepared: list[tuple[PlannedPost, list[dict[str, Any]]]] = []
        for n, pp in enumerate(plan.posts):  # slow ffmpeg work happens outside the scheduling lock
            try:
                prepared.append((pp, self._prepare(sid, n, pp, items, notes)))
            except MediaError as exc:
                notes.append(f"Couldn't prepare the {pp.platform} {pp.kind}: {exc}")
        if not prepared:
            raise PlanError("; ".join(notes) or "Nothing could be prepared.")

        created: list[tuple[str, PlannedPost, datetime, str]] = []
        with self._lock:  # slot choice + insert must be atomic across concurrent submissions
            now = self.clock()
            for pp, files in prepared:
                when, when_note = self._schedule(pp, now)
                if when_note:
                    notes.append(when_note)
                max_len = IG_CAPTION_MAX if pp.platform == "instagram" else TIKTOK_TITLE_MAX
                caption = "" if pp.kind == "story" else compose_caption(pp.platform, pp.caption, pp.hashtags, max_len=max_len)
                pid = self.store.add_post(
                    submission_id=sid, platform=pp.platform, kind=pp.kind, caption=caption, hashtags=pp.hashtags,
                    media=files, publish_at=when, status=PENDING_APPROVAL if needs_approval else SCHEDULED,
                    pillar=plan.pillar, highlight=pp.highlight, why=pp.why)
                created.append((pid, pp, when, caption))
        self.store.update_submission(sid, status="scheduled")

        if self.draft_maker:
            for n, (_, pp, _, _) in enumerate(created):
                if pp.kind in ("reel", "video"):
                    try:
                        msg = self.draft_maker(sid, n, pp, [items[i] for i in pp.media_indexes])
                        if msg:
                            notes.append(msg)
                    except Exception as exc:  # optional extra: never block posting
                        notes.append(f"CapCut/Jianying draft skipped: {exc}")
        text, actions = self._plan_message(plan, created, notes, needs_approval, now)
        self.notifier.notify(text, actions=actions)

    # ------------------------------------------------------------------ media prep
    def _prepare(self, sid: str, n: int, pp: PlannedPost, items: list[MediaItem], notes: list[str]) -> list[dict[str, Any]]:
        out = self.settings.prepared_dir / sid / f"{n}-{pp.platform}-{pp.kind}"
        out.mkdir(parents=True, exist_ok=True)
        sel = [items[i] for i in pp.media_indexes]
        font = self.settings.font_path
        overlay = pp.on_screen_text
        if overlay and not media_mod.find_font(overlay, font):
            notes.append("I couldn't add the on-screen text: no suitable font is installed (CJK text needs e.g. fonts-noto-cjk).")
            overlay = ""
        files: list[Path] = []
        if pp.platform == "instagram":
            if pp.kind == "photo":
                files = [media_mod.prepare_feed_image(sel[0].path, out / "photo.jpg")]
            elif pp.kind == "carousel":
                for j, it in enumerate(sel):
                    files.append(media_mod.prepare_feed_image(it.path, out / f"{j}.jpg") if it.kind == "image" else
                                 media_mod.prepare_video(it.path, out / f"{j}.mp4", max_seconds=STORY_MAX_SECONDS,
                                                         width=1080, height=1350))
            elif pp.kind == "reel":
                files = [media_mod.prepare_video(sel[0].path, out / "reel.mp4", trim_start=pp.trim_start, trim_end=pp.trim_end,
                                                 max_seconds=min(self.settings.max_reel_seconds, 90),
                                                 overlay_text=overlay, font_path=font)]
            elif pp.kind == "story":
                it = sel[0]
                files = [media_mod.prepare_story_image(it.path, out / "story.jpg", overlay_text=overlay, font_path=font)
                         if it.kind == "image" else
                         media_mod.prepare_video(it.path, out / "story.mp4", trim_start=pp.trim_start, trim_end=pp.trim_end,
                                                 max_seconds=STORY_MAX_SECONDS, overlay_text=overlay, font_path=font)]
        else:
            if pp.kind == "video":
                files = [media_mod.prepare_video(sel[0].path, out / "tiktok.mp4", trim_start=pp.trim_start,
                                                 trim_end=pp.trim_end, max_seconds=TIKTOK_MAX_SECONDS,
                                                 overlay_text=overlay, font_path=font)]
            else:
                files = [media_mod.prepare_plain_jpeg(it.path, out / f"{j}.jpg") for j, it in enumerate(sel)]
        result = []
        for f in files:
            p = media_mod.probe(f)
            result.append({"path": str(f), "kind": p.kind, "duration": p.duration})
        return result

    # ------------------------------------------------------------------ scheduling
    def _schedule(self, pp: PlannedPost, now: datetime) -> tuple[datetime, str]:
        undo = timedelta(seconds=self.settings.undo_window_seconds)
        note = ""
        explicit = parse_when(pp.when, self.zone)
        if explicit is not None:
            if explicit >= now + timedelta(minutes=1):
                return explicit, ""
            note = "The time you asked for had already passed, so I used the next free slot."
        elif pp.when.strip().lower() in {"now", "asap"}:
            return now + undo, ""
        if pp.kind == "story" and not note:  # Stories are timely: go soon rather than wait for a feed slot
            return now + undo, ""
        plat = pp.platform
        existing = self.store.times_for(plat, feed.LIVE_STATUSES, FEED_KINDS[plat])
        slot = feed.choose_slot(
            existing=existing, now=now, windows=self.brand.posting_windows.get(plat, []), zone=self.zone,
            min_gap=timedelta(minutes=self.brand.min_gap_minutes.get(plat, 120)),
            max_per_day=self.brand.max_per_day.get(plat, 3), jitter=timedelta(minutes=self.rng.randint(-7, 7)))
        return slot, note

    # ------------------------------------------------------------------ owner messages
    def _is_dry(self, platform: str) -> bool:
        return isinstance(self.publishers.get(platform), DryRunPublisher)

    def _fmt(self, dt: datetime, now: datetime | None = None) -> str:
        local = dt.astimezone(self.zone)
        text = f"{local:%a %d %b %H:%M}"
        if now is not None:
            mins = int((dt - now).total_seconds() // 60)
            text += f" (in {mins // 60}h {mins % 60}m)" if mins >= 60 else f" (in {max(mins, 0)}m)"
        return text

    def _plan_message(self, plan, created, notes, needs_approval, now) -> tuple[str, list[tuple[str, str]]]:
        lines = []
        if any(self._is_dry(pp.platform) for _, pp, _, _ in created):
            lines.append("🧪 DRY RUN - nothing will really be posted (set DRY_RUN=false when you're ready).")
        lines.append(f"🗓 Plan: {plan.summary or 'your content'}")
        actions: list[tuple[str, str]] = []
        for i, (pid, pp, when, caption) in enumerate(created, 1):
            excerpt = (caption or pp.on_screen_text or "(Story)").replace("\n", " ")[:110]
            lines.append(f"{i}. {pp.platform.title()} {pp.kind} - {self._fmt(when, now)}"
                         + (f" - Highlight: {pp.highlight}" if pp.highlight else "") + f"\n   “{excerpt}”  [{pid}]")
            if needs_approval:
                actions.append((f"✅ Approve #{i}", f"approve:{pid}"))
            actions.append((f"✖ Cancel #{i}", f"cancel:{pid}"))
        meta = [f"Pillar: {plan.pillar}"] if plan.pillar else []
        if plan.trend_used:
            meta.append(f"Trend: {plan.trend_used}")
        if meta:
            lines.append(" · ".join(meta))
        if needs_approval:
            why = f"flagged {', '.join(plan.risk_flags)}" if plan.risk_flags else "approval mode is on"
            lines.append(f"⚠️ Waiting for your OK ({why}).")
        for n in notes:
            lines.append(f"ℹ️ {n}")
        if plan.edit_brief:
            lines.append(f"🎬 CapCut/Jianying polish idea:\n{plan.edit_brief}")
        if plan.message_to_owner:
            lines.append(plan.message_to_owner)
        return "\n".join(lines), actions

    # ------------------------------------------------------------------ owner actions
    def cancel(self, post_id: str) -> bool:
        ok = self.store.cancel_post(post_id)
        self.notifier.notify(f"✖ Cancelled [{post_id}]." if ok else f"Couldn't cancel [{post_id}] (already posted, cancelled or unknown).")
        return ok

    def approve(self, post_id: str) -> bool:
        post = self.store.get_post(post_id)
        if not post:
            self.notifier.notify(f"Unknown post [{post_id}].")
            return False
        now = self.clock()
        when = max(parse_iso(post["publish_at"]), now + timedelta(seconds=20))
        ok = self.store.approve_post(post_id, when)
        self.notifier.notify(f"✅ Approved [{post_id}] - going out {self._fmt(when, now)}." if ok
                             else f"[{post_id}] isn't waiting for approval.")
        return ok

    def retry(self, post_id: str) -> bool:
        post = self.store.get_post(post_id)
        if not post or post["status"] != FAILED:
            self.notifier.notify(f"[{post_id}] isn't a failed post.")
            return False
        self.store.set_post(post_id, status=SCHEDULED, publish_at=self.clock(), attempts=0, error=None)
        self.notifier.notify(f"🔁 Retrying [{post_id}] now.")
        return True

    # ------------------------------------------------------------------ publishing
    def tick(self, now: datetime | None = None) -> int:
        """Publish everything that is due. Safe to call repeatedly / concurrently."""
        now = now or self.clock()
        done = 0
        for post in self.store.due_posts(now):
            if self.store.claim_post(post["id"]):
                self._publish_one(post, now)
                done += 1
        return done

    def _publish_one(self, post: dict[str, Any], now: datetime) -> None:
        pid, platform, kind = post["id"], post["platform"], post["kind"]
        label = f"{platform.title()} {kind} [{pid}]"
        publisher = self.publishers.get(platform)
        try:
            if publisher is None:
                raise PublishError(f"No publisher configured for {platform}.", retryable=False)
            media = [PreparedMedia(path=m["path"], kind=m["kind"], duration=m.get("duration", 0.0)) for m in post["media"]]
            if publisher.needs_urls(kind):
                for m in media:
                    m.url = self.host.publish(m.path)
            res = publisher.publish(PublishRequest(post_id=pid, platform=platform, kind=kind, caption=post["caption"], media=media))
        except PublishError as exc:
            self._on_failure(post, exc, label, now)
            return
        except Exception as exc:
            log.exception("Unexpected publish error for %s", pid)
            self.store.set_post(pid, status=FAILED, error=f"{type(exc).__name__}: {exc}")
            self.notifier.notify(f"❌ {label} failed unexpectedly: {exc}", actions=[("🔁 Retry", f"retry:{pid}")])
            return
        self.store.set_post(pid, status=PUBLISHED, remote_id=res.remote_id, permalink=res.permalink, note=res.note, error=None)
        if kind == "story" and post.get("highlight"):
            self.store.add_highlight_pending(pid, post["highlight"])
        link = f"\n{res.permalink}" if res.permalink else ""
        self.notifier.notify(f"✅ Posted {label}{link}" + (f"\nℹ️ {res.note}" if res.note else ""))

    def _on_failure(self, post: dict[str, Any], exc: PublishError, label: str, now: datetime) -> None:
        pid, attempts = post["id"], post["attempts"] + 1
        if exc.retryable and attempts < MAX_ATTEMPTS:
            when = now + timedelta(minutes=BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)])
            self.store.set_post(pid, status=SCHEDULED, attempts=attempts, publish_at=when, error=str(exc))
            self.notifier.notify(f"⚠️ {label} hit a snag ({exc}). Trying again {self._fmt(when, now)}.")
        else:
            self.store.set_post(pid, status=FAILED, attempts=attempts, error=str(exc))
            self.notifier.notify(f"❌ {label} failed: {exc}", actions=[("🔁 Retry", f"retry:{pid}")])

    def recover(self) -> int:
        """After a crash/restart: posts stuck in 'publishing' may or may not have gone out - flag, never re-send."""
        stuck = self.store.list_posts([PUBLISHING], limit=100)
        for p in stuck:
            self.store.set_post(p["id"], status=FAILED, error="Interrupted while publishing - check the platform before retrying.")
            self.notifier.notify(f"⚠️ {p['platform'].title()} {p['kind']} [{p['id']}] was interrupted mid-publish. "
                                 "Check the app to see if it went live, then retry if not.", actions=[("🔁 Retry", f"retry:{p['id']}")])
        return len(stuck)

    # ------------------------------------------------------------------ reports & maintenance
    def queue_text(self) -> str:
        upcoming = self.store.list_posts([SCHEDULED, PENDING_APPROVAL, PUBLISHING], limit=20)
        if not upcoming:
            return "Nothing queued. Send me a photo or video!"
        now = self.clock()
        lines = ["📬 Queue:"]
        for p in upcoming:
            tag = " (needs approval)" if p["status"] == PENDING_APPROVAL else ""
            ex = (p["caption"] or "(Story)").replace("\n", " ")[:60]
            lines.append(f"- {p['platform']} {p['kind']} {self._fmt(parse_iso(p['publish_at']), now)}{tag} “{ex}” [{p['id']}]")
        return "\n".join(lines)

    def weekly_review(self) -> str:
        now = self.clock()
        since = now - timedelta(days=7)
        posts = self.store.list_posts([PUBLISHED, FAILED], limit=200, newest_first=True)
        week = [p for p in posts if parse_iso(p["publish_at"]) >= since]
        done = [p for p in week if p["status"] == PUBLISHED]
        failed = [p for p in week if p["status"] == FAILED]
        by: dict[str, int] = {}
        for p in done:
            by[f"{p['platform']} {p['kind']}"] = by.get(f"{p['platform']} {p['kind']}", 0) + 1
        lines = ["📊 Weekly feed review", f"Published {len(done)} post(s) this week" +
                 (": " + ", ".join(f"{v}× {k}" for k, v in sorted(by.items())) if by else ".")]
        if failed:
            lines.append(f"⚠️ {len(failed)} failed - see /queue or ask me to retry.")
        status = feed.pillar_status(self.brand, posts)
        if status:
            lines.append(status)
        top = self.trends.trends_for_prompt(3)
        if top:
            lines.append("Trends I'm watching:\n" + top)
        checklist = feed.highlight_checklist(self.store, self.brand, self.zone)
        if checklist:
            lines.append(checklist)
        return "\n".join(lines)

    def chat(self, text: str) -> str:
        """Free-text conversation with the account manager (no posting power)."""
        context = "\n\n".join(x for x in [self.queue_text(), "Recent feed:\n" + feed.feed_summary(
            self.store.list_posts(feed.LIVE_STATUSES, limit=12, newest_first=True), self.clock(), self.zone),
            "Trends:\n" + self.trends.trends_for_prompt(6)] if x)
        return self.planner.llm.text(
            system=("You are the owner's Instagram + TikTok account manager. Answer briefly and practically. You can "
                    "advise on content, captions, trends and the queue, but you cannot post: tell them to send media or "
                    "use commands (/queue, /trends, /cancel <id>) when action is needed.\n\n"
                    f"BRAND BRIEF:\n{self.brand.prompt_block()}"),
            content=f"{context}\n\nOwner says: {text}")

    def cleanup(self) -> None:
        """Delete old prepared/inbox files and expired public media."""
        self.host.cleanup()
        cutoff = _time.time() - RETENTION_DAYS * 86400
        for root in (self.settings.prepared_dir, self.settings.inbox_dir):
            if not root.exists():
                continue
            for d in root.iterdir():
                try:
                    if d.stat().st_mtime < cutoff:
                        shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink(missing_ok=True)
                except OSError:
                    log.debug("cleanup skipped %s", d)
