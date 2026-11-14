"""Publisher interface shared by Instagram, TikTok and the dry-run publisher."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class PreparedMedia:
    path: str
    kind: str  # "image" | "video"
    duration: float = 0.0
    url: str = ""  # public URL, filled in by the pipeline when the publisher needs one
    edited: bool = False  # an enhanced master (not the owner's original): manual hand-off must send it back


@dataclass
class PublishRequest:
    post_id: str
    platform: str
    kind: str  # photo | carousel | reel | story | video
    caption: str  # final text, hashtags already merged and length-limited
    media: list[PreparedMedia] = field(default_factory=list)


@dataclass
class PublishResult:
    remote_id: str
    permalink: str = ""
    note: str = ""
    handed_off: bool = False  # manual method: nothing was published, the owner will post it


class PublishError(RuntimeError):
    """A publish attempt failed. ``retryable`` is only True when retrying cannot create a duplicate post."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class Publisher(Protocol):
    platform: str

    def needs_urls(self, kind: str) -> bool:
        """True if media must be reachable at a public URL for this kind of post."""

    def publish(self, req: PublishRequest) -> PublishResult: ...

    def selfcheck(self) -> list[str]:
        """Read-only credential/permission checks; returns human-readable lines."""
