"""Publisher that posts nothing. Used when DRY_RUN=true or credentials are missing."""

from __future__ import annotations

import logging

from .base import PublishRequest, PublishResult

log = logging.getLogger(__name__)


class DryRunPublisher:
    def __init__(self, platform: str, reason: str = "DRY_RUN is on"):
        self.platform = platform
        self.reason = reason
        self.sent: list[PublishRequest] = []

    def needs_urls(self, kind: str) -> bool:
        return False

    def publish(self, req: PublishRequest) -> PublishResult:
        self.sent.append(req)
        log.info("[DRY RUN:%s] %s %s | %d media | %r", self.platform, req.kind, req.post_id, len(req.media),
                 req.caption[:80])
        return PublishResult(remote_id=f"dryrun-{req.post_id}", permalink="",
                             note=f"DRY RUN ({self.reason}) - nothing was posted")

    def selfcheck(self) -> list[str]:
        return [f"{self.platform}: dry-run mode ({self.reason}) - no real posting will happen"]
