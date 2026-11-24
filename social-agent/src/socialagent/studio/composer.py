"""Composer + calendar: turn library media into scheduled posts, and manage them on a calendar.

Posts are created only by an explicit owner action ("Schedule" in the studio), through the same `Pipeline.schedule_targets`
the Telegram drafts use, so slot choice, media preparation, the undo window, atomic publishing and crash recovery are the
tested ones. What posts is what was previewed: a look is rendered once per (asset, look) and that file is both the preview
and the master that gets published.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import enhance
from .. import media as media_mod
from ..brain import PlanError, eligible_targets
from ..config import Settings
from ..media import MediaError
from ..models import MediaItem
from ..pipeline import Pipeline
from ..prefs import Prefs
from ..store import Store
from ..timeutil import iso, parse_when
from .assets import AssetLibrary

POST_STATUSES = ("scheduled", "publishing", "handed_off", "published", "failed", "cancelled")


class ComposeError(ValueError):
    """Shown to the owner as-is."""


def target_key(platform: str, kind: str) -> str:
    return f"{platform}:{kind}"


class Composer:
    def __init__(self, *, settings: Settings, store: Store, assets: AssetLibrary, pipeline: Pipeline, prefs: Prefs):
        self.settings, self.store, self.assets, self.pipeline, self.prefs = settings, store, assets, pipeline, prefs
        self.renders = settings.data_dir / "studio" / "renders"
        self._render_lock = threading.Lock()

    # ---- helpers ------------------------------------------------------------------------------
    def _items(self, asset_ids: list[str]) -> list[tuple[str, MediaItem]]:
        ids = [str(a) for a in asset_ids][:35]
        out = []
        for aid in ids:
            a = self.assets.get(aid)
            if a is None:
                raise ComposeError("One of the selected items no longer exists.")
            out.append((aid, MediaItem(path=a["path"], kind=a["kind"], width=a["width"], height=a["height"],
                                       duration=a["duration"], size=a["size"])))
        if not out:
            raise ComposeError("Pick at least one photo or video.")
        return out

    def _look(self, look: str) -> str:
        if not enhance.is_look(look):
            raise ComposeError(f"Unknown look {look!r}.")
        return enhance.ORIGINAL if self.settings.edit_level == "none" else look

    def master(self, aid: str, look: str) -> MediaItem:
        """The exact file that will be published for this asset and look (rendered once, then cached)."""
        a = self.assets.get(aid)
        if a is None:
            raise ComposeError("That item no longer exists.")
        look = self._look(look)
        src = MediaItem(path=a["path"], kind=a["kind"], width=a["width"], height=a["height"], duration=a["duration"], size=a["size"])
        if look == enhance.ORIGINAL:
            return src
        dst = self.renders / aid / look / ("master.jpg" if a["kind"] == "image" else "master.mp4")
        with self._render_lock:  # ffmpeg is heavy: one render at a time keeps the shared server responsive
            if not dst.exists():
                probe = media_mod.probe(a["path"])
                if probe.hdr:
                    return src  # HDR is left as shot (enhancing it needs tone-mapping)
                tmp = dst.with_name("tmp-" + dst.name)
                enhance.enhance(a["path"], tmp, look)
                tmp.replace(dst)
        return media_mod.probe(dst)

    def preview(self, aid: str, look: str) -> Path:
        """A small JPEG of the master (a still frame for videos) for the composer."""
        m = self.master(aid, look)
        look = self._look(look)
        out = self.renders / aid / look / "preview.jpg"
        if not out.exists() or out.stat().st_mtime < Path(m.path).stat().st_mtime:
            out.parent.mkdir(parents=True, exist_ok=True)
            if m.kind == "video":
                frame = out.with_name("frame.jpg")
                media_mod.thumbnail(m.path, frame, at=min(0.5, max(m.duration / 2, 0.0)))
                media_mod.vision_jpeg(frame, out, max_edge=960)
                frame.unlink(missing_ok=True)
            else:
                media_mod.vision_jpeg(m.path, out, max_edge=960)
        return out

    # ---- composer -------------------------------------------------------------------------------
    def options(self, asset_ids: list[str]) -> dict[str, Any]:
        items = self._items(asset_ids)
        targets = []
        for e in eligible_targets([m for _, m in items]):
            when = self.pipeline.suggest_slot(e.platform, e.kind)
            targets.append({"key": target_key(e.platform, e.kind), "platform": e.platform, "kind": e.kind, "label": e.label,
                            "method": self.prefs.method(e.platform), "suggested": iso(when),
                            "caption_limit": 2200 if e.platform == "instagram" else 2200, "has_caption": e.kind != "story"})
        looks = [{"key": k, "label": lk.label, "emoji": lk.emoji} for k, lk in enhance.LOOKS.items()] + \
                [{"key": enhance.ORIGINAL, "label": "Original", "emoji": "⛔"}]
        return {"targets": targets, "looks": looks if self.settings.edit_level != "none" else looks[-1:],
                "default_look": self.prefs.look if self.settings.edit_level != "none" else enhance.ORIGINAL,
                "timezone": self.settings.timezone}

    def schedule(self, asset_ids: list[str], targets: list[dict[str, Any]], *, look: str = "natural") -> dict[str, Any]:
        items = self._items(asset_ids)
        look = self._look(look)
        eligible = {target_key(e.platform, e.kind): e for e in eligible_targets([m for _, m in items])}
        chosen: list[dict[str, Any]] = []
        for t in targets or []:
            e = eligible.get(str(t.get("key", "")))
            if e is None:
                raise ComposeError(f"{t.get('key')!r} can't be published from this selection.")
            caption = str(t.get("caption") or "").strip()
            hashtags = ["#" + str(h).strip().lstrip("#") for h in (t.get("hashtags") or []) if str(h).strip().lstrip("#")][:30]
            when = str(t.get("when") or "").strip()
            if when and when.lower() not in ("now", "next_slot") and parse_when(when, self.pipeline.zone) is None:
                raise ComposeError(f"I can't read the time {when!r}.")
            chosen.append({"platform": e.platform, "kind": e.kind, "media_indexes": list(e.media_indexes),
                           "caption": caption, "hashtags": hashtags, "on_screen_text": caption if e.kind == "story" else "",
                           "when": when, "highlight": str(t.get("highlight") or "")})
        if not chosen:
            raise ComposeError("Pick at least one place to post.")
        missing = [eligible[target_key(c["platform"], c["kind"])].label for c in chosen
                   if c["kind"] != "story" and not c["caption"] and not c["hashtags"]]
        if missing:
            raise ComposeError(f"Add a caption for: {', '.join(missing)} (or untick it). Stories are the only posts without one.")
        try:
            masters = [self.master(aid, look) for aid, _ in items]
        except (MediaError, ValueError) as exc:
            raise ComposeError(f"Couldn't render the {look} look: {exc}") from None
        sid = self.store.add_submission(source="studio", instruction="",
                                        media=[{**m.to_dict(), "asset_id": aid} for (aid, _), m in zip(items, masters)])
        try:
            created, notes = self.pipeline.schedule_targets(sid, masters, chosen, method_for=self.prefs.method)
        except (PlanError, MediaError) as exc:
            raise ComposeError(f"Couldn't schedule: {exc}") from None
        self.store.update_submission(sid, status="scheduled")
        return {"posts": [{**c, "when": iso(c["when"])} for c in created], "notes": notes}

    # ---- calendar -------------------------------------------------------------------------------
    def posts(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        if end <= start or end - start > timedelta(days=100):
            raise ComposeError("Ask for at most 100 days at a time.")
        first_asset: dict[str, str | None] = {}
        out = []
        for p in self.store.posts_between(start, end):
            sid = p["submission_id"]
            if sid not in first_asset:
                sub = self.store.get_submission(sid) or {}
                media = json.loads(sub.get("media_json") or "[]")
                first_asset[sid] = next((m.get("asset_id") for m in media if m.get("asset_id")), None)
            out.append(post_view(p, first_asset[sid]))
        return out

    def reschedule(self, pid: str, when: str) -> dict[str, Any]:
        dt = parse_when(when, self.pipeline.zone)
        if dt is None:
            raise ComposeError("Pick a date and time.")
        if dt < self.pipeline.clock() + timedelta(minutes=1):
            raise ComposeError("That time has already passed. Pick a later one.")
        if not self.store.reschedule_post(pid, dt):
            raise ComposeError("Only posts that haven't started publishing can be moved.")
        return post_view(self.store.get_post(pid))  # type: ignore[arg-type]

    def edit_caption(self, pid: str, caption: str) -> dict[str, Any]:
        caption = caption.strip()
        post = self.store.get_post(pid)
        if post is None:
            raise ComposeError("That post no longer exists.")
        if len(caption) > 2200:
            raise ComposeError("Captions can be at most 2,200 characters.")
        if not self.store.edit_scheduled_caption(pid, caption):
            raise ComposeError("Only posts that haven't started publishing can be edited.")
        return post_view(self.store.get_post(pid))  # type: ignore[arg-type]

    def media_file(self, pid: str, index: int) -> Path | None:
        """A prepared file of a post (for manual hand-off downloads); only paths the post itself recorded."""
        post = self.store.get_post(pid)
        if post is None or not 0 <= index < len(post["media"]):
            return None
        p = Path(post["media"][index]["path"])
        return p if p.is_file() else None


def post_view(p: dict[str, Any], asset_id: str | None = None) -> dict[str, Any]:
    return {"id": p["id"], "platform": p["platform"], "kind": p["kind"], "status": p["status"], "method": p.get("method", "api"),
            "publish_at": p["publish_at"], "caption": p["caption"], "error": p.get("error") or "", "note": p.get("note") or "",
            "permalink": p.get("permalink") or "", "files": len(p["media"]),
            "thumb_url": f"/studio/api/assets/{asset_id}/thumb" if asset_id else ""}
