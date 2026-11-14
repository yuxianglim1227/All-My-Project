"""Draft-first intake: media in -> understand (vision, once) -> write (text, per style) -> a card the owner reviews.

Lifecycle of a draft (``drafts.state``)::

    drafting --(understand + write)--> ready --approve--> approved (posts scheduled via the Pipeline)
        |                                |  ^
        +--> failed --retry--> drafting  |  +-- tone / language / targets / rewrite / edit (stay ready)
                                         +--> discarded

A draft is a *mutable document*; only ``approve`` turns it into publish jobs (``posts``), so nothing can reach a
platform without the owner's explicit tap. Changing tone or language re-runs only the cheap text-only Writer.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import feed
from . import media as media_mod
from .brain import PlanContext, PlanError, Understander, Writer, eligible_targets
from .cards import Button, Card
from .llm import LLMError
from .media import MediaError
from .models import APPROVED, DISCARDED, DRAFT_FAILED, DRAFTING, READY, ContentPlan, MediaItem
from .notify import Notifier
from .pipeline import Pipeline
from .prefs import METHODS, PLATFORMS, Prefs
from .store import Store
from .styles import StyleCatalog
from .timeutil import tz

log = logging.getLogger(__name__)

PREVIEW_CHARS = 380
CARD_LIMIT = 3900  # Telegram messages max out at 4096 characters
PLATFORM_ICON = {"instagram": "📷", "tiktok": "🎵"}


@dataclass
class ActionResult:
    toast: str = ""  # short confirmation for the tap (Telegram: the little pop-up)
    prompt: str = ""  # non-empty = the channel should ask the owner for free text next, then call back


class DraftService:
    def __init__(self, *, settings, store: Store, pipeline: Pipeline, understander: Understander, writer: Writer,
                 prefs: Prefs, catalog: StyleCatalog, notifier: Notifier):
        self.settings, self.store, self.pipeline = settings, store, pipeline
        self.understander, self.writer, self.prefs, self.catalog, self.notifier = understander, writer, prefs, catalog, notifier
        self.zone = tz(settings.timezone)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._confirming: set[str] = set()  # drafts waiting for "Yes, approve anyway"
        self._flash: dict[str, str] = {}  # one-shot notes shown on the next card render

    # ------------------------------------------------------------------ helpers
    def _lock(self, did: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(did, threading.RLock())

    @property
    def brand(self):
        return self.pipeline.brand

    def _sync_brand(self) -> None:
        if self.pipeline.reload_brand():
            b = self.pipeline.brand
            self.understander.brand = self.writer.brand = self.prefs.brand = b
            self.catalog = self.prefs.catalog = StyleCatalog.from_brand(b.custom_tones)

    def _items(self, d: dict[str, Any]) -> list[MediaItem]:
        sub = self.store.get_submission(d["submission_id"]) or {}
        return [MediaItem.from_dict(m) for m in json.loads(sub.get("media_json") or "[]")]

    def _context(self, now: datetime) -> PlanContext:
        recent = self.store.list_posts(feed.LIVE_STATUSES, limit=30, newest_first=True)
        return PlanContext(
            now_local=f"{now.astimezone(self.zone):%Y-%m-%d %H:%M (%A)} {self.settings.timezone}",
            feed_summary=feed.feed_summary(recent, now, self.zone), pillar_status=feed.pillar_status(self.brand, recent),
            trends=self.pipeline.trends.trends_for_prompt(), inspiration=self.pipeline.trends.inspiration_for_prompt())

    def _present(self, d: dict[str, Any], card: Card) -> None:
        handle = self.notifier.present(card, handle=d.get("card_ref"))
        if handle != d.get("card_ref"):
            self.store.update_draft(d["id"], card_ref=handle)
            d["card_ref"] = handle

    # ------------------------------------------------------------------ create
    def create(self, media_paths: list[Path | str], instruction: str = "", source: str = "cli",
               handle: str | None = None) -> str | None:
        """Look at the media, write a draft in the owner's style and show the card. Returns the draft id."""
        self._sync_brand()
        try:
            items = [media_mod.probe(p) for p in media_paths]
        except MediaError as exc:
            self.notifier.notify(f"❌ I couldn't read that file: {exc}")
            return None
        if not items:
            self.notifier.notify("Send me a photo or video (add an instruction if you like) and I'll draft it.")
            return None
        sid = self.store.add_submission(source=source, instruction=instruction, media=[i.to_dict() for i in items])
        hints = self.catalog.parse_hints(instruction)
        did = self.store.add_draft(submission_id=sid, tone=hints.tone or self.prefs.tone, lang=hints.language or self.prefs.language,
                                   instruction=instruction, state=DRAFTING)
        if handle:  # the acknowledgement message the channel already sent: persist it so EVERY later update edits it in place
            self.store.update_draft(did, card_ref=handle)
        d = self.store.get_draft(did)
        kinds = [i.kind for i in items]
        busy = Card("draft", did, f"📥 Got it ({kinds.count('image')} photo(s), {kinds.count('video')} video(s)). Reading it and "
                                  f"drafting a {self.catalog.tone(d['tone']).label.lower()} caption…")
        self._present(d, busy)
        with self._lock(did):
            self._run_draft(did, items, instruction)
        return did

    def _run_draft(self, did: str, items: list[MediaItem], instruction: str) -> None:
        try:
            self._understand_and_write(did, items, instruction)
        except (LLMError, PlanError) as exc:
            self._fail(did, str(exc))
        except Exception as exc:  # never lose the owner's media silently
            log.exception("Unexpected error while drafting %s", did)
            self._fail(did, f"{type(exc).__name__}: {exc}")

    def _understand_and_write(self, did: str, items: list[MediaItem], instruction: str) -> None:
        brief, notes = self.understander.understand(instruction, items, self._context(self.pipeline.clock()))
        targets: list[dict[str, Any]] = []
        for i, e in enumerate(eligible_targets(items), 1):
            sug = next((p for p in brief.posts if (p.platform, p.kind) == e.key), None)
            targets.append({
                "id": f"t{i}", "platform": e.platform, "kind": e.kind, "label": e.label, "short": e.short,
                "media_indexes": list(sug.media_indexes) if sug else list(e.media_indexes), "selected": sug is not None,
                "caption": "", "hashtags": [], "on_screen_text": "", "when": sug.when if sug else "next_slot",
                "highlight": sug.highlight if sug else "", "why": sug.why if sug else "", "written": False})
        if targets and not any(t["selected"] for t in targets):
            targets[0]["selected"] = True
        self.store.update_draft(did, brief=brief.to_dict(), targets=targets)
        if notes:
            self._flash[did] = " ".join(notes)
        self._write(did)

    def _write(self, did: str, only: list[str] | None = None, change: str = "") -> None:
        """(Re)write captions for the selected targets in the draft's current style. Text only: fast and cheap."""
        d = self.store.get_draft(did)
        targets = d["targets"]
        wanted = [t for t in targets if t["selected"] and (only is None or t["id"] in only)]
        if not wanted:
            self.store.update_draft(did, state=READY, error=None)
            self._present(d, self.card(did))
            return
        now = self.pipeline.clock()
        written = self.writer.write(
            brief=ContentPlan.from_dict(d["brief"] or {}), targets=wanted, tone=self.catalog.tone(d["tone"]),
            language=self.catalog.language(d["lang"]), instruction=d["instruction"], change=change,
            previous={t["id"]: t["caption"] for t in wanted if t["written"]}, trends=self.pipeline.trends.trends_for_prompt(),
            inspiration=self.pipeline.trends.inspiration_for_prompt(),
            now_local=f"{now.astimezone(self.zone):%Y-%m-%d %H:%M (%A)} {self.settings.timezone}")
        for t in wanted:
            w = written[t["id"]]
            t.update(caption=w["caption"], hashtags=w["hashtags"], on_screen_text=w["on_screen_text"], written=True)
            if w["when"]:
                t["when"] = w["when"]
        self.store.update_draft(did, targets=targets, state=READY, error=None)
        self._present(self.store.get_draft(did), self.card(did))

    def _fail(self, did: str, message: str) -> None:
        self.store.update_draft(did, state=DRAFT_FAILED, error=message)
        d = self.store.get_draft(did)
        card = Card("draft", did, f"❌ I couldn't draft this: {message}\nYour media is saved, nothing was posted.",
                    [[Button("🔄 Try again", "retry"), Button("🗑 Discard", "discard")]])
        self._present(d, card)

    # ------------------------------------------------------------------ owner edits (each keeps the draft 'ready')
    def _editable(self, did: str) -> dict[str, Any] | None:
        d = self.store.get_draft(did)
        if d is None:
            self.notifier.notify(f"I can't find draft [{did}].")
            return None
        if d["state"] not in (READY, DRAFT_FAILED):
            self.notifier.notify(f"Draft [{did}] is already {d['state']}.")
            return None
        return d

    def _guarded_write(self, did: str, **kw: Any) -> None:
        """Run a rewrite; on failure keep the old captions and say so on the card."""
        try:
            self._write(did, **kw)
        except (LLMError, PlanError) as exc:
            self._flash[did] = f"⚠️ Couldn't rewrite: {exc} (kept the previous text)"
            self._present(self.store.get_draft(did), self.card(did))

    def set_tone(self, did: str, tone: str) -> None:
        with self._lock(did):
            if (d := self._editable(did)) and self.catalog.has_tone(tone) and d["state"] == READY:
                self.store.update_draft(did, tone=tone)
                self._guarded_write(did)

    def set_language(self, did: str, lang: str) -> None:
        with self._lock(did):
            if (d := self._editable(did)) and self.catalog.has_language(lang) and d["state"] == READY:
                self.store.update_draft(did, lang=lang)
                self._guarded_write(did)

    def toggle_target(self, did: str, tid: str) -> None:
        with self._lock(did):
            d = self._editable(did)
            if not d or d["state"] != READY:
                return
            t = next((x for x in d["targets"] if x["id"] == tid), None)
            if t is None:
                return
            t["selected"] = not t["selected"]
            self.store.update_draft(did, targets=d["targets"])
            if t["selected"] and not t["written"]:
                self._guarded_write(did, only=[tid])
            else:
                self._present(self.store.get_draft(did), self.card(did))

    def rewrite(self, did: str, change: str = "") -> None:
        with self._lock(did):
            if (d := self._editable(did)) and d["state"] == READY:
                self._guarded_write(did, change=change)

    def instruct(self, did: str, text: str) -> None:
        """Free text from the owner: '=my exact caption' sets it literally, anything else is a change request."""
        text = text.strip()
        if text.startswith("="):
            self.set_caption(did, text[1:].strip())
        elif text:
            self.rewrite(did, text)

    def set_caption(self, did: str, text: str) -> None:
        """Use the owner's own words, unchanged, for every selected post caption (hashtags kept unless they typed their own)."""
        with self._lock(did):
            d = self._editable(did)
            if not d or d["state"] != READY or not text.strip():
                return
            for t in d["targets"]:
                if t["selected"] and t["kind"] != "story":
                    t["caption"], t["written"] = text.strip(), True
                    if "#" in text:
                        t["hashtags"] = []
            self.store.update_draft(did, targets=d["targets"])
            self._present(self.store.get_draft(did), self.card(did))

    def retry(self, did: str) -> None:
        with self._lock(did):
            d = self.store.get_draft(did)
            if d and d["state"] == DRAFT_FAILED:
                self.store.update_draft(did, state=DRAFTING, error=None)
                self._present(d, Card("draft", did, "🔄 Trying again…"))
                self._run_draft(did, self._items(d), d["instruction"])

    def discard(self, did: str) -> None:
        with self._lock(did):
            d = self.store.get_draft(did)
            if d and d["state"] in (READY, DRAFT_FAILED, DRAFTING):
                self.store.update_draft(did, state=DISCARDED)
                self._present(d, Card("draft", did, f"🗑 Discarded draft [{did}]. Nothing was posted."))

    # ------------------------------------------------------------------ approve -> scheduled posts
    def approve(self, did: str, *, now: bool = False, confirmed: bool = False) -> None:
        with self._lock(did):
            d = self._editable(did)
            if not d or d["state"] != READY:
                return
            selected = [t for t in d["targets"] if t["selected"]]
            if not selected:
                self._flash[did] = "Pick at least one place to post (IG post / story / TikTok)."
                self._present(d, self.card(did))
                return
            brief = d["brief"] or {}
            flags = brief.get("risk_flags") or []
            if flags and not confirmed and did not in self._confirming:
                self._confirming.add(did)
                self._flash[did] = (f"⚠️ I flagged: {', '.join(flags)}. Tap “Yes, approve anyway” if that's fine, or fix it first.")
                self._present(d, self.card(did))
                return
            self._confirming.discard(did)
            self._present(d, Card("draft", did, "⏳ Scheduling…"))
            try:
                created, notes = self.pipeline.schedule_targets(
                    d["submission_id"], self._items(d), selected, pillar=brief.get("pillar", ""),
                    method_for=self.prefs.method, now_flag=now)
            except (PlanError, MediaError) as exc:
                self._flash[did] = f"❌ Couldn't schedule: {exc}"
                self._present(d, self.card(did))
                return
            self.store.update_draft(did, state=APPROVED)
            self._present(d, self._approved_card(did, created, notes))

    def _approved_card(self, did: str, created: list[dict[str, Any]], notes: list[str]) -> Card:
        now = self.pipeline.clock()
        lines = [f"✅ Approved draft [{did}]"]
        rows: list[list[Button]] = []
        for i, c in enumerate(created, 1):
            how = ("I'll send you a ready package then" if c["method"] == "manual"
                   else ("dry run - nothing will really be posted" if self.pipeline.is_dry(c["platform"]) else "auto-post"))
            lines.append(f"{i}. {c['platform'].title()} {c['kind']} - {self.pipeline.fmt(c['when'], now)} ({how}) [{c['id']}]")
            if c.get("highlight"):
                lines.append(f"   Highlight: {c['highlight']}")
            rows.append([Button(f"✖ Cancel #{i}", f"cancel:{c['id']}")])
        lines += [f"ℹ️ {n}" for n in notes]
        return Card("post", did, "\n".join(lines), rows)

    # ------------------------------------------------------------------ cards
    def _method_label(self, platform: str) -> str:
        if self.prefs.method(platform) == "manual":
            return "you post it (I send a ready package)"
        if self.pipeline.is_dry(platform):
            return "dry run - nothing is posted"
        if platform == "tiktok" and self.settings.tiktok_mode == "inbox":
            return "saved to your TikTok drafts"
        return "auto-post"

    def card(self, did: str) -> Card:
        d = self.store.get_draft(did)
        if d is None:
            return Card("draft", did, f"Draft [{did}] not found.")
        text = self._card_text(d, PREVIEW_CHARS)
        if len(text) > CARD_LIMIT:
            text = self._card_text(d, 140)
        return Card("draft", did, text, self._card_rows(d))

    def _card_text(self, d: dict[str, Any], preview: int) -> str:
        items = self._items(d)
        kinds = [i.kind for i in items]
        brief = d["brief"] or {}
        tone, lang = self.catalog.tone(d["tone"]), self.catalog.language(d["lang"])
        now = self.pipeline.clock()
        lines = [f"📝 Draft [{d['id']}] · {kinds.count('image')} photo(s) · {kinds.count('video')} video(s)"]
        if brief.get("summary"):
            lines.append(brief["summary"])
        lines.append(f"🎭 {tone.emoji} {tone.label}   🌐 {lang.label}")
        selected = [t for t in d["targets"] if t["selected"]]
        for t in selected:
            when = self.pipeline.fmt(self.pipeline.suggest_slot(t["platform"], t["kind"], t["when"]), now)
            lines.append(f"\n{PLATFORM_ICON.get(t['platform'], '•')} {t['label']} → {when} · {self._method_label(t['platform'])}")
            if t["kind"] == "story":
                lines.append(f"Text sticker idea: {t['on_screen_text'] or '(none)'}" + (f"\nHighlight: {t['highlight']}" if t["highlight"] else ""))
            else:
                cap = t["caption"] if len(t["caption"]) <= preview else t["caption"][:preview].rstrip() + "…"
                lines.append(cap or "(no caption yet)")
                if t["hashtags"]:
                    lines.append(" ".join(t["hashtags"]))
        if not selected:
            lines.append("\nNothing selected yet: tap where it should go below.")
        flags = brief.get("risk_flags") or []
        if flags:
            lines.append(f"\n⚠️ Flagged: {', '.join(flags)}")
        if brief.get("edit_brief"):
            lines.append(f"💡 Edit idea (nothing is edited in Phase 1): {brief['edit_brief']}")
        if flash := self._flash.pop(d["id"], ""):
            lines.append(f"ℹ️ {flash}")
        lines.append("\nTip: reply to this message to tell me what to change, or start with = to use your own caption.")
        return "\n".join(lines)

    def _card_rows(self, d: dict[str, Any]) -> list[list[Button]]:
        tones = [Button(f"{t.emoji} {t.label}", f"tone:{t.key}", t.key == d["tone"]) for t in self.catalog.tones]
        rows = [tones[i:i + 3] for i in range(0, len(tones), 3)]
        rows.append([Button(x.label, f"lang:{x.key}", x.key == d["lang"]) for x in self.catalog.languages])
        rows.append([Button(("☑ " if t["selected"] else "☐ ") + t["short"], f"tgt:{t['id']}", t["selected"]) for t in d["targets"]])
        rows.append([Button("🔄 Rewrite", "rewrite"), Button("✏️ My caption", "mycap"), Button("💬 Change…", "change")])
        flagged = bool((d["brief"] or {}).get("risk_flags"))
        approve = (Button("⚠️ Yes, approve anyway", "approve!") if flagged and d["id"] in self._confirming
                   else Button("✅ Approve", "approve"))
        rows.append([approve, Button("⚡ Now", "now"), Button("🗑 Discard", "discard")])
        return rows

    def show(self, did: str) -> None:
        """Post the draft card again as a fresh message (e.g. /draft <id>)."""
        d = self.store.get_draft(did)
        if d is None:
            self.notifier.notify(f"I can't find draft [{did}].")
            return
        d["card_ref"] = None
        self._present(d, self.card(did) if d["state"] in (READY, DRAFT_FAILED) else Card("draft", did, f"Draft [{did}] is {d['state']}."))

    def list_open(self) -> None:
        drafts = self.store.list_drafts([READY, DRAFT_FAILED], limit=10)
        if not drafts:
            self.notifier.notify("No open drafts. Send me a photo or video!")
            return
        for d in drafts:
            summary = ((d["brief"] or {}).get("summary") or "(no summary)")[:90]
            self.notifier.notify(f"📝 [{d['id']}] {d['state']} - {summary}", actions=[("Open", f"d:{d['id']}:show")])

    # ------------------------------------------------------------------ button taps
    def handle_action(self, did: str, action: str) -> ActionResult:
        """Entry point for every tap on a draft card (runs in a worker thread)."""
        kind, _, arg = action.partition(":")
        if kind == "tone":
            self.set_tone(did, arg)
        elif kind == "lang":
            self.set_language(did, arg)
        elif kind == "tgt":
            self.toggle_target(did, arg)
        elif action == "rewrite":
            self.rewrite(did)
        elif action == "approve":
            self.approve(did)
        elif action == "approve!":
            self.approve(did, confirmed=True)
        elif action == "now":
            self.approve(did, now=True)
        elif action == "discard":
            self.discard(did)
        elif action == "retry":
            self.retry(did)
        elif action == "show":
            self.show(did)
        elif action == "mycap":
            return ActionResult(prompt="✏️ Send the caption you want me to use, exactly as written.")
        elif action == "change":
            return ActionResult(prompt="💬 What should I change? (e.g. “shorter, mention it was Sunday”, “less emoji”, “post at 8pm”)")
        return ActionResult()

    # ------------------------------------------------------------------ /settings
    def settings_card(self) -> Card:
        tone, lang = self.prefs.tone, self.prefs.language
        lines = ["⚙️ Settings", "Defaults for every new draft (you can still change each draft with its buttons).",
                 f"🎭 Tone: {self.catalog.tone(tone).label}   🌐 Language: {self.catalog.language(lang).label}"]
        rows = []
        tones = [Button(f"{t.emoji} {t.label}", f"tone:{t.key}", t.key == tone) for t in self.catalog.tones]
        rows += [tones[i:i + 3] for i in range(0, len(tones), 3)]
        rows.append([Button(x.label, f"lang:{x.key}", x.key == lang) for x in self.catalog.languages])
        for plat in PLATFORMS:
            m = self.prefs.method(plat)
            lines.append(f"{PLATFORM_ICON[plat]} {plat.title()}: {'auto-post via API' if m == 'api' else 'you post it from the app (I send a package)'}")
            rows.append([Button(f"{PLATFORM_ICON[plat]} {plat.title()}: " + ("API" if x == "api" else "Manual"), f"method:{plat}:{x}", m == x)
                         for x in METHODS])
        lines.append(f"✂️ Editing: {self.settings.edit_level} (Phase 1 = originals; formats fixed only where a platform requires it)")
        return Card("settings", "-", "\n".join(lines), rows)

    def settings_action(self, action: str) -> str:
        kind, _, arg = action.partition(":")
        try:
            if kind == "tone":
                self.prefs.set_tone(arg)
            elif kind == "lang":
                self.prefs.set_language(arg)
            elif kind == "method":
                plat, _, method = arg.partition(":")
                self.prefs.set_method(plat, method)
            else:
                return ""
        except ValueError as exc:
            return str(exc)
        return "Saved"
