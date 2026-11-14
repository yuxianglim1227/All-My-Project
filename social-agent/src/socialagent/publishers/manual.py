"""Manual hand-off: at the scheduled time the bot sends a ready-to-post package and YOU tap Post in the app.

Why it exists: Instagram's API cannot create in-app drafts, and setting up the Meta/TikTok developer apps is the
slowest part of onboarding. With this method the agent already does the thinking (what to post, caption, hashtags,
timing) from day one; publishing is two taps in the native app. Nothing public can happen without you.
"""

from __future__ import annotations

import logging
import os

from ..notify import Notifier
from .base import PublishRequest, PublishResult

log = logging.getLogger(__name__)

STEPS = {
    ("instagram", "photo"): "Instagram -> + -> Post -> choose the photo you sent me -> paste the caption -> Share",
    ("instagram", "carousel"): "Instagram -> + -> Post -> select the photos/videos in order -> paste the caption -> Share",
    ("instagram", "reel"): "Instagram -> + -> Reel -> choose the video you sent me -> paste the caption -> Share",
    ("instagram", "story"): "Instagram -> + -> Story -> choose the media you sent me -> add the text sticker below -> Share to Story",
    ("tiktok", "video"): "TikTok -> + -> Upload -> choose the video you sent me -> paste the caption -> Post",
    ("tiktok", "photo"): "TikTok -> + -> Upload -> Photos -> select them in order -> paste the caption -> Post",
}


class ManualPublisher:
    platform = "manual"

    MAX_SEND_BYTES = 49 * 1024 * 1024  # the cloud Bot API cannot upload more than 50 MB

    def __init__(self, notifier: Notifier, host=None):
        self.notifier, self.host = notifier, host  # host: MediaHost for download links when a file is too big to send

    def needs_urls(self, kind: str) -> bool:
        return False

    def publish(self, req: PublishRequest) -> PublishResult:
        steps = STEPS.get((req.platform, req.kind), f"Open {req.platform.title()} and post it")
        is_story = req.kind == "story"
        edited = [m for m in req.media if m.edited]
        self.notifier.notify(f"⏰ Time to post your {req.platform.title()} {req.kind} [{req.post_id}]\n{steps}\n" + (
            "Use the edited media below (tap and hold -> Save). It is the exact version you previewed." if edited
            else "The media is the original you sent me; it is still in your gallery."))
        for m in edited:
            if os.path.getsize(m.path) <= self.MAX_SEND_BYTES:
                self.notifier.send_files([m.path], "Edited media - save it, then post")
            elif self.host is not None:
                self.notifier.notify(f"This file is too big to send here. Download (valid ~3 days):\n{self.host.publish(m.path)}")
            else:
                self.notifier.notify(f"This file is too big to send here; find it on the server: {m.path}")
        body = req.caption.strip() or "(no text for this one)"
        label = "Suggested text sticker (copy):" if is_story else "Caption (long-press to copy):"
        self.notifier.notify(f"{label}\n\n{body}", actions=[("✅ I posted it", f"posted:{req.post_id}")])
        return PublishResult(remote_id=f"manual-{req.post_id}", note="Handed off to you to post in the app.", handed_off=True)

    def selfcheck(self) -> list[str]:
        return ["manual: ready - you post from the native app using the package the bot sends (no API needed)"]
