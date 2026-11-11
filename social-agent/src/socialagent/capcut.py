"""EXPERIMENTAL: hand an edit over to CapCut / Jianying as a draft project.

CapCut and Jianying have no public API, so the agent cannot drive the app. What it *can* do, when it runs
on the same computer as the desktop app, is write a draft (clip + hook text) into the app's drafts
folder using the open-source ``pyJianYingDraft`` library; you open the draft in the app to polish it
(trending audio, effects, templates) and export.

Verified here: the library creates ``draft_content.json`` / ``draft_meta_info.json`` with the calls below.
NOT verified: that a given CapCut/Jianying release opens drafts written this way (newer releases changed
their draft format/encryption). Treat as a convenience, not a dependency - normal posting uses ffmpeg.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import MediaItem, PlannedPost


class CapCutUnavailable(RuntimeError):
    pass


def make_draft(drafts_dir: str | Path, name: str, clips: list[MediaItem], overlay_text: str = "",
               width: int = 1080, height: int = 1920) -> str:
    try:
        import pyJianYingDraft as d  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CapCutUnavailable("Install the optional extra: pip install 'socialagent[capcut]'") from exc
    folder = Path(drafts_dir)
    if not folder.is_dir():
        raise CapCutUnavailable(f"JIANYING_DRAFTS_DIR does not exist on this machine: {folder}")
    videos = [c for c in clips if c.kind == "video"]
    if not videos:
        raise CapCutUnavailable("No video clips to put in the draft.")
    safe = re.sub(r"[^\w.-]+", "_", name)[:60]
    script = d.DraftFolder(str(folder)).create_draft(safe, width, height, allow_replace=True)
    track = script.append_track(d.TrackSpec(d.TrackType.video))
    cursor = 0.0
    for clip in videos:
        script.add_segment(d.VideoSegment(clip.path, d.trange(cursor, clip.duration)), track)
        cursor += clip.duration
    if overlay_text.strip():
        text_track = script.append_track(d.TrackSpec(d.TrackType.text))
        script.add_segment(
            d.TextSegment(overlay_text.strip(), d.trange(0, min(cursor, 4.0)),
                          style=d.TextStyle(size=8.0, bold=True, color=(1.0, 1.0, 1.0), align=1, auto_wrapping=True),
                          clip_settings=d.ClipSettings(transform_y=0.65)), text_track)
    script.save()
    return safe


def draft_maker_for(drafts_dir: str):
    """Adapter with the signature Pipeline expects."""

    def maker(sid: str, n: int, pp: PlannedPost, clips: list[MediaItem]) -> str | None:
        name = make_draft(drafts_dir, f"socialagent_{sid}_{n}", clips, pp.on_screen_text)
        return f"Experimental: CapCut/Jianying draft '{name}' written to your drafts folder - open it in the app to polish."

    return maker
