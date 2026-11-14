"""Channel-neutral message cards.

The draft service builds a ``Card`` (plain text + rows of buttons); each channel decides how to draw it
(Telegram: one message edited in place with an inline keyboard; CLI: printed text). No channel logic lives
in the core, which is what lets WhatsApp or a Mini App be added later without touching it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Button:
    label: str
    action: str  # e.g. "tone:funny", "approve" - interpreted by the owner of the card
    selected: bool = False


@dataclass(frozen=True)
class Preview:
    """A small image/video the channel shows next to the card (the exact media that would be posted, downsized)."""

    path: str
    kind: str  # "image" | "video"

    @property
    def key(self) -> str:
        """Changes whenever the file changes, so a channel knows whether to re-send or edit its copy."""
        try:
            st = os.stat(self.path)
            return f"{self.path}|{st.st_mtime_ns}|{st.st_size}"
        except OSError:
            return self.path


@dataclass
class Card:
    kind: str  # "draft" | "settings" | "post"
    ref: str  # draft id, "-" for settings, post id
    text: str
    rows: list[list[Button]] = field(default_factory=list)
    previews: list[Preview] = field(default_factory=list)
    preview_caption: str = ""

    def flat_text(self) -> str:
        """Text plus a compact button legend (CLI / tests)."""
        text = self.text
        if self.previews:
            text += "\nPreview files: " + ", ".join(p.path for p in self.previews)
        if not self.rows:
            return text
        legend = "\n".join("  " + " | ".join(("[x] " if b.selected else "") + b.label + f" -> {b.action}" for b in row)
                           for row in self.rows)
        return f"{text}\n{legend}"
