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
    Tone("sassy", "Sassy", "💅",
         "Confident, teasing and a little dramatic; playful attitude, never mean and never about a real person's flaws."),
    Tone("wholesome", "Wholesome", "🥰",
         "Warm, grateful and gentle; heartfelt without being cheesy."),
    Tone("luxury", "Luxe", "💎",
         "Refined, understated and premium: calm confidence, precise words, no slang, no hard sell."),
    Tone("nostalgic", "Nostalgic", "🎞️",
         "Wistful and film-like: a short reflection on the moment as a memory."),
    Tone("genz", "Gen Z", "😜",
         "Casual Gen-Z internet voice: lowercase, light slang, self-aware and a bit chaotic. Do not force memes or overdo it."),
    Tone("inspirational", "Inspire", "🌟",
         "Uplifting and motivating with one genuine takeaway; avoid clichés and toxic positivity."),
    Tone("mysterious", "Teaser", "🕶️",
         "Short and intriguing: hint at a story or a reveal without giving it all away; make people curious."),
    Tone("professional", "Pro", "💼",
         "Clear, credible and polished: first-person business-casual, benefits stated plainly, no hype."),
    Tone("deadpan", "Deadpan", "🙃",
         "Dry understatement and straight-faced humour: the joke is that you do not act like it is a joke."),
    Tone("foodie", "Foodie", "🍜",
         "Sensory and appetising: texture, aroma, the first-bite reaction. Only describe what is visible or stated."),
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
    "sassy": ("sassy", "cheeky", "teasing", "毒舌", "俏皮"),
    "wholesome": ("wholesome", "heartwarming", "heartfelt", "温馨", "治愈"),
    "luxury": ("luxe", "luxury tone", "luxurious tone", "高级感", "轻奢"),
    "nostalgic": ("nostalgic", "nostalgia", "throwback", "怀旧", "回忆杀"),
    "genz": ("gen z", "genz", "gen-z", "slangy"),
    "inspirational": ("inspirational", "motivational", "uplifting", "励志", "正能量"),
    "mysterious": ("mysterious", "teaser", "cryptic", "神秘"),
    "professional": ("professional tone", "professional voice", "corporate", "businesslike", "专业风"),
    "deadpan": ("deadpan", "dry humour", "dry humor", "冷幽默"),
    "foodie": ("appetizing", "appetising", "mouthwatering", "foodie tone", "诱人", "馋人"),
}
# Photo/video look (see enhance.py). Specific phrases only: "bright" or "natural" alone appear in ordinary descriptions.
_LOOK_HINTS: dict[str, tuple[str, ...]] = {
    "original": ("no edit", "unedited", "original look", "straight from the camera", "不修图", "原图"),
    "vivid": ("vivid", "vibrant", "punchy", "鲜艳"),
    "bright": ("bright and airy", "light and airy", "airy", "明亮", "清透"),
    "moody": ("moody", "cinematic", "氛围感", "电影感"),
    "natural": ("natural look", "natural edit", "自然风"),
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
    look: str | None = None


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

    def tone_pages(self, per_page: int = 5) -> list[list[Tone]]:
        """Tones split into pages for the keyboard (a '➕ More' button cycles through them)."""
        return [self.tones[i:i + per_page] for i in range(0, len(self.tones), per_page)]

    def has_tone(self, key: str) -> bool:
        return any(t.key == key for t in self.tones)

    def has_language(self, key: str) -> bool:
        return any(x.key == key for x in self.languages)

    def parse_hints(self, text: str) -> StyleHints:
        """Pick a tone / language / look out of free text like "funny, in Chinese, moody" (None where nothing is said).

        For tones the most specific (longest) matching phrase wins, so "dry humour" selects Deadpan, not Funny.
        """
        low = (text or "").lower()
        best: tuple[int, str] | None = None
        for key, words in _TONE_HINTS.items():
            if not self.has_tone(key):
                continue
            hit = max((len(w) for w in words if _has(low, w)), default=0)
            if hit and (best is None or hit > best[0]):
                best = (hit, key)
        lang = next((k for k, words in _LANG_HINTS if self.has_language(k) and any(_has(low, w) for w in words)), None)
        look = next((k for k, words in _LOOK_HINTS.items() if any(_has(low, w) for w in words)), None)
        return StyleHints(best[1] if best else None, lang, look)
