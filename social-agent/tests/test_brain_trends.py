import json

import httpx
import pytest

from conftest import needs_ffmpeg
from fakes import FakeLLM
from socialagent import media
from socialagent.brain import PlanContext, PlanError, Planner, validate_plan
from socialagent.config import BrandProfile
from socialagent.llm import LLMError
from socialagent.models import PLAN_SCHEMA, ContentPlan, MediaItem, PlannedPost
from socialagent.store import Store
from socialagent.trends import TrendResearcher, harvest_links, platform_of

BRAND = BrandProfile.from_dict({
    "account_name": "Test", "niche": "food", "pillars": [{"name": "Daily life", "weight": 60}, {"name": "Tips", "weight": 40}],
    "hashtags": {"always": ["#mybrand"], "banned": ["#spam"]},
    "highlights": [{"name": "Food"}, {"name": "Travel"}],
})
IMG = lambda i=0: MediaItem(path=f"/x/{i}.jpg", kind="image", width=1000, height=1000)   # noqa: E731
VID = lambda i=0: MediaItem(path=f"/x/{i}.mp4", kind="video", width=1080, height=1920, duration=10, has_audio=True)  # noqa: E731


def post(platform, kind, **kw):
    return PlannedPost(platform=platform, kind=kind, caption=kw.pop("caption", "cap"), **kw)


def validated(posts, media_items, **plan_kw):
    return validate_plan(ContentPlan(pillar=plan_kw.pop("pillar", "Daily life"), posts=posts, **plan_kw), media_items, BRAND)


# ---------------------------------------------------------------- validate_plan
def test_unsupported_formats_are_dropped_with_a_reason():
    plan, notes = validated([post("instagram", "video"), post("tiktok", "reel"), post("instagram", "photo")], [IMG()])
    assert [(p.platform, p.kind) for p in plan.posts] == [("instagram", "photo")]
    assert sum("isn't available" in n for n in notes) == 2


def test_everything_invalid_raises_plan_error():
    with pytest.raises(PlanError):
        validated([post("instagram", "reel")], [IMG()])          # reel needs a video
    with pytest.raises(PlanError):
        validated([], [IMG()])


def test_media_assignment_rules():
    media_items = [IMG(0), IMG(1), VID(2)]
    plan, _ = validated([
        post("instagram", "photo", media_indexes=[0, 1]),                 # photo -> exactly one image
        post("instagram", "carousel", media_indexes=[0, 1, 2]),           # carousel may mix
        post("instagram", "reel", media_indexes=[0, 2]),                  # reel -> the video only
        post("instagram", "story", media_indexes=[1, 0]),                 # story -> first only
        post("tiktok", "video", media_indexes=[2]),
        post("tiktok", "photo", media_indexes=[0, 1, 2]),                 # tiktok photo -> images only
    ], media_items)
    got = {(p.platform, p.kind): p.media_indexes for p in plan.posts}
    assert got == {("instagram", "photo"): [0], ("instagram", "carousel"): [0, 1, 2], ("instagram", "reel"): [2],
                   ("instagram", "story"): [1], ("tiktok", "video"): [2], ("tiktok", "photo"): [0, 1]}


def test_empty_or_out_of_range_indexes_default_to_all_media():
    plan, _ = validated([post("instagram", "carousel", media_indexes=[99, -1])], [IMG(0), IMG(1)])
    assert plan.posts[0].media_indexes == [0, 1]


def test_single_item_carousel_is_downgraded():
    plan, notes = validated([post("instagram", "carousel")], [IMG()])
    assert plan.posts[0].kind == "photo" and any("carousel needs 2+" in n for n in notes)
    plan, _ = validated([post("instagram", "carousel")], [VID()])
    assert plan.posts[0].kind == "reel"


def test_carousel_capped_at_ten():
    plan, _ = validated([post("instagram", "carousel")], [IMG(i) for i in range(14)])
    assert plan.posts[0].media_indexes == list(range(10))


def test_duplicates_removed():
    plan, _ = validated([post("instagram", "photo", media_indexes=[0]), post("instagram", "photo", media_indexes=[0])], [IMG()])
    assert len(plan.posts) == 1


def test_hashtags_brand_first_banned_removed_and_capped():
    tags = [f"#t{i}" for i in range(30)] + ["#spam"]
    plan, _ = validated([post("instagram", "photo", hashtags=tags), post("tiktok", "photo", hashtags=tags)], [IMG()])
    ig, tt = plan.posts
    assert ig.hashtags[0] == "#mybrand" and len(ig.hashtags) == 12 and "#spam" not in ig.hashtags
    assert len(tt.hashtags) == 8


def test_story_rules_no_caption_no_hashtags_highlight_canonicalised():
    plan, notes = validated([post("instagram", "story", hashtags=["#a"], highlight="travel", on_screen_text="  hello   world ")], [IMG()])
    s = plan.posts[0]
    assert (s.caption, s.hashtags, s.highlight, s.on_screen_text) == ("", [], "Travel", "hello world")
    plan, notes = validated([post("instagram", "story", highlight="Nope")], [IMG()])
    assert plan.posts[0].highlight == "" and any("Unknown Highlight" in n for n in notes)
    plan, _ = validated([post("instagram", "photo", highlight="Food")], [IMG()])
    assert plan.posts[0].highlight == ""                    # only Stories go into Highlights


def test_on_screen_text_only_for_overlay_capable_kinds_and_length_capped():
    plan, _ = validated([post("instagram", "photo", on_screen_text="burn me"),
                         post("instagram", "reel", on_screen_text="x" * 200, media_indexes=[1])], [IMG(), VID(1)])
    photo, reel = plan.posts
    assert photo.on_screen_text == "" and len(reel.on_screen_text) == 80


def test_trim_values_sanitised_and_pillar_canonicalised():
    plan, _ = validated([post("tiktok", "video", trim_start=-3, trim_end=-1)], [VID()], pillar="daily LIFE")
    p = plan.posts[0]
    assert (p.trim_start, p.trim_end, plan.pillar) == (0.0, 0.0, "Daily life")
    plan, _ = validated([post("tiktok", "video", trim_start=5, trim_end=2)], [VID()])
    assert plan.posts[0].trim_end == 0.0                     # end before start -> "to the end"
    plan, notes = validated([post("tiktok", "video")], [VID()], pillar="Made up")
    assert plan.pillar == "" and any("Pillar" in n for n in notes)


def test_risk_flags_normalised():
    plan, _ = validated([post("tiktok", "video")], [VID()], risk_flags=[" Other People Visible ", ""])
    assert plan.risk_flags == ["other_people_visible"]


# ---------------------------------------------------------------- planner (real media + fake LLM)
@needs_ffmpeg
def test_planner_builds_multimodal_request_and_returns_validated_plan(media_dir, tmp_path):
    llm = FakeLLM()
    planner = Planner(llm, BRAND, tmp_path)
    items = [media.probe(media_dir / "photo.jpg"), media.probe(media_dir / "landscape.mp4")]
    ctx = PlanContext(now_local="2026-11-11 18:00 (Wednesday)", feed_summary="- IG reel: x", trends="- trend A",
                      inspiration="- inspo B")
    plan, notes = planner.plan("story only please", items, ctx)

    assert plan.posts[0].kind == "photo" and plan.posts[0].hashtags[0] == "#mybrand"
    call = llm.calls[0]
    assert call["schema"] is PLAN_SCHEMA
    assert "Daily life" in call["system"] and "UNTRUSTED" in call["system"]
    blocks = call["content"]
    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == 1 + 4                               # 1 photo + 4 sampled video frames
    assert all(b["source"]["media_type"] == "image/jpeg" and b["source"]["data"] for b in images)
    text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
    assert "story only please" in text and "Media #1: video 1280x720" in text
    assert "<trends>\n- trend A\n</trends>" in text and "<inspiration>\n- inspo B\n</inspiration>" in text
    assert "2026-11-11 18:00" in text


@needs_ffmpeg
def test_planner_respects_image_budget(media_dir, tmp_path):
    planner = Planner(FakeLLM(), BRAND, tmp_path, max_images=3)
    items = [media.probe(media_dir / "photo.jpg")] * 2 + [media.probe(media_dir / "landscape.mp4")] * 2
    blocks = planner.build_content("", items, PlanContext(now_local="t"))
    assert sum(b["type"] == "image" for b in blocks) == 3
    assert any("omitted" in b.get("text", "") for b in blocks)
    assert "(none - use your judgement)" in blocks[0]["text"]


# ---------------------------------------------------------------- trends
def make_researcher(llm=None, handler=None):
    store = Store(":memory:")
    http = httpx.Client(transport=httpx.MockTransport(handler or (lambda r: httpx.Response(404))))
    return TrendResearcher(llm or FakeLLM(), store, BRAND, http=http), store


def test_refresh_stores_trends_and_marks_fresh():
    llm = FakeLLM()
    tr, store = make_researcher(llm)
    items = tr.refresh()
    assert [t["name"] for t in items] == ["Photo dump"] and store.kv_get("trends_live") == "1"
    kinds = [c["kind"] for c in llm.calls]
    assert kinds == ["research", "json"]
    assert "Douyin" in llm.calls[0]["content"] and "https://example.com/a" in llm.calls[1]["content"]
    assert "[tiktok/rising] Photo dump" in tr.trends_for_prompt()
    from socialagent.timeutil import utcnow
    assert tr.is_stale(utcnow()) is False


def test_refresh_degrades_to_evergreen_when_web_search_unavailable():
    llm = FakeLLM()
    llm.research_error = LLMError("web search not enabled")
    tr, store = make_researcher(llm)
    items = tr.refresh()
    assert store.kv_get("trends_live") == "0"
    assert items[0]["stage"] == "evergreen" and items[0]["sources"] == []     # never claims fake "live" sources
    assert [c["kind"] for c in llm.calls] == ["research", "text", "json"]
    assert "NOT available" in llm.calls[1]["content"]


def test_is_stale_without_prior_refresh():
    tr, _ = make_researcher()
    from socialagent.timeutil import utcnow
    assert tr.is_stale(utcnow()) is True


def test_platform_and_link_harvesting():
    assert platform_of("https://www.tiktok.com/@a/video/1") == "tiktok"
    assert platform_of("https://vm.tiktok.com/abc/") == "tiktok"
    assert platform_of("https://v.douyin.com/abc/") == "douyin"
    assert platform_of("https://evil-tiktok.com.attacker.io/x") == "other"
    text = json.dumps({"Likes": [{"Link": "https://www.tiktok.com/@a/video/1"}, {"Link": "https://v.douyin.com/x/"},
                                 {"Link": "https://example.com/no"}], "again": "https://www.tiktok.com/@a/video/1,"})
    assert harvest_links(text) == ["https://www.tiktok.com/@a/video/1", "https://v.douyin.com/x/"]


def test_add_link_tiktok_uses_oembed_and_analyses_caption():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"title": "Easy noodles", "author_name": "chef", "thumbnail_url": "https://t/x.jpg"})

    llm = FakeLLM()
    tr, store = make_researcher(llm, handler)
    res = tr.add_link("https://www.tiktok.com/@chef/video/123", note="love the pacing")
    assert seen["url"].startswith("https://www.tiktok.com/oembed?url=https%3A%2F%2Fwww.tiktok.com")
    assert res["status"] == "saved" and res["title"] == "Easy noodles" and res["analysis"]["format_name"]
    assert "Easy noodles" in llm.calls[0]["content"]
    assert "Easy noodles" in tr.inspiration_for_prompt() and "Split-screen reaction" in tr.inspiration_for_prompt()
    assert tr.add_link("https://www.tiktok.com/@chef/video/123")["status"] == "duplicate"


def test_add_link_douyin_has_no_metadata_and_asks_for_screenshot():
    llm = FakeLLM()
    tr, store = make_researcher(llm)
    res = tr.add_link("https://v.douyin.com/abc/")
    assert res["platform"] == "douyin" and "screenshot" in res["hint"] and llm.calls == []   # nothing to analyse yet


def test_add_link_survives_oembed_failure():
    tr, _ = make_researcher(handler=lambda r: httpx.Response(500))
    res = tr.add_link("https://www.tiktok.com/@a/video/9")
    assert res["status"] == "saved" and "saved the link only" in res["hint"]


def test_import_export_saves_up_to_limit_without_llm_calls():
    llm = FakeLLM()
    tr, store = make_researcher(llm)
    return_links = [f"https://www.tiktok.com/@a/video/{i}" for i in range(5)] + ["https://www.tiktok.com/@a/video/0"]
    import tempfile, pathlib
    p = pathlib.Path(tempfile.mkdtemp()) / "export.txt"
    p.write_text("\n".join(return_links))
    res = tr.import_export(p, limit=3)
    assert res == {"found": 5, "saved": 3, "duplicates": 0}
    assert llm.calls == []                                   # bulk import must not burn API calls
    assert len(store.list_inspiration(10)) == 3


@needs_ffmpeg
def test_add_media_screenshot_is_analysed_with_vision(media_dir, tmp_path):
    llm = FakeLLM()
    tr, store = make_researcher(llm)
    shot = tmp_path / "douyin.png"
    shot.write_bytes((media_dir / "square.png").read_bytes())
    res = tr.add_media(shot, note="抖音 format I like")
    blocks = llm.calls[0]["content"]
    assert blocks[0]["type"] == "image" and "抖音" in blocks[1]["text"]
    assert res["analysis"]["hook"] == "Wait for it"
    assert store.list_inspiration(1)[0]["platform"] == "screenshot"
