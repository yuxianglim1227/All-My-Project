"""How the core talks to the owner, independent of the channel (Telegram, CLI, tests)."""

from __future__ import annotations

import logging
from typing import Protocol

from .cards import Card

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def notify(self, text: str, *, actions: list[tuple[str, str]] | None = None) -> None:
        """A plain message, optionally with (label, callback-data) buttons."""

    def present(self, card: Card, *, handle: str | None = None) -> str:
        """Show a card. With a handle from an earlier call it replaces that message (edit in place). Returns the handle."""

    def send_files(self, paths: list[str], caption: str = "", *, as_photo: bool = False) -> None:
        """Send media files back to the owner (best effort). as_photo = show inline instead of as downloadable documents."""


class ListNotifier:
    """Collects everything (tests, CLI)."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, list[tuple[str, str]]]] = []
        self.cards: list[tuple[Card, str]] = []  # every card shown, with the handle it ended up under
        self.latest: dict[str, Card] = {}  # handle -> the card currently on screen
        self.files: list[tuple[list[str], str, bool]] = []

    def notify(self, text: str, *, actions: list[tuple[str, str]] | None = None) -> None:
        self.messages.append((text, actions or []))
        log.info("NOTIFY: %s", text.replace("\n", " | ")[:200])

    def present(self, card: Card, *, handle: str | None = None) -> str:
        handle = handle or f"h{len(self.latest) + 1}"
        self.cards.append((card, handle))
        self.latest[handle] = card
        return handle

    def send_files(self, paths: list[str], caption: str = "", *, as_photo: bool = False) -> None:
        self.files.append((list(paths), caption, as_photo))

    @property
    def texts(self) -> list[str]:
        return [t for t, _ in self.messages]

    @property
    def last_card(self) -> Card:
        return self.cards[-1][0]
