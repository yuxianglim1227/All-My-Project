import threading
from datetime import timedelta

import pytest

from conftest import needs_ffmpeg
from helpers import T0, make_rig, one_post, set_plan
from socialagent import media
from socialagent.models import UNDERSTAND_SCHEMA, WRITE_SCHEMA
from socialagent.timeutil import iso

pytestmark = needs_ffmpeg


def calls(rig, schema):
    return [c for c in rig.llm.calls if c["kind"] == "json" and c["schema"] is schema]


def posts(rig):
    return rig.store.list_posts(limit=100)


def button(card, action):
    return next((b for row in card.rows for b in row if b.action == action), None)


def actions(card):
    return [b.action for row in card.rows for b in row]


# ---------------------------------------------------------------- create
def test_create_shows_ack_then_edits_the_same_card_in_place(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    set_plan(rig.llm, one_post(), one_post(kind="story", when="next_slot", highlight="travel", on_screen_text="Latte time"))
    did = rig.submit([media_dir / "photo.jpg"], "coffee", approve=False)

    ack, final = rig.notifier.cards[0], rig.notifier.cards[-1]
    assert "Got it" in ack[0].text and ack[0].rows == []
    assert final[1] == ack[1]                                           # edited in place: same message handle
    card = final[0]
    assert card.kind == "draft" and card.ref == did and f"[{did}]" in card.text
    assert "Instagram post" in card.text and "Coffee o'clock" in card.text and "#coffee" in card.text
    assert "Text sticker idea: Latte time" in card.text and "Highlight: Travel" in card.text
    assert rig.store.get_draft(did)["state"] == "ready" and rig.store.get_draft(did)["card_ref"] == ack[1]
    assert posts(rig) == []                                             # NOTHING is scheduled before approval


def test_vision_runs_once_and_writer_gets_text_only(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    rig.submit([media_dir / "photo.jpg"], approve=False)
    (u,), (w,) = calls(rig, UNDERSTAND_SCHEMA), calls(rig, WRITE_SCHEMA)
    assert any(b["type"] == "image" for b in u["content"])               # the vision call sees the pixels ...
    assert isinstance(w["content"], str) and "base64" not in w["content"]   # ... the writer never does
    assert "ELIGIBLE TARGETS" in u["content"][0]["text"] and "instagram photo" in u["content"][0]["text"]
    assert "a cafe table" in w["content"] and "FACTS" in w["content"]


def test_eligible_targets_can_all_be_toggled_for_a_video(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    set_plan(rig.llm, one_post("instagram", "reel"))
    did = rig.submit([media_dir / "landscape.mp4"], approve=False)
    d = rig.store.get_draft(did)
    assert [(t["platform"], t["kind"], t["selected"]) for t in d["targets"]] == [
        ("instagram", "reel", True), ("instagram", "story", False), ("tiktok", "video", False)]
    assert actions(rig.notifier.last_card).count("tgt:t1") == 1 and button(rig.notifier.last_card, "tgt:t3").label.startswith("☐")


def test_style_hints_typed_in_the_caption_preselect_tone_and_language(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    did = rig.submit([media_dir / "photo.jpg"], "funny, in 中文 please", approve=False)
    d = rig.store.get_draft(did)
    assert (d["tone"], d["lang"]) == ("funny", "zh")
    w = rig.llm.write_calls[0]
    assert "Tone: Funny" in w and "简体中文" in w and "funny, in 中文 please" in w
    card = rig.notifier.last_card
    assert button(card, "tone:funny").selected and button(card, "lang:zh").selected and not button(card, "tone:descriptive").selected


def test_defaults_come_from_prefs_when_no_hint(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    rig.prefs.set_tone("aesthetic")
    rig.prefs.set_language("bi")
    did = rig.submit([media_dir / "photo.jpg"], "just post it", approve=False)
    assert (rig.store.get_draft(did)["tone"], rig.store.get_draft(did)["lang"]) == ("aesthetic", "bi")
    assert "English first, then" in rig.llm.write_calls[0]


# ---------------------------------------------------------------- changing the style is cheap
def test_tone_and_language_taps_rerun_only_the_writer_and_edit_in_place(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    handle = rig.notifier.cards[0][1]
    rig.drafts.handle_action(did, "tone:funny")
    rig.drafts.handle_action(did, "lang:zh-hant")
    assert len(calls(rig, UNDERSTAND_SCHEMA)) == 1 and len(calls(rig, WRITE_SCHEMA)) == 3     # vision once; writer per change
    assert "Tone: Funny" in rig.llm.write_calls[1] and "繁體中文" in rig.llm.write_calls[2]
    d = rig.store.get_draft(did)
    assert (d["tone"], d["lang"], d["state"]) == ("funny", "zh-hant", "ready")
    assert {h for _, h in rig.notifier.cards} == {handle}                      # never a new message: always the same card
    assert "PREVIOUS DRAFT" in rig.llm.write_calls[1]                         # a rewrite is told not to repeat itself


def test_unknown_tone_is_ignored(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    rig.drafts.set_tone(did, "unicorn")
    assert rig.store.get_draft(did)["tone"] == "descriptive" and len(calls(rig, WRITE_SCHEMA)) == 1


def test_toggling_a_target_writes_only_that_one_when_needed(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    assert "- t1: instagram photo" in rig.llm.write_calls[0] and "- t2" not in rig.llm.write_calls[0]
    rig.drafts.handle_action(did, "tgt:t3")                                   # TikTok photo: not written yet
    w = rig.llm.write_calls[1]
    assert "- t3: tiktok photo" in w and "- t1" not in w
    d = rig.store.get_draft(did)
    assert [t["selected"] for t in d["targets"]] == [True, False, True] and d["targets"][2]["written"]
    n = len(rig.llm.write_calls)
    rig.drafts.handle_action(did, "tgt:t3")                                   # off again: no LLM call
    rig.drafts.handle_action(did, "tgt:t3")                                   # back on, already written: no LLM call
    assert len(rig.llm.write_calls) == n


def test_change_request_and_literal_caption(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    set_plan(rig.llm, one_post(), one_post(kind="story", on_screen_text="hi"))
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    rig.drafts.instruct(did, "shorter, mention Sunday")
    assert "CHANGE REQUEST (for this rewrite):\nshorter, mention Sunday" in rig.llm.write_calls[-1]

    rig.drafts.toggle_target(did, "t2")                                       # story selected too
    rig.drafts.instruct(did, "= Sunday coffee, my way")
    d = rig.store.get_draft(did)
    feed_t, story_t = d["targets"][0], d["targets"][1]
    assert feed_t["caption"] == "Sunday coffee, my way" and feed_t["hashtags"] == ["#coffee"]   # words kept, tags kept
    assert story_t["caption"] == ""                                           # Stories never get a caption
    rig.drafts.instruct(did, "= own words #mine")
    assert rig.store.get_draft(did)["targets"][0]["hashtags"] == []           # they typed their own tags
    n = len(rig.llm.write_calls)
    rig.drafts.instruct(did, "   ")
    rig.drafts.set_caption(did, "")
    assert len(rig.llm.write_calls) == n


def test_writer_failure_keeps_the_previous_text_and_says_so(media_dir, tmp_path):
    from socialagent.llm import LLMError
    rig = make_rig(tmp_path)
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    before = rig.store.get_draft(did)["targets"][0]["caption"]
    rig.llm.write_error = LLMError("Claude is rate-limiting me right now; try again in a minute.")
    rig.drafts.handle_action(did, "tone:funny")
    d = rig.store.get_draft(did)
    assert d["state"] == "ready" and d["targets"][0]["caption"] == before
    assert "Couldn't rewrite" in rig.notifier.last_card.text and "rate-limiting" in rig.notifier.last_card.text
    rig.llm.write_error = None
    rig.drafts.handle_action(did, "rewrite")
    assert "Couldn't rewrite" not in rig.notifier.last_card.text              # the note was one-shot


def test_failed_draft_offers_retry_and_recovers(media_dir, tmp_path, monkeypatch):
    from socialagent.llm import LLMError
    rig = make_rig(tmp_path)
    real = rig.llm.json
    monkeypatch.setattr(rig.llm, "json", lambda **kw: (_ for _ in ()).throw(LLMError("ANTHROPIC_API_KEY is missing or invalid.")))
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    card = rig.notifier.last_card
    assert rig.store.get_draft(did)["state"] == "failed" and "ANTHROPIC_API_KEY" in card.text and "nothing was posted" in card.text
    assert actions(card) == ["retry", "discard"] and posts(rig) == []
    monkeypatch.setattr(rig.llm, "json", real)
    rig.drafts.handle_action(did, "retry")
    assert rig.store.get_draft(did)["state"] == "ready" and "Instagram post" in rig.notifier.last_card.text


def test_unreadable_and_empty_input_never_creates_a_draft(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    assert rig.drafts.create([media_dir / "notes.txt"], "", "test") is None
    assert "couldn't read that file" in rig.notifier.texts[-1]
    assert rig.drafts.create([], "", "test") is None
    assert rig.store.list_drafts() == []


# ---------------------------------------------------------------- approve / discard
def test_approve_schedules_posts_and_replaces_the_card_with_cancel_buttons(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    set_plan(rig.llm, one_post(), one_post("tiktok", "photo", caption="tt caption", hashtags=["#fyp"]))
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    assert posts(rig) == []
    rig.drafts.approve(did)
    ps = sorted(posts(rig), key=lambda p: p["platform"])
    assert [(p["platform"], p["kind"], p["status"], p["method"]) for p in ps] == [
        ("instagram", "photo", "scheduled", "api"), ("tiktok", "photo", "scheduled", "api")]
    assert ps[0]["publish_at"] == iso(T0.replace(hour=12, minute=30)) and ps[1]["publish_at"] == iso(T0.replace(hour=18))
    card = rig.notifier.last_card
    assert card.kind == "post" and card.text.startswith("✅ Approved draft") and [b.action for b in card.rows[0]] == [f"cancel:{ps[0]['id']}"]
    assert rig.store.get_draft(did)["state"] == "approved"
    n = len(posts(rig))
    rig.drafts.approve(did)                                                   # a second tap does nothing
    rig.drafts.handle_action(did, "tone:funny")                               # and old buttons can't edit an approved draft
    assert len(posts(rig)) == n and "already approved" in rig.notifier.texts[-1]


def test_now_waits_for_the_undo_window(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    rig.submit([media_dir / "photo.jpg"], now=True)
    assert posts(rig)[0]["publish_at"] == iso(T0 + timedelta(seconds=120))


def test_nothing_selected_blocks_approval(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    rig.drafts.toggle_target(did, "t1")
    rig.drafts.approve(did)
    assert posts(rig) == [] and "Pick at least one place" in rig.notifier.last_card.text


def test_risk_flags_need_a_second_confirming_tap(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    set_plan(rig.llm, one_post(), risk_flags=["other_people_visible"])
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    assert "⚠️ Flagged: other_people_visible" in rig.notifier.last_card.text
    assert button(rig.notifier.last_card, "approve") is not None
    rig.drafts.handle_action(did, "approve")
    assert posts(rig) == [] and button(rig.notifier.last_card, "approve!") is not None
    assert "Yes, approve anyway" in button(rig.notifier.last_card, "approve!").label
    rig.drafts.handle_action(did, "approve!")
    assert len(posts(rig)) == 1


def test_discard_is_final_and_posts_nothing(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    rig.drafts.handle_action(did, "discard")
    assert rig.store.get_draft(did)["state"] == "discarded" and "Discarded" in rig.notifier.last_card.text
    rig.drafts.approve(did)
    assert posts(rig) == [] and "already discarded" in rig.notifier.texts[-1]


def test_prepare_failure_is_reported_on_the_card_and_draft_stays_open(media_dir, tmp_path, monkeypatch):
    from socialagent.media import MediaError
    rig = make_rig(tmp_path)
    monkeypatch.setattr(media, "compliant_image", lambda *a, **k: (_ for _ in ()).throw(MediaError("bad image")))
    did = rig.submit([media_dir / "photo.jpg"])
    assert posts(rig) == [] and rig.store.get_draft(did)["state"] == "ready"
    assert "Couldn't schedule" in rig.notifier.last_card.text and "bad image" in rig.notifier.last_card.text


# ---------------------------------------------------------------- Phase 1 media: originals pass through
def test_phase1_passes_jpeg_through_byte_for_byte_and_only_fixes_what_platforms_reject(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    rig.submit([media_dir / "photo.jpg"])
    out = posts(rig)[0]["media"][0]["path"]
    assert open(out, "rb").read() == (media_dir / "photo.jpg").read_bytes()    # untouched original

    rig2 = make_rig(tmp_path / "b")
    rig2.submit([media_dir / "square.png"])                                    # PNG: Instagram needs JPEG
    out2 = posts(rig2)[0]["media"][0]["path"]
    assert out2.endswith(".jpg") and media.probe(out2).kind == "image" and open(out2, "rb").read()[:2] == b"\xff\xd8"

    rig3 = make_rig(tmp_path / "c")
    rig3.submit([media_dir / "tall.png"])                                      # 1:4 feed photo: padded to a ratio the API accepts
    p3 = media.probe(posts(rig3)[0]["media"][0]["path"])
    assert 0.8 <= p3.width / p3.height <= 1.91


def test_phase1_video_is_not_reframed_or_texted(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    set_plan(rig.llm, one_post("instagram", "reel", on_screen_text="Wait for it", trim_start=1, trim_end=2))
    rig.submit([media_dir / "landscape.mp4"])
    out = posts(rig)[0]["media"][0]["path"]
    p = media.probe(out)
    assert (p.width, p.height, p.vcodec) == (1280, 720, "h264")                # still landscape: nothing creative happened
    assert open(out, "rb").read() == (media_dir / "landscape.mp4").read_bytes()  # h264/aac mp4: copied as-is


def test_fit_and_creative_levels_still_exist_for_phase_2(media_dir, tmp_path):
    rig = make_rig(tmp_path, env={"EDIT_LEVEL": "fit"})
    set_plan(rig.llm, one_post("instagram", "reel"))
    rig.submit([media_dir / "landscape.mp4"])
    p = media.probe(posts(rig)[0]["media"][0]["path"])
    assert (p.width, p.height) == (1080, 1920)                                 # fit: blur-padded to 9:16


# ---------------------------------------------------------------- manual hand-off
def test_manual_method_sends_a_ready_package_and_waits_for_confirmation(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    rig.prefs.set_method("instagram", "manual")
    set_plan(rig.llm, one_post(kind="story", highlight="Food", on_screen_text="Hotpot night"))
    did = rig.submit([media_dir / "photo.jpg"], now=True)
    p = posts(rig)[0]
    assert p["method"] == "manual" and p["media"][0]["path"] == str(media_dir / "photo.jpg")     # originals, untouched
    assert "I'll send you a ready package" in rig.notifier.last_card.text
    ig = rig.publishers["instagram"]
    rig.pipeline.tick(T0 + timedelta(minutes=3))
    assert ig.requests == []                                                   # the API publisher was NOT used
    assert rig.store.get_post(p["id"])["status"] == "handed_off"
    assert any("Time to post your Instagram story" in t for t in rig.notifier.texts)
    text, acts = rig.notifier.messages[-1]
    assert "Suggested text sticker" in text and "Hotpot night" in text and acts == [("✅ I posted it", f"posted:{p['id']}")]
    assert rig.store.pending_highlights() == []                                # not until the owner confirms
    assert rig.pipeline.confirm_posted(p["id"]) is True
    assert rig.store.get_post(p["id"])["status"] == "published" and [h["collection"] for h in rig.store.pending_highlights()] == ["Food"]
    assert rig.pipeline.confirm_posted(p["id"]) is False
    assert did


def test_default_method_is_manual_without_credentials_and_api_with_them(tmp_path):
    from socialagent.config import BrandProfile
    from socialagent.prefs import Prefs
    from socialagent.store import Store
    from socialagent.styles import StyleCatalog
    store, brand = Store(":memory:"), BrandProfile()
    cat = StyleCatalog.from_brand()
    assert Prefs(store, brand, cat).method("instagram") == "manual"           # useful from the very first message
    prefs = Prefs(store, brand, cat, lambda platform: platform == "tiktok")
    assert (prefs.method("instagram"), prefs.method("tiktok")) == ("manual", "api")
    prefs.set_method("instagram", "api")
    assert prefs.method("instagram") == "api"
    with pytest.raises(ValueError):
        prefs.set_method("instagram", "carrier-pigeon")


# ---------------------------------------------------------------- settings, cards, brand reload, concurrency
def test_settings_card_and_actions_persist(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    card = rig.drafts.settings_card()
    assert card.kind == "settings" and "Tone: Describe" in card.text and button(card, "method:instagram:manual") is not None
    assert rig.drafts.settings_action("tone:hype") == "Saved" and rig.drafts.settings_action("lang:zh") == "Saved"
    assert rig.drafts.settings_action("method:instagram:manual") == "Saved"
    assert rig.drafts.settings_action("tone:nope").startswith("Unknown tone") and rig.drafts.settings_action("bogus") == ""
    again = rig.drafts.settings_card()
    assert button(again, "tone:hype").selected and button(again, "lang:zh").selected and button(again, "method:instagram:manual").selected
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    assert (rig.store.get_draft(did)["tone"], rig.store.get_draft(did)["lang"]) == ("hype", "zh")


def test_card_stays_within_telegram_limits_and_has_the_expected_buttons(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    set_plan(rig.llm, one_post(caption="x" * 2000), one_post("tiktok", "photo", caption="y" * 2000),
             one_post(kind="story", on_screen_text="z"))
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    rig.drafts.toggle_target(did, "t2")
    card = rig.notifier.last_card
    assert len(card.text) <= 3900
    acts = actions(card)
    for expected in ("tone:funny", "lang:zh", "lang:bi", "tgt:t1", "rewrite", "mycap", "change", "approve", "now", "discard"):
        assert expected in acts, expected
    assert all(len(f"d:{did}:{a}") <= 64 for a in acts)                       # Telegram callback_data limit


def test_custom_tones_from_brand_yaml_appear_after_a_hot_reload(media_dir, tmp_path):
    import os
    import time
    rig = make_rig(tmp_path)
    f = rig.settings.brand_file
    f.write_text(f.read_text() + '\ntones:\n  - { key: zesty, label: "Zesty", emoji: "🍋", guidance: "Bright and tangy." }\n')
    os.utime(f, (time.time() + 5, time.time() + 5))
    did = rig.submit([media_dir / "photo.jpg"], "zesty", approve=False)         # 'zesty' is not a built-in hint ...
    assert rig.store.get_draft(did)["tone"] == "descriptive"
    assert button(rig.notifier.last_card, "tone:zesty") is None                # (custom tones sit after the built-in pages)
    rig.drafts.handle_action(did, "tonepage:2")
    assert button(rig.notifier.last_card, "tone:zesty") is not None            # ... but the button exists
    rig.drafts.handle_action(did, "tone:zesty")
    assert "Tone: Zesty - Bright and tangy." in rig.llm.write_calls[-1]


def test_rapid_taps_are_serialised_per_draft(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    errors = []

    def tap(action):
        try:
            rig.drafts.handle_action(did, action)
        except Exception as exc:                                               # pragma: no cover - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=tap, args=(a,)) for a in ("tone:funny", "lang:zh", "tone:hype", "rewrite", "lang:en")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    d = rig.store.get_draft(did)
    assert errors == [] and d["state"] == "ready" and d["tone"] in {"funny", "hype", "descriptive"} and d["targets"][0]["written"]


def test_prompts_for_free_text_actions_and_list_and_reopen(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    assert "exactly as written" in rig.drafts.handle_action(did, "mycap").prompt
    assert "What should I change" in rig.drafts.handle_action(did, "change").prompt
    rig.drafts.list_open()
    text, acts = rig.notifier.messages[-1]
    assert f"[{did}]" in text and acts == [("Open", f"d:{did}:show")]
    before = {h for _, h in rig.notifier.cards}
    rig.drafts.show(did)                                                       # /draft <id>: a fresh message
    assert {h for _, h in rig.notifier.cards} - before and rig.store.get_draft(did)["card_ref"] != min(before)
    rig.drafts.show("nope")
    assert "can't find draft [nope]" in rig.notifier.texts[-1]


def test_a_channel_supplied_handle_is_persisted_so_every_update_edits_that_message(media_dir, tmp_path):
    """Regression: the handle used to live only in memory, so later updates sent a SECOND message instead of editing."""
    rig = make_rig(tmp_path)
    did = rig.drafts.create([media_dir / "photo.jpg"], "", "test", handle="777")
    assert rig.store.get_draft(did)["card_ref"] == "777"
    rig.drafts.handle_action(did, "tone:funny")
    rig.drafts.handle_action(did, "lang:zh")
    rig.drafts.approve(did)
    assert {h for _, h in rig.notifier.cards} == {"777"} and len(rig.notifier.cards) >= 5


def test_more_tones_are_reachable_by_paging_and_the_selected_tone_page_is_shown(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    card = rig.notifier.last_card
    assert button(card, "tone:funny") is not None and button(card, "tone:sassy") is None and button(card, "tonepage:1").label == "➕ More 1/3"
    n = len(rig.llm.write_calls)
    rig.drafts.handle_action(did, "tonepage:1")                               # paging is free: no AI call
    card = rig.notifier.last_card
    assert button(card, "tone:sassy") is not None and button(card, "tone:funny") is None and button(card, "tonepage:2").label == "➕ More 2/3"
    assert len(rig.llm.write_calls) == n
    rig.drafts.handle_action(did, "tone:sassy")
    assert rig.store.get_draft(did)["tone"] == "sassy" and "Tone: Sassy" in rig.llm.write_calls[-1]
    assert button(rig.notifier.last_card, "tone:sassy").selected
    rig.drafts.handle_action(did, "tonepage:2"); rig.drafts.handle_action(did, "tonepage:0")
    assert button(rig.notifier.last_card, "tone:funny") is not None           # wraps around
    # a draft that starts on a tone from page 3 opens on that page
    did2 = rig.submit([media_dir / "photo.jpg"], "deadpan", approve=False)
    assert button(rig.notifier.last_card, "tone:deadpan").selected and button(rig.notifier.last_card, "tonepage:0") is not None
    assert rig.store.get_draft(did2)["tone"] == "deadpan"


def test_settings_card_pages_through_tones_too(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    assert button(rig.drafts.settings_card(), "tone:funny") is not None
    assert rig.drafts.settings_action("tonepage:1") == ""
    page2 = rig.drafts.settings_card()
    assert button(page2, "tone:sassy") is not None and button(page2, "tone:funny") is None
    assert rig.drafts.settings_action("tone:sassy") == "Saved" and rig.prefs.tone == "sassy"


# ================================================================ Phase 2a: edited masters + previews ("what you see is what posts")
ENH = {"EDIT_LEVEL": "enhance"}


def test_masters_and_previews_are_rendered_while_the_draft_is_built(media_dir, tmp_path):
    from pathlib import Path
    rig = make_rig(tmp_path, env=ENH)
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    d = rig.store.get_draft(did)
    master = d["masters"][0]["path"]
    assert d["look"] == "natural" and master != str(media_dir / "photo.jpg") and master.startswith(str(rig.settings.data_dir / "drafts"))
    assert media.probe(master).vcodec == "mjpeg" and open(master, "rb").read() != (media_dir / "photo.jpg").read_bytes()
    card = rig.notifier.last_card
    assert len(card.previews) == 1 and card.previews[0].kind == "image" and Path(card.previews[0].path).exists()
    assert media.probe(card.previews[0].path).width <= 1280
    assert "✨ Natural" in card.text and card.preview_caption == "👀 Final media - Natural look"
    assert button(card, "look:vivid") is not None and button(card, "look:natural").selected and button(card, "look:original") is not None
    assert button(card, "compare") is not None


def test_approve_publishes_exactly_the_file_you_previewed(media_dir, tmp_path):
    rig = make_rig(tmp_path, env=ENH)
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    master = rig.store.get_draft(did)["masters"][0]["path"]
    rig.drafts.approve(did)
    sent = posts(rig)[0]["media"][0]["path"]
    assert open(sent, "rb").read() == open(master, "rb").read()                 # the previewed master, byte for byte
    assert open(sent, "rb").read() != (media_dir / "photo.jpg").read_bytes()    # ... and it is the edited version, not the original


def test_switching_look_rerenders_swaps_the_preview_and_never_calls_the_ai(media_dir, tmp_path):
    import os
    rig = make_rig(tmp_path, env=ENH)
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    natural = rig.store.get_draft(did)["masters"][0]["path"]
    key1 = rig.notifier.last_card.previews[0].key
    writes, vision = len(rig.llm.write_calls), len(calls(rig, UNDERSTAND_SCHEMA))

    rig.drafts.handle_action(did, "look:moody")
    d = rig.store.get_draft(did)
    assert d["look"] == "moody" and d["masters"][0]["path"] != natural and rig.notifier.last_card.previews[0].key != key1
    assert "Re-rendering" in rig.notifier.cards[-2][0].text                       # a status card while it renders
    assert len(rig.llm.write_calls) == writes and len(calls(rig, UNDERSTAND_SCHEMA)) == vision

    mtime = os.stat(natural).st_mtime_ns
    rig.drafts.handle_action(did, "look:natural")                                 # back to a look rendered before: cached, instant
    assert os.stat(natural).st_mtime_ns == mtime and rig.store.get_draft(did)["masters"][0]["path"] == natural

    rig.drafts.handle_action(did, "look:original")
    d = rig.store.get_draft(did)
    assert d["masters"][0]["path"] == str(media_dir / "photo.jpg")                # original = untouched file
    card = rig.notifier.last_card
    assert button(card, "compare") is None and card.preview_caption.endswith("(as you sent it)") and button(card, "look:original").selected
    n = len(rig.notifier.cards)
    rig.drafts.handle_action(did, "look:original")                                 # same look again: nothing to render
    assert len(rig.notifier.cards) == n + 1 and "Re-rendering" not in rig.notifier.last_card.text
    rig.drafts.handle_action(did, "look:sepia")                                    # unknown look ignored
    assert rig.store.get_draft(did)["look"] == "original"


def test_look_hints_and_defaults(media_dir, tmp_path):
    rig = make_rig(tmp_path, env=ENH)
    assert rig.store.get_draft(rig.submit([media_dir / "photo.jpg"], "moody please", approve=False))["look"] == "moody"
    rig.prefs.set_look("vivid")
    assert rig.store.get_draft(rig.submit([media_dir / "photo.jpg"], "", approve=False))["look"] == "vivid"
    d = rig.store.get_draft(rig.submit([media_dir / "photo.jpg"], "no edit, funny", approve=False))
    assert d["look"] == "original" and d["tone"] == "funny" and d["masters"][0]["path"] == str(media_dir / "photo.jpg")
    with pytest.raises(ValueError):
        rig.prefs.set_look("sepia")


def test_edit_level_none_means_originals_no_look_buttons_but_still_a_preview(media_dir, tmp_path):
    rig = make_rig(tmp_path)                                                       # the test rig defaults to EDIT_LEVEL=none
    did = rig.submit([media_dir / "photo.jpg"], "moody", approve=False)
    d, card = rig.store.get_draft(did), rig.notifier.last_card
    assert d["look"] == "original" and d["masters"][0]["path"] == str(media_dir / "photo.jpg")
    assert len(card.previews) == 1 and not any(a.startswith("look:") for a in actions(card)) and "✨" not in card.text
    rig.drafts.set_look(did, "vivid")
    assert rig.store.get_draft(did)["look"] == "original"


def test_video_master_is_h264_previewed_small_and_published_as_previewed(media_dir, tmp_path):
    rig = make_rig(tmp_path, env=ENH)
    set_plan(rig.llm, one_post("instagram", "reel"))
    did = rig.submit([media_dir / "landscape.mp4"], approve=False)
    master = rig.store.get_draft(did)["masters"][0]
    m = media.probe(master["path"])
    assert (m.vcodec, m.acodec, m.width, m.height) == ("h264", "aac", 1280, 720)
    pv = rig.notifier.last_card.previews[0]
    p = media.probe(pv.path)
    assert pv.kind == "video" and p.height <= 720 and p.size < 3_000_000
    rig.drafts.approve(did)
    assert open(posts(rig)[0]["media"][0]["path"], "rb").read() == open(master["path"], "rb").read()


def test_hdr_clips_are_left_as_shot(media_dir, tmp_path, monkeypatch):
    import dataclasses
    orig = media.probe
    monkeypatch.setattr(media, "probe", lambda p: dataclasses.replace(orig(p), hdr=True) if str(p).endswith("landscape.mp4") else orig(p))
    rig = make_rig(tmp_path, env=ENH)
    set_plan(rig.llm, one_post("instagram", "reel"))
    did = rig.submit([media_dir / "landscape.mp4"], approve=False)
    assert rig.store.get_draft(did)["masters"][0]["path"] == str(media_dir / "landscape.mp4")
    assert "HDR video left as shot" in rig.notifier.last_card.text


def test_an_enhancement_failure_falls_back_to_the_original_with_a_note(media_dir, tmp_path, monkeypatch):
    from socialagent import enhance
    from socialagent.media import MediaError
    monkeypatch.setattr(enhance, "enhance", lambda *a, **k: (_ for _ in ()).throw(MediaError("ffmpeg exploded")))
    rig = make_rig(tmp_path, env=ENH)
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    assert rig.store.get_draft(did)["state"] == "ready" and rig.store.get_draft(did)["masters"][0]["path"] == str(media_dir / "photo.jpg")
    assert "Couldn't enhance item 1" in rig.notifier.last_card.text and "ffmpeg exploded" in rig.notifier.last_card.text


def test_a_render_crash_never_blocks_the_draft(media_dir, tmp_path, monkeypatch):
    rig = make_rig(tmp_path, env=ENH)
    monkeypatch.setattr(rig.drafts, "_render", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    card = rig.notifier.last_card
    assert rig.store.get_draft(did)["state"] == "ready" and card.previews == [] and "Couldn't render the preview" in card.text
    rig.drafts.approve(did)
    assert open(posts(rig)[0]["media"][0]["path"], "rb").read() == (media_dir / "photo.jpg").read_bytes()    # posts the original


def test_compare_sends_a_before_after_photo(media_dir, tmp_path):
    rig = make_rig(tmp_path, env=ENH)
    did = rig.submit([media_dir / "photo.jpg"], approve=False)
    rig.drafts.handle_action(did, "compare")
    (paths, caption, as_photo), = rig.notifier.files
    assert as_photo is True and "Before | After" in caption and "Natural" in caption
    p = media.probe(paths[0])
    assert (p.kind, p.width, p.height) == ("image", 1440, 720)                        # two 720x720 halves, side by side
    rig.drafts.handle_action(did, "look:original")
    rig.drafts.compare(did)
    assert len(rig.notifier.files) == 1 and "nothing to compare" in rig.notifier.last_card.text


def test_manual_handoff_sends_the_edited_file_and_a_link_when_it_is_too_big(media_dir, tmp_path, monkeypatch):
    from socialagent.publishers import ManualPublisher
    rig = make_rig(tmp_path, env=ENH)
    rig.prefs.set_method("instagram", "manual")
    did = rig.submit([media_dir / "photo.jpg"], now=True)
    master = rig.store.get_draft(did)["masters"][0]["path"]
    rig.pipeline.tick(T0 + timedelta(minutes=3))
    assert rig.notifier.files == [([master], "Edited media - save it, then post", False)]               # sent as a document: full quality
    assert any("exact version you previewed" in t for t in rig.notifier.texts)

    rig2 = make_rig(tmp_path / "big", env=ENH)
    rig2.prefs.set_method("instagram", "manual")
    monkeypatch.setattr(ManualPublisher, "MAX_SEND_BYTES", 1)
    rig2.submit([media_dir / "photo.jpg"], now=True)
    rig2.pipeline.tick(T0 + timedelta(minutes=3))
    assert rig2.notifier.files == [] and any("too big to send here" in t and "https://agent.example/m/" in t for t in rig2.notifier.texts)

    rig3 = make_rig(tmp_path / "orig")                                                                   # EDIT_LEVEL=none: the original is already on the phone
    rig3.prefs.set_method("instagram", "manual")
    rig3.submit([media_dir / "photo.jpg"], now=True)
    rig3.pipeline.tick(T0 + timedelta(minutes=3))
    assert rig3.notifier.files == [] and any("original you sent me" in t for t in rig3.notifier.texts)


def test_settings_offer_a_default_look_only_when_editing_is_on(media_dir, tmp_path):
    rig = make_rig(tmp_path, env=ENH)
    card = rig.drafts.settings_card()
    assert button(card, "look:vivid") is not None and "default look: Natural" in card.text
    assert rig.drafts.settings_action("look:bright") == "Saved" and rig.prefs.look == "bright"
    assert rig.drafts.settings_action("look:sepia").startswith("Unknown look")
    assert button(make_rig(tmp_path / "n").drafts.settings_card(), "look:vivid") is None


def test_an_old_database_gains_the_new_draft_columns(tmp_path):
    import sqlite3
    from socialagent.store import Store
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE drafts (id TEXT PRIMARY KEY, submission_id TEXT NOT NULL, state TEXT NOT NULL, tone TEXT NOT NULL,
        lang TEXT NOT NULL, instruction TEXT NOT NULL, brief_json TEXT, targets_json TEXT NOT NULL DEFAULT '[]', card_ref TEXT,
        error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO drafts(id,submission_id,state,tone,lang,instruction,created_at,updated_at) VALUES('abc123def0','s','ready','funny','en','','t','t');""")
    con.commit(); con.close()
    store = Store(db)
    d = store.get_draft("abc123def0")
    assert d["look"] == "natural" and d["masters"] is None                           # defaults for the old row
    store.update_draft("abc123def0", look="moody", masters=[{"path": "x"}])
    assert store.get_draft("abc123def0")["look"] == "moody" and Store(db).get_draft("abc123def0")["masters"] == [{"path": "x"}]
