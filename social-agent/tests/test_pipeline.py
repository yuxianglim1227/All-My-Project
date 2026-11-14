import threading
import time
from datetime import timedelta

from conftest import needs_ffmpeg
from helpers import ScriptedPublisher, fatal, make_rig, one_post, retryable, set_plan, T0
from socialagent import media
from socialagent.media import MediaError
from socialagent.models import FAILED, PUBLISHED, PUBLISHING, SCHEDULED
from socialagent.publishers import DryRunPublisher
from socialagent.timeutil import iso

pytestmark = needs_ffmpeg


def post_of(rig, did=None):
    posts = rig.store.list_posts(limit=100)
    if did is None:
        return posts
    sub = rig.store.get_draft(did)["submission_id"]
    return [p for p in posts if p["submission_id"] == sub]


# ---------------------------------------------------------------- planning & scheduling
def test_photo_is_prepared_scheduled_in_next_window_and_reported(media_dir, tmp_path):
    rig = make_rig(tmp_path, publishers={"instagram": DryRunPublisher("instagram"), "tiktok": DryRunPublisher("tiktok")})
    set_plan(rig.llm, one_post())
    did = rig.submit([media_dir / "photo.jpg"], "nice coffee")
    (p,) = post_of(rig, did)
    assert p["status"] == SCHEDULED and p["platform"] == "instagram" and p["kind"] == "photo"
    assert p["publish_at"] == iso(T0.replace(hour=12, minute=30))                 # next IG window after 10:00
    assert p["caption"] == "Coffee o'clock\n\n#coffee"
    prepared = media.probe(p["media"][0]["path"])
    assert (prepared.kind, prepared.width, prepared.height) == ("image", 1000, 1000)

    card = rig.notifier.last_card                                                    # the approved card replaces the draft card
    assert card.kind == "post" and "dry run - nothing will really be posted" in card.text
    assert f"[{p['id']}]" in card.text and "Wed 11 Nov 12:30" in card.text
    assert [b.action for b in card.rows[0]] == [f"cancel:{p['id']}"]
    assert rig.store.get_draft(did)["state"] == "approved"


def test_nothing_publishes_early_and_post_now_waits_for_the_undo_window(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    set_plan(rig.llm, one_post(when="now"))
    rig.submit([media_dir / "photo.jpg"])
    (p,) = post_of(rig)
    assert p["publish_at"] == iso(T0 + timedelta(seconds=120))
    assert rig.pipeline.tick(T0 + timedelta(seconds=119)) == 0 and rig.publishers["instagram"].requests == []
    assert rig.pipeline.tick(T0 + timedelta(seconds=121)) == 1
    assert rig.store.get_post(p["id"])["status"] == PUBLISHED
    assert any("✅ Posted Instagram photo" in t for t in rig.notifier.texts)
    assert rig.pipeline.tick(T0 + timedelta(days=1)) == 0                           # never published twice


def test_video_cross_post_schedules_reel_and_tiktok_in_their_own_windows(media_dir, tmp_path):
    rig = make_rig(tmp_path, env={"EDIT_LEVEL": "fit"})
    set_plan(rig.llm, one_post("instagram", "reel", caption="Reel", on_screen_text="Wait for it"),
             one_post("tiktok", "video", caption="TikTok", hashtags=["#fyp"]))
    rig.submit([media_dir / "landscape.mp4"], "post everywhere")
    ig, tt = sorted(post_of(rig), key=lambda p: p["platform"])
    assert ig["publish_at"] == iso(T0.replace(hour=12, minute=30)) and tt["publish_at"] == iso(T0.replace(hour=18))
    for p in (ig, tt):
        m = media.probe(p["media"][0]["path"])
        assert (m.kind, m.width, m.height) == ("video", 1080, 1920) and p["media"][0]["duration"] > 0
    assert tt["caption"] == "TikTok #fyp" and ig["caption"] == "Reel\n\n#coffee"   # IG: tags after a blank line; TikTok: inline


def test_slots_do_not_collide_across_submissions(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    set_plan(rig.llm, one_post())
    for _ in range(3):
        rig.submit([media_dir / "photo.jpg"])
    times = sorted(p["publish_at"] for p in post_of(rig))
    assert times == [iso(T0.replace(hour=12, minute=30)), iso(T0.replace(hour=19, minute=30)),
                     iso((T0 + timedelta(days=1)).replace(hour=12, minute=30))]


def test_explicit_time_honoured_and_past_time_falls_back_with_note(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    set_plan(rig.llm, one_post(when="2026-11-12T19:00"))
    rig.submit([media_dir / "photo.jpg"])
    assert post_of(rig)[0]["publish_at"] == iso(T0.replace(day=12, hour=19))
    set_plan(rig.llm, one_post(when="2026-11-10T09:00"))
    rig.submit([media_dir / "photo.jpg"])
    assert "had already passed" in rig.notifier.last_card.text


def test_story_goes_out_soon_and_feeds_the_highlight_checklist(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    set_plan(rig.llm, one_post(kind="story", highlight="travel", on_screen_text="Tokyo!"))
    rig.submit([media_dir / "photo.jpg"])
    (p,) = post_of(rig)
    assert p["publish_at"] == iso(T0 + timedelta(seconds=120)) and p["highlight"] == "Travel"
    assert p["caption"] == "Tokyo!"                                                  # Stories: the field carries the suggested text sticker
    assert media.probe(p["media"][0]["path"]).kind == "image"
    rig.pipeline.tick(T0 + timedelta(minutes=5))
    pending = rig.store.pending_highlights()
    assert [h["collection"] for h in pending] == ["Travel"]
    review = rig.pipeline.weekly_review()
    assert "Highlights to update" in review and "Travel" in review


def test_prepare_failure_for_one_post_does_not_block_the_others(media_dir, tmp_path, monkeypatch):
    rig = make_rig(tmp_path)
    set_plan(rig.llm, one_post("instagram", "reel"), one_post("tiktok", "video"))
    real = media.compliant_video

    def flaky(src, dst, **kw):
        if "tiktok" in str(dst):
            raise MediaError("boom")
        return real(src, dst, **kw)

    monkeypatch.setattr(media, "compliant_video", flaky)
    rig.submit([media_dir / "landscape.mp4"])
    assert [p["platform"] for p in post_of(rig)] == ["instagram"]
    assert "Couldn't prepare the tiktok video" in rig.notifier.last_card.text


# ---------------------------------------------------------------- approval / cancel / risk
def test_cancel_before_publish_wins_and_cannot_cancel_after(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    set_plan(rig.llm, one_post(when="now"), one_post("tiktok", "photo", when="now"))
    rig.submit([media_dir / "photo.jpg"])
    by_platform = {p["platform"]: p for p in post_of(rig)}
    a, b = by_platform["instagram"], by_platform["tiktok"]
    assert rig.pipeline.cancel(a["id"]) is True
    assert rig.pipeline.tick(T0 + timedelta(minutes=5)) == 1                         # only the TikTok one is left
    assert rig.store.get_post(a["id"])["status"] == "cancelled" and rig.publishers["instagram"].requests == []
    assert len(rig.publishers["tiktok"].requests) == 1
    assert rig.pipeline.cancel(b["id"]) is False and "Couldn't cancel" in rig.notifier.texts[-1]


# ---------------------------------------------------------------- publishing, retries, safety
def _single(rig, media_dir, **post):
    set_plan(rig.llm, one_post(when="now", **post))
    rig.submit([media_dir / "photo.jpg"])
    return post_of(rig)[0]["id"]


def test_retryable_errors_back_off_then_succeed(media_dir, tmp_path):
    pub = ScriptedPublisher("instagram", [retryable("rate limited"), retryable("still limited")])
    rig = make_rig(tmp_path, publishers={"instagram": pub, "tiktok": ScriptedPublisher("tiktok")})
    pid = _single(rig, media_dir)
    t = T0 + timedelta(minutes=3)
    assert rig.pipeline.tick(t) == 1
    p = rig.store.get_post(pid)
    assert (p["status"], p["attempts"], p["publish_at"]) == (SCHEDULED, 1, iso(t + timedelta(minutes=2)))
    assert "Trying again" in rig.notifier.texts[-1]
    t2 = t + timedelta(minutes=2)
    rig.pipeline.tick(t2)
    p = rig.store.get_post(pid)
    assert (p["attempts"], p["publish_at"]) == (2, iso(t2 + timedelta(minutes=5)))
    rig.pipeline.tick(t2 + timedelta(minutes=5))
    assert rig.store.get_post(pid)["status"] == PUBLISHED and len(pub.requests) == 3


def test_retries_are_capped(media_dir, tmp_path):
    pub = ScriptedPublisher("instagram", [retryable()] * 5)
    rig = make_rig(tmp_path, publishers={"instagram": pub, "tiktok": ScriptedPublisher("tiktok")})
    pid = _single(rig, media_dir)
    t = T0 + timedelta(minutes=3)
    for _ in range(6):
        rig.pipeline.tick(t)
        t += timedelta(minutes=20)
    p = rig.store.get_post(pid)
    assert p["status"] == FAILED and p["attempts"] == 3 and len(pub.requests) == 3


def test_fatal_error_fails_immediately_with_retry_button_then_manual_retry_works(media_dir, tmp_path):
    pub = ScriptedPublisher("instagram", [fatal("Instagram rejected the media")])
    rig = make_rig(tmp_path, publishers={"instagram": pub, "tiktok": ScriptedPublisher("tiktok")})
    pid = _single(rig, media_dir)
    rig.pipeline.tick(T0 + timedelta(minutes=3))
    assert rig.store.get_post(pid)["status"] == FAILED and len(pub.requests) == 1
    text, actions = rig.notifier.messages[-1]
    assert "rejected the media" in text and actions == [("🔁 Retry", f"retry:{pid}")]
    rig.clock.now = T0 + timedelta(minutes=10)
    assert rig.pipeline.retry(pid) is True
    rig.pipeline.tick(T0 + timedelta(minutes=11))
    assert rig.store.get_post(pid)["status"] == PUBLISHED
    assert rig.pipeline.retry(pid) is False                                          # only failed posts can be retried


def test_unexpected_publisher_exception_never_leaves_post_stuck(media_dir, tmp_path):
    pub = ScriptedPublisher("instagram", [RuntimeError("kaboom")])
    rig = make_rig(tmp_path, publishers={"instagram": pub, "tiktok": ScriptedPublisher("tiktok")})
    pid = _single(rig, media_dir)
    rig.pipeline.tick(T0 + timedelta(minutes=3))
    p = rig.store.get_post(pid)
    assert p["status"] == FAILED and "kaboom" in p["error"]


def test_missing_publisher_fails_cleanly(media_dir, tmp_path):
    rig = make_rig(tmp_path, publishers={"tiktok": ScriptedPublisher("tiktok")})
    pid = _single(rig, media_dir)
    rig.pipeline.tick(T0 + timedelta(minutes=3))
    assert rig.store.get_post(pid)["status"] == FAILED


def test_concurrent_ticks_publish_exactly_once(media_dir, tmp_path):
    pub = ScriptedPublisher("instagram", delay=0.3)
    rig = make_rig(tmp_path, publishers={"instagram": pub, "tiktok": ScriptedPublisher("tiktok")})
    _single(rig, media_dir)
    results = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        results.append(rig.pipeline.tick(T0 + timedelta(minutes=3)))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(results) == 1 and len(pub.requests) == 1


def test_recover_flags_interrupted_posts_and_never_resends_them(media_dir, tmp_path):
    pub = ScriptedPublisher("instagram")
    rig = make_rig(tmp_path, publishers={"instagram": pub, "tiktok": ScriptedPublisher("tiktok")})
    pid = _single(rig, media_dir)
    assert rig.store.claim_post(pid)                                                 # simulate a crash mid-publish
    assert rig.store.get_post(pid)["status"] == PUBLISHING
    assert rig.pipeline.recover() == 1
    assert rig.store.get_post(pid)["status"] == FAILED and "check the app" in rig.notifier.texts[-1].lower()
    assert rig.pipeline.tick(T0 + timedelta(days=1)) == 0 and pub.requests == []


def test_publishers_needing_urls_get_public_links_to_real_files(media_dir, tmp_path):
    pub = ScriptedPublisher("instagram", needs_url=True)
    rig = make_rig(tmp_path, publishers={"instagram": pub, "tiktok": ScriptedPublisher("tiktok")})
    _single(rig, media_dir)
    rig.pipeline.tick(T0 + timedelta(minutes=3))
    url = pub.requests[0].media[0].url
    assert url.startswith("https://agent.example/m/")
    _, _, token, name = url.rsplit("/", 3)
    assert rig.host.resolve(token, name).stat().st_size > 0
    assert pub.requests[0].caption.startswith("Coffee o'clock")


# ---------------------------------------------------------------- misc behaviour
def test_brand_file_is_hot_reloaded(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    set_plan(rig.llm, one_post())
    f = rig.settings.brand_file
    f.write_text(f.read_text().replace('account_name: "My Account"', 'account_name: "Renamed Brand"'))
    import os
    os.utime(f, (time.time() + 5, time.time() + 5))
    rig.submit([media_dir / "photo.jpg"])
    assert rig.pipeline.brand.account_name == "Renamed Brand"
    assert "Renamed Brand" in rig.llm.calls[-1]["system"]


def test_draft_maker_is_optional_and_failures_never_block_posting(media_dir, tmp_path):
    calls = []

    def maker(sid, n, pp, clips):
        calls.append((pp.kind, len(clips)))
        return "Draft ready"

    rig = make_rig(tmp_path, env={"EDIT_LEVEL": "creative"}, draft_maker=maker)
    set_plan(rig.llm, one_post("instagram", "reel"))
    rig.submit([media_dir / "landscape.mp4"])
    assert calls == [("reel", 1)] and "Draft ready" in rig.notifier.last_card.text

    def bad(*a):
        raise RuntimeError("no jianying")

    rig2 = make_rig(tmp_path / "b", env={"EDIT_LEVEL": "creative"}, draft_maker=bad)
    set_plan(rig2.llm, one_post("instagram", "reel"))
    rig2.submit([media_dir / "landscape.mp4"])
    assert len(post_of(rig2)) == 1 and "draft skipped: no jianying" in rig2.notifier.last_card.text

    rig3 = make_rig(tmp_path / "c", draft_maker=maker)                              # Phase 1 (EDIT_LEVEL=none): the hook is not used
    set_plan(rig3.llm, one_post("instagram", "reel"))
    rig3.submit([media_dir / "landscape.mp4"])
    assert calls == [("reel", 1)]


def test_queue_review_and_chat_use_real_state(media_dir, tmp_path):
    rig = make_rig(tmp_path)
    assert "Nothing queued" in rig.pipeline.queue_text()
    set_plan(rig.llm, one_post())
    rig.submit([media_dir / "photo.jpg"])
    q = rig.pipeline.queue_text()
    assert "instagram photo" in q and "Coffee o'clock" in q
    rig.llm.text_result = "Post more Reels."
    assert rig.pipeline.chat("what should I post?") == "Post more Reels."
    call = rig.llm.calls[-1]
    assert "Coffee o'clock" in call["content"] and "what should I post?" in call["content"] and "cannot post" in call["system"]
    rig.pipeline.tick(T0 + timedelta(days=1))
    assert "Published 1 post(s)" in rig.pipeline.weekly_review()


def test_cleanup_removes_only_old_files(media_dir, tmp_path):
    import os
    rig = make_rig(tmp_path)
    old, new = rig.settings.prepared_dir / "old", rig.settings.prepared_dir / "new"
    old.mkdir(); new.mkdir()
    (old / "x.jpg").write_bytes(b"1")
    ancient = time.time() - 30 * 86400
    os.utime(old, (ancient, ancient))
    rig.pipeline.cleanup()
    assert not old.exists() and new.exists()
