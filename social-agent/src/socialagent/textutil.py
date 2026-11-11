"""Caption and hashtag helpers (pure functions, heavily unit-tested)."""

from __future__ import annotations

import re
from typing import Iterable

IG_CAPTION_MAX = 2200
TIKTOK_TITLE_MAX = 2200
TIKTOK_PHOTO_TITLE_MAX = 90
TIKTOK_PHOTO_DESC_MAX = 4000

_BAD = re.compile(r"[^\w]", re.UNICODE)


def normalize_hashtag(tag: str) -> str:
    """'#My Tag!' -> '#MyTag'. Returns '' if nothing usable is left."""
    body = _BAD.sub("", str(tag).replace("#", "").replace(" ", ""))
    return f"#{body}" if body else ""


def normalize_hashtags(tags: Iterable[str], *, banned: Iterable[str] = (), first: Iterable[str] = (),
                       limit: int = 12) -> list[str]:
    """Clean, de-duplicate (case-insensitive), drop banned, put `first` (brand tags) at the front, cap."""
    banned_l = {normalize_hashtag(b).lower() for b in banned}
    out: list[str] = []
    seen: set[str] = set()
    for raw in [*first, *tags]:
        tag = normalize_hashtag(raw)
        if tag and tag.lower() not in seen and tag.lower() not in banned_l:
            seen.add(tag.lower())
            out.append(tag)
    return out[:limit]


def compose_caption(platform: str, caption: str, hashtags: list[str], *, max_len: int) -> str:
    """Caption + hashtags, trimmed to max_len (drop hashtags first, then shorten the caption)."""
    caption = caption.strip()
    sep = "\n\n" if platform == "instagram" else " "
    tags = list(hashtags)
    while tags:
        text = caption + sep + " ".join(tags)
        if len(text) <= max_len:
            return text
        tags.pop()
    return truncate(caption, max_len)


def truncate(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max(max_len - 1, 0)].rstrip() + "…"
