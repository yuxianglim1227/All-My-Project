"""Plain data types shared across the agent, plus the JSON schema Claude must answer with."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PLATFORMS = ("instagram", "tiktok")
# What each platform can publish through its official API.
KINDS: dict[str, set[str]] = {
    "instagram": {"photo", "carousel", "reel", "story"},
    "tiktok": {"video", "photo"},
}

# Post lifecycle
SCHEDULED = "scheduled"
PUBLISHING = "publishing"
PUBLISHED = "published"
HANDED_OFF = "handed_off"  # manual method: caption package sent, waiting for the owner to post it
FAILED = "failed"
CANCELLED = "cancelled"

# Draft lifecycle
DRAFTING = "drafting"
READY = "ready"
APPROVED = "approved"
DISCARDED = "discarded"
DRAFT_FAILED = "failed"


@dataclass
class MediaItem:
    path: str
    kind: str  # "image" | "video"
    width: int = 0
    height: int = 0
    duration: float = 0.0
    has_audio: bool = False
    vcodec: str = ""
    acodec: str = ""
    container: str = ""
    size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MediaItem":
        return cls(
            path=str(d["path"]), kind=str(d["kind"]), width=int(d.get("width", 0)),
            height=int(d.get("height", 0)), duration=float(d.get("duration", 0.0)),
            has_audio=bool(d.get("has_audio", False)), vcodec=str(d.get("vcodec", "")),
            acodec=str(d.get("acodec", "")), container=str(d.get("container", "")), size=int(d.get("size", 0)),
        )


@dataclass
class PlannedPost:
    platform: str
    kind: str
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    on_screen_text: str = ""
    when: str = "next_slot"  # "now" | "next_slot" | local ISO datetime
    highlight: str = ""
    media_indexes: list[int] = field(default_factory=list)
    trim_start: float = 0.0
    trim_end: float = 0.0  # seconds from start; 0 = to the end
    why: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlannedPost":
        return cls(
            platform=str(d.get("platform", "")).lower().strip(),
            kind=str(d.get("kind", "")).lower().strip(),
            caption=str(d.get("caption", "")),
            hashtags=[str(h) for h in d.get("hashtags") or []],
            on_screen_text=str(d.get("on_screen_text", "")),
            when=str(d.get("when") or "next_slot"),
            highlight=str(d.get("highlight", "")),
            media_indexes=[int(i) for i in d.get("media_indexes") or []],
            trim_start=float(d.get("trim_start") or 0.0),
            trim_end=float(d.get("trim_end") or 0.0),
            why=str(d.get("why", "")),
        )


@dataclass
class ContentPlan:
    pillar: str = ""
    summary: str = ""
    posts: list[PlannedPost] = field(default_factory=list)
    edit_brief: str = ""
    trend_used: str = ""
    risk_flags: list[str] = field(default_factory=list)
    message_to_owner: str = ""
    mood: str = ""
    facts: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ContentPlan":
        return cls(
            pillar=str(d.get("pillar", "")),
            summary=str(d.get("summary", "")),
            posts=[PlannedPost.from_dict(p) for p in d.get("posts") or []],
            edit_brief=str(d.get("edit_brief", "")),
            trend_used=str(d.get("trend_used", "")),
            risk_flags=[str(r) for r in d.get("risk_flags") or []],
            message_to_owner=str(d.get("message_to_owner", "")),
            mood=str(d.get("mood", "")), facts=[str(f) for f in d.get("facts") or []],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _str_array() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


# Conservative JSON Schema (no $ref / min / max) so structured outputs accept it.
# Every property is required; "absent" is expressed as "" / 0 / [].
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pillar": {"type": "string"},
        "summary": {"type": "string"},
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "enum": list(PLATFORMS)},
                    "kind": {"type": "string", "enum": ["photo", "carousel", "reel", "story", "video"]},
                    "caption": {"type": "string"},
                    "hashtags": _str_array(),
                    "on_screen_text": {"type": "string"},
                    "when": {"type": "string"},
                    "highlight": {"type": "string"},
                    "media_indexes": {"type": "array", "items": {"type": "integer"}},
                    "trim_start": {"type": "number"},
                    "trim_end": {"type": "number"},
                    "why": {"type": "string"},
                },
                "required": [
                    "platform", "kind", "caption", "hashtags", "on_screen_text", "when",
                    "highlight", "media_indexes", "trim_start", "trim_end", "why",
                ],
                "additionalProperties": False,
            },
        },
        "edit_brief": {"type": "string"},
        "trend_used": {"type": "string"},
        "risk_flags": _str_array(),
        "message_to_owner": {"type": "string"},
    },
    "required": [
        "pillar", "summary", "posts", "edit_brief", "trend_used", "risk_flags", "message_to_owner",
    ],
    "additionalProperties": False,
}

TRENDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "trends": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "enum": ["tiktok", "instagram", "both"]},
                    "name": {"type": "string"},
                    "format_description": {"type": "string"},
                    "why_it_works": {"type": "string"},
                    "how_to_adapt": {"type": "string"},
                    "hook_templates": _str_array(),
                    "audio_note": {"type": "string"},
                    "sources": _str_array(),
                    "stage": {"type": "string", "enum": ["rising", "peak", "fading", "evergreen"]},
                },
                "required": [
                    "platform", "name", "format_description", "why_it_works", "how_to_adapt",
                    "hook_templates", "audio_note", "sources", "stage",
                ],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["trends", "summary"],
    "additionalProperties": False,
}

INSPO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "format_name": {"type": "string"},
        "hook": {"type": "string"},
        "structure": {"type": "string"},
        "visual_style": {"type": "string"},
        "text_overlay_style": {"type": "string"},
        "pacing": {"type": "string"},
        "translation": {"type": "string"},
        "how_to_recreate": {"type": "string"},
    },
    "required": [
        "format_name", "hook", "structure", "visual_style", "text_overlay_style",
        "pacing", "translation", "how_to_recreate",
    ],
    "additionalProperties": False,
}


# What Claude returns after LOOKING at the media (no captions yet): used once per submission.
UNDERSTAND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pillar": {"type": "string"},
        "summary": {"type": "string"},
        "mood": {"type": "string"},
        "facts": _str_array(),
        "trend_used": {"type": "string"},
        "risk_flags": _str_array(),
        "edit_ideas": {"type": "string"},
        "targets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "enum": list(PLATFORMS)},
                    "kind": {"type": "string", "enum": ["photo", "carousel", "reel", "story", "video"]},
                    "media_indexes": {"type": "array", "items": {"type": "integer"}},
                    "when": {"type": "string"},
                    "highlight": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["platform", "kind", "media_indexes", "when", "highlight", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["pillar", "summary", "mood", "facts", "trend_used", "risk_flags", "edit_ideas", "targets"],
    "additionalProperties": False,
}

# What Claude returns when WRITING captions (text only, no images): used every time the style changes.
WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "captions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "caption": {"type": "string"},
                    "hashtags": _str_array(),
                    "on_screen_text": {"type": "string"},
                    "when": {"type": "string"},
                },
                "required": ["target", "caption", "hashtags", "on_screen_text", "when"],
                "additionalProperties": False,
            },
        },
        "note": {"type": "string"},
    },
    "required": ["captions", "note"],
    "additionalProperties": False,
}
