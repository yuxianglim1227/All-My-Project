"""Caption style: tone x language, with a catalog the owner can extend in brand.yaml.

The owner picks a style per draft with one tap (or types a hint such as "funny 中文" in the caption), and
the defaults live in ``/settings``. ``parse_hints`` is deliberately deterministic (no LLM call) so that
reading a hint is instant and free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class Tone:
    key: str
    label: str
    emoji: str
    guidance: str


@dataclass(frozen=True)
class Language:
    key: str
    label: str  # shown on buttons / in cards
    instruction: str  # given to the writer


@dataclass(frozen=True)
class Style:
    tone: str
    language: str


DEFAULT_TONES: tuple[Tone, ...] = (
    Tone("descriptive", "Describe", "📝",
         "Plain, warm and factual: say what is in the picture/video (and where/when if the owner said so). "
         "No jokes, no hype, no exaggeration."),
    Tone("funny", "Funny", "😂",
         "Witty and playful. Build ONE clear joke, pun or self-aware observation from what is actually visible. "
         "Light emoji. Never mock a real person."),
    Tone("aesthetic", "Aesthetic", "✨",
         "Minimal, poetic and mood-first. Short lines, soft imagery, at most one emoji."),
    Tone("storytelling", "Story", "📖",
         "A personal first-person mini-story with a beginning and a payoff (2-4 short paragraphs on Instagram, "
         "1-2 lines on TikTok). Only use facts the owner gave or that are visible."),
    Tone("informative", "Tips", "💡",
         "Value first: a hook, then 2-3 useful points or takeaways, ending with a save/share prompt."),
    Tone("hype", "Hype", "🔥",
         "High energy and bold with a strong call to action. Still truthful: no invented claims or prices."),
)

DEFAULT_LANGUAGES: tuple[Language, ...] = (
    Language("en", "EN", "Write in natural, idiomatic English."),
    Language("zh", "简体", "用自然、地道的简体中文写作（不要翻译腔），可用网络用语但不要过度。"),
    Language("zh-hant", "繁體", "用自然、地道的繁體中文寫作（不要翻譯腔）。"),
    Language("bi", "EN+中", "Write the caption in English first, then the same message in natural Simplified Chinese on "
                          "new lines below (adapt idioms, do not translate word for word). Mix hashtags in both languages."),
)

# Keyword -> key. Latin words are matched on word boundaries; Chinese keywords as substrings.
_TONE_HINTS: dict[str, tuple[str, ...]] = {
    "descriptive": ("descriptive", "describe", "depict", "factual", "plain", "straightforward", "描述", "直白", "平实"),
    "funny": ("funny", "humor", "humour", "humorous", "witty", "joke", "jokes", "搞笑", "幽默", "好玩", "有趣"),
    "aesthetic": ("aesthetic", "minimal", "minimalist", "poetic", "vibey", "文艺", "唯美", "氛围"),
    "storytelling": ("storytelling", "story-telling", "personal story", "故事感", "讲故事"),
    "informative": ("informative", "educational", "how-to", "howto", "干货", "科普", "教程"),
    "hype": ("hype", "hyped", "energetic", "excited", "种草", "安利"),
}
# Order matters: "bilingual" / "traditional" must win over plain "chinese" / "english".
_LANG_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bi", ("bilingual", "both languages", "english and chinese", "chinese and english", "en+zh", "en + zh", "中英", "双语", "雙語")),
    ("zh-hant", ("traditional chinese", "traditional", "繁體", "繁体", "繁中")),
    ("zh", ("chinese", "mandarin", "中文", "简体", "汉语", "华语")),
    ("en", ("english", "英文", "英语")),
)


def _has(text_lower: str, word: str) -> bool:
    if re.fullmatch(r"[a-z0-9+\- ]+", word):
        return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text_lower) is not None
    return word in text_lower


@dataclass(frozen=True)
class StyleHints:
    tone: str | None = None
    language: str | None = None


class StyleCatalog:
    def __init__(self, tones: Iterable[Tone] = DEFAULT_TONES, languages: Iterable[Language] = DEFAULT_LANGUAGES):
        self.tones = list(tones)
        self.languages = list(languages)
        if not self.tones or not self.languages:
            raise ValueError("A style catalog needs at least one tone and one language")

    @classmethod
    def from_brand(cls, custom_tones: Iterable[Mapping[str, Any]] = ()) -> "StyleCatalog":
        """Built-in tones plus/overriding the ones in brand.yaml (``tones:`` list)."""
        tones = {t.key: t for t in DEFAULT_TONES}
        for raw in custom_tones:
            key = str(raw.get("key") or "").strip().lower()
            if not key or not raw.get("guidance"):
                continue
            tones[key] = Tone(key, str(raw.get("label") or key.title()), str(raw.get("emoji") or "🎭"), str(raw["guidance"]))
        return cls(tones.values(), DEFAULT_LANGUAGES)

    def tone(self, key: str) -> Tone:
        return next((t for t in self.tones if t.key == key), self.tones[0])

    def language(self, key: str) -> Language:
        return next((x for x in self.languages if x.key == key), self.languages[0])

    def has_tone(self, key: str) -> bool:
        return any(t.key == key for t in self.tones)

    def has_language(self, key: str) -> bool:
        return any(x.key == key for x in self.languages)

    def parse_hints(self, text: str) -> StyleHints:
        """Pick a tone/language out of free text like "funny, in Chinese" (None where nothing is said)."""
        low = (text or "").lower()
        tone = next((k for k, words in _TONE_HINTS.items() if self.has_tone(k) and any(_has(low, w) for w in words)), None)
        lang = next((k for k, words in _LANG_HINTS if self.has_language(k) and any(_has(low, w) for w in words)), None)
        return StyleHints(tone, lang)
