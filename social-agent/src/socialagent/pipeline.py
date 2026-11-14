"""Publish-side engine: turn APPROVED draft targets into scheduled posts, publish them when due, report back.

(Intake - looking at media, writing captions, the draft card and its buttons - lives in ``drafts.py``.)

Safety properties enforced here (and covered by tests):
* nothing is published before its scheduled time, and "post now" still waits the undo window;
* a post is claimed atomically, so it can never be published twice by concurrent ticks;
* an ambiguous failure is never retried automatically (could create a duplicate post);
* posts left "publishing" by a crash are marked failed for review, never silently re-sent;
* nothing reaches this module without the owner's explicit approval of a draft.
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
from typing import Any, Callable

from . import feed
from . import media as media_mod
from .brain import PlanError
from .config import BrandProfile, Settings
from .hosting import MediaHost
from .media import MediaError
from .models import FAILED, HANDED_OFF, MediaItem, PUBLISHED, PUBLISHING, SCHEDULED, PlannedPost
from .notify import ListNotifier, Notifier
from .publishers import DryRunPublisher, PreparedMedia, PublishError, PublishRequest, Publisher
from .store import Store
from .textutil import IG_CAPTION_MAX, TIKTOK_TITLE_MAX, compose_caption
from .timeutil import parse_iso, parse_when, tz, utcnow
from .trends import TrendResearcher

__all__ = ["ListNotifier", "Notifier", "Pipeline"]

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_MINUTES = (2, 5, 15)
STORY_MAX_SECONDS = 60
TIKTOK_MAX_SECONDS = 180
FEED_KINDS = {"instagram": ("photo", "carousel", "reel"), "tiktok": ("video", "photo")}
RETENTION_DAYS = 14


class Pipeline:
    def __init__(self, *, settings: Settings, brand: BrandProfile, store: Store, trends: TrendResearcher,
                 publishers: dict[str, Publisher], host: MediaHost, notifier: Notifier, manual: Publisher | None = None,
                 clock: Callable[[], datetime] = utcnow, rng: random.Random | None = None,
                 draft_maker: Callable[[str, int, PlannedPost, list[MediaItem]], str | None] | None = None):
        self.settings, self.brand, self.store, self.trends = settings, brand, store, trends
        self.publishers, self.manual, self.host, self.notifier = publishers, manual, host, notifier
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

    def reload_brand(self) -> bool:
        """Re-read brand.yaml if it changed on disk. Returns True when the profile was replaced."""
        m = self._mtime()
        if m and m != self._brand_mtime:
            self.brand = self.trends.brand = BrandProfile.load(self.settings.brand_file)
            self._brand_mtime = m
            log.info("Brand profile reloaded")
            return True
        return False

    # ------------------------------------------------------------------ approved drafts -> scheduled posts
    def schedule_targets(self, sid: str, items: list[MediaItem], targets: list[dict[str, Any]], *, pillar: str = "",
                         method_for: Callable[[str], str] = lambda platform: "api",
                         now_flag: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
        """Prepare media and create publish jobs for approved targets. Returns (created posts, notes for the owner)."""
        notes: list[str] = []
        prepared: list[tuple[dict[str, Any], str, list[dict[str, Any]]]] = []
        for n, t in enumerate(targets):  # slow ffmpeg work happens outside the scheduling lock
            method = method_for(t["platform"])
            try:
                prepared.append((t, method, self._prepare(sid, n, t, items, notes, method)))
            except MediaError as exc:
                notes.append(f"Couldn't prepare the {t['platform']} {t['kind']}: {exc}")
        if not prepared:
            raise PlanError("; ".join(notes) or "Nothing could be prepared.")

        created: list[dict[str, Any]] = []
        with self._lock:  # slot choice + insert must be atomic across concurrent approvals
            now = self.clock()
            for t, method, files in prepared:
                when, when_note = self._schedule(t["platform"], t["kind"], t.get("when", ""), now, force_now=now_flag)
                if when_note:
                    notes.append(when_note)
                if t["kind"] == "story":  # Stories have no caption: the field carries the suggested text sticker
                    caption = t.get("on_screen_text", "")
                else:
                    max_len = IG_CAPTION_MAX if t["platform"] == "instagram" else TIKTOK_TITLE_MAX
                    caption = compose_caption(t["platform"], t.get("caption", ""), t.get("hashtags", []), max_len=max_len)
                pid = self.store.add_post(
                    submission_id=sid, platform=t["platform"], kind=t["kind"], caption=caption, hashtags=t.get("hashtags", []),
                    media=files, publish_at=when, status=SCHEDULED, pillar=pillar, highlight=t.get("highlight", ""),
                    why=t.get("why", ""), method=method)
                created.append({"id": pid, "platform": t["platform"], "kind": t["kind"], "when": when, "method": method,
                                "caption": caption, "highlight": t.get("highlight", "")})
        if self.draft_maker and self.settings.edit_level == "creative":  # Phase 2 extra: never blocks posting
            for n, t in enumerate(targets):
                if t["kind"] in ("reel", "video"):
                    try:
                        msg = self.draft_maker(sid, n, PlannedPost(platform=t["platform"], kind=t["kind"],
                                                                   on_screen_text=t.get("on_screen_text", "")),
                                               [items[i] for i in t["media_indexes"]])
                        if msg:
                            notes.append(msg)
                    except Exception as exc:
                        notes.append(f"CapCut/Jianying draft skipped: {exc}")
        return created, notes

    # ------------------------------------------------------------------ media prep (EDIT_LEVEL)
    def _prepare(self, sid: str, n: int, t: dict[str, Any], items: list[MediaItem], notes: list[str],
                 method: str) -> list[dict[str, Any]]:
        """none: originals, fixed only where the platform would reject them. fit: + reframe. creative: + hook text.

        Manual hand-off always uses the untouched originals (you post from your own gallery).
        """
        sel = [items[i] for i in t["media_indexes"]]
        if method == "manual":
            return [{"path": it.path, "kind": it.kind, "duration": it.duration} for it in sel]
        out = self.settings.prepared_dir / sid / f"{n}-{t['platform']}-{t['kind']}"
        out.mkdir(parents=True, exist_ok=True)
        level, font = self.settings.edit_level, self.settings.font_path
        overlay = t.get("on_screen_text", "") if level == "creative" else ""
        if overlay and not media_mod.find_font(overlay, font):
            notes.append("I couldn't add the on-screen text: no suitable font is installed (CJK text needs e.g. fonts-noto-cjk).")
            overlay = ""
        platform, kind = t["platform"], t["kind"]
        max_reel = min(self.settings.max_reel_seconds, 90)
        files: list[Path] = []
        if platform == "instagram":
            if kind in ("photo", "carousel"):
                for j, it in enumerate(sel):
                    dst = out / (f"{j}.jpg" if kind == "carousel" else "photo.jpg")
                    if it.kind == "image":
                        files.append(media_mod.compliant_image(it.path, dst, ig_feed=True) if level == "none"
                                     else media_mod.prepare_feed_image(it.path, dst))
                    else:  # a video inside a carousel
                        vdst = dst.with_suffix(".mp4")
                        files.append(media_mod.compliant_video(it.path, vdst, max_seconds=STORY_MAX_SECONDS) if level == "none"
                                     else media_mod.prepare_video(it.path, vdst, max_seconds=STORY_MAX_SECONDS, width=1080, height=1350))
            elif kind == "reel":
                files = [media_mod.compliant_video(sel[0].path, out / "reel.mp4", max_seconds=max_reel) if level == "none"
                         else media_mod.prepare_video(sel[0].path, out / "reel.mp4", max_seconds=max_reel,
                                                      overlay_text=overlay, font_path=font)]
            elif kind == "story":
                it = sel[0]
                if it.kind == "image":
                    files = [media_mod.compliant_image(it.path, out / "story.jpg") if level == "none"
                             else media_mod.prepare_story_image(it.path, out / "story.jpg", overlay_text=overlay, font_path=font)]
                else:
                    files = [media_mod.compliant_video(it.path, out / "story.mp4", max_seconds=STORY_MAX_SECONDS) if level == "none"
                             else media_mod.prepare_video(it.path, out / "story.mp4", max_seconds=STORY_MAX_SECONDS,
                                                          overlay_text=overlay, font_path=font)]
        else:
            if kind == "video":
                files = [media_mod.compliant_video(sel[0].path, out / "tiktok.mp4", max_seconds=TIKTOK_MAX_SECONDS) if level == "none"
                         else media_mod.prepare_video(sel[0].path, out / "tiktok.mp4", max_seconds=TIKTOK_MAX_SECONDS,
                                                      overlay_text=overlay, font_path=font)]
            else:
                files = [media_mod.compliant_image(it.path, out / f"{j}.jpg") if level == "none"
                         else media_mod.prepare_plain_jpeg(it.path, out / f"{j}.jpg") for j, it in enumerate(sel)]
        result = []
        for f in files:
            p = media_mod.probe(f)
            result.append({"path": str(f), "kind": p.kind, "duration": p.duration})
        return result

    # ------------------------------------------------------------------ scheduling
    def _schedule(self, platform: str, kind: str, when_str: str, now: datetime, *, force_now: bool = False) -> tuple[datetime, str]:
        undo = timedelta(seconds=self.settings.undo_window_seconds)
        if force_now:
            return now + undo, ""
        note = ""
        explicit = parse_when(when_str, self.zone)
        if explicit is not None:
            if explicit >= now + timedelta(minutes=1):
                return explicit, ""
            note = "The time you asked for had already passed, so I used the next free slot."
        elif (when_str or "").strip().lower() in {"now", "asap"}:
            return now + undo, ""
        if kind == "story" and not note:  # Stories are timely: go soon rather than wait for a feed slot
            return now + undo, ""
        existing = self.store.times_for(platform, feed.LIVE_STATUSES, FEED_KINDS[platform])
        slot = feed.choose_slot(
            existing=existing, now=now, windows=self.brand.posting_windows.get(platform, []), zone=self.zone,
            min_gap=timedelta(minutes=self.brand.min_gap_minutes.get(platform, 120)),
            max_per_day=self.brand.max_per_day.get(platform, 3), jitter=timedelta(minutes=self.rng.randint(-7, 7)))
        return slot, note

    def suggest_slot(self, platform: str, kind: str, when_str: str = "") -> datetime:
        """When this target would go out if approved right now (shown on the draft card; not reserved)."""
        return self._schedule(platform, kind, when_str, self.clock())[0]

    def is_dry(self, platform: str, method: str = "api") -> bool:
        return method != "manual" and isinstance(self.publishers.get(platform), DryRunPublisher)

    def fmt(self, dt: datetime, now: datetime | None = None) -> str:
        local = dt.astimezone(self.zone)
        text = f"{local:%a %d %b %H:%M}"
        if now is not None:
            mins = int((dt - now).total_seconds() // 60)
            text += f" (in {mins // 60}h {mins % 60}m)" if mins >= 60 else f" (in {max(mins, 0)}m)"
        return text

    # ------------------------------------------------------------------ owner actions on posts
    def cancel(self, post_id: str) -> bool:
        ok = self.store.cancel_post(post_id)
        self.notifier.notify(f"✖ Cancelled [{post_id}]." if ok else f"Couldn't cancel [{post_id}] (already posted, cancelled or unknown).")
        return ok

    def retry(self, post_id: str) -> bool:
        post = self.store.get_post(post_id)
        if not post or post["status"] != FAILED:
            self.notifier.notify(f"[{post_id}] isn't a failed post.")
            return False
        self.store.set_post(post_id, status=SCHEDULED, publish_at=self.clock(), attempts=0, error=None)
        self.notifier.notify(f"🔁 Retrying [{post_id}] now.")
        return True

    def confirm_posted(self, post_id: str) -> bool:
        """Manual method: the owner says they posted it. Stories then join the Highlights checklist."""
        post = self.store.get_post(post_id)
        ok = self.store.confirm_posted(post_id)
        if ok and post and post["kind"] == "story" and post.get("highlight"):
            self.store.add_highlight_pending(post_id, post["highlight"])
        self.notifier.notify(f"✅ Marked [{post_id}] as posted." if ok else f"[{post_id}] isn't waiting for you to post it.")
        return ok

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
        publisher = self.manual if post.get("method") == "manual" else self.publishers.get(platform)
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
        if res.handed_off:  # the manual publisher already sent the package; the owner confirms when they have posted
            self.store.set_post(pid, status=HANDED_OFF, remote_id=res.remote_id, note=res.note, error=None)
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
            self.notifier.notify(f"⚠️ {label} hit a snag ({exc}). Trying again {self.fmt(when, now)}.")
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
        upcoming = self.store.list_posts([SCHEDULED, PUBLISHING, HANDED_OFF], limit=20)
        drafts = self.store.list_drafts(["ready"], limit=10)
        if not upcoming and not drafts:
            return "Nothing queued. Send me a photo or video!"
        now = self.clock()
        lines = []
        if drafts:
            lines.append(f"📝 {len(drafts)} draft(s) waiting for you: " + ", ".join(f"[{d['id']}]" for d in drafts) + "  (/drafts)")
        if upcoming:
            lines.append("📬 Queue:")
        for p in upcoming:
            tag = " (waiting for you to post it)" if p["status"] == HANDED_OFF else (" (manual reminder)" if p.get("method") == "manual" else "")
            ex = (p["caption"] or "(Story)").replace("\n", " ")[:60]
            lines.append(f"- {p['platform']} {p['kind']} {self.fmt(parse_iso(p['publish_at']), now)}{tag} “{ex}” [{p['id']}]")
        return "\n".join(lines)

    def weekly_review(self) -> str:
        now = self.clock()
        since = now - timedelta(days=7)
        posts = self.store.list_posts([PUBLISHED, FAILED, HANDED_OFF], limit=200, newest_first=True)
        week = [p for p in posts if parse_iso(p["publish_at"]) >= since]
        done = [p for p in week if p["status"] == PUBLISHED]
        failed = [p for p in week if p["status"] == FAILED]
        waiting = [p for p in week if p["status"] == HANDED_OFF]
        by: dict[str, int] = {}
        for p in done:
            by[f"{p['platform']} {p['kind']}"] = by.get(f"{p['platform']} {p['kind']}", 0) + 1
        lines = ["📊 Weekly feed review", f"Published {len(done)} post(s) this week" +
                 (": " + ", ".join(f"{v}× {k}" for k, v in sorted(by.items())) if by else ".")]
        if waiting:
            lines.append(f"⏳ {len(waiting)} hand-off(s) not confirmed yet: tap “I posted it” on the reminder, or /queue.")
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
        return self.trends.llm.text(
            system=("You are the owner's Instagram + TikTok account manager. Answer briefly and practically. You can "
                    "advise on content, captions, trends and the queue, but you cannot post: tell them to send media or "
                    "use commands (/queue, /drafts, /trends, /cancel <id>) when action is needed.\n\n"
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
