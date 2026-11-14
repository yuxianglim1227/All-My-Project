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
    f.write_text(f.read_text() + '\ntones:\n  - { key: sassy, label: "Sassy", emoji: "💅", guidance: "Confident and teasing." }\n')
    os.utime(f, (time.time() + 5, time.time() + 5))
    did = rig.submit([media_dir / "photo.jpg"], "sassy", approve=False)         # 'sassy' is not a built-in hint ...
    assert rig.store.get_draft(did)["tone"] == "descriptive"
    assert button(rig.notifier.last_card, "tone:sassy") is not None            # ... but the button exists
    rig.drafts.handle_action(did, "tone:sassy")
    assert "Tone: Sassy - Confident and teasing." in rig.llm.write_calls[-1]


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
