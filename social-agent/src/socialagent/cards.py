"""Channel-neutral message cards.

The draft service builds a ``Card`` (plain text + rows of buttons); each channel decides how to draw it
(Telegram: one message edited in place with an inline keyboard; CLI: printed text). No channel logic lives
in the core, which is what lets WhatsApp or a Mini App be added later without touching it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Button:
    label: str
    action: str  # e.g. "tone:funny", "approve" - interpreted by the owner of the card
    selected: bool = False


@dataclass
class Card:
    kind: str  # "draft" | "settings" | "post"
    ref: str  # draft id, "-" for settings, post id
    text: str
    rows: list[list[Button]] = field(default_factory=list)

    def flat_text(self) -> str:
        """Text plus a compact button legend (CLI / tests)."""
        if not self.rows:
            return self.text
        legend = "\n".join("  " + " | ".join(("[x] " if b.selected else "") + b.label + f" -> {b.action}" for b in row)
                           for row in self.rows)
        return f"{self.text}\n{legend}"
