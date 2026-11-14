import pytest

from socialagent.styles import DEFAULT_LANGUAGES, DEFAULT_TONES, StyleCatalog

CAT = StyleCatalog.from_brand()


@pytest.mark.parametrize("text,tone,lang", [
    ("funny", "funny", None),
    ("make it FUNNY and in english", "funny", "en"),
    ("搞笑 中文", "funny", "zh"),
    ("just describe it", "descriptive", None),
    ("depict what you see, in Chinese", "descriptive", "zh"),
    ("aesthetic vibes", "aesthetic", None),
    ("文艺一点，繁體", "aesthetic", "zh-hant"),
    ("traditional chinese please", None, "zh-hant"),          # 'traditional' wins over plain 'chinese'
    ("bilingual", None, "bi"),
    ("English and Chinese", None, "bi"),
    ("中英双语", None, "bi"),
    ("storytelling", "storytelling", None),
    ("干货", "informative", None),
    ("hype it up", "hype", None),
    ("", None, None),
    ("post this tonight", None, None),
])
def test_parse_hints(text, tone, lang):
    h = CAT.parse_hints(text)
    assert (h.tone, h.language) == (tone, lang)


@pytest.mark.parametrize("text", [
    "story only",                 # an Instagram *Story* target, not the storytelling tone
    "tiktok only",
    "explain the recipe",         # 'plain' inside 'explain'
    "hyperlink in bio",           # 'hype' inside 'hyperlink'
    "englishman at the bar",
    "tips for next time",         # too generic to switch tone
])
def test_parse_hints_has_no_false_positives(text):
    h = CAT.parse_hints(text)
    assert (h.tone, h.language) == (None, None)


def test_catalog_defaults_and_fallbacks():
    assert [t.key for t in CAT.tones] == [t.key for t in DEFAULT_TONES] and [x.key for x in CAT.languages] == [x.key for x in DEFAULT_LANGUAGES]
    assert CAT.tone("nope").key == "descriptive" and CAT.language("nope").key == "en"
    assert CAT.has_tone("funny") and not CAT.has_tone("nope") and CAT.has_language("zh-hant") and not CAT.has_language("fr")
    assert all(t.guidance and t.emoji and t.label for t in CAT.tones) and all(x.instruction for x in CAT.languages)


def test_custom_tones_add_and_override_and_skip_invalid():
    cat = StyleCatalog.from_brand([
        {"key": "Sassy", "label": "Sassy", "emoji": "💅", "guidance": "Confident and teasing."},
        {"key": "funny", "guidance": "Dry deadpan humour only."},            # overrides the built-in
        {"key": "broken"},                                                      # no guidance: ignored
        {"guidance": "no key"},
    ])
    assert cat.has_tone("sassy") and not cat.has_tone("broken") and len(cat.tones) == len(DEFAULT_TONES) + 1
    assert cat.tone("funny").guidance == "Dry deadpan humour only." and cat.tone("funny").label == "Funny"
    assert cat.tone("sassy").emoji == "💅"
    assert cat.parse_hints("funny").tone == "funny"
    with pytest.raises(ValueError):
        StyleCatalog([], DEFAULT_LANGUAGES)
