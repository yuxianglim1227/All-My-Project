import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from helpers import T0, make_rig, one_post, set_plan
from socialagent import capcut, feed
from socialagent.config import BrandProfile
from socialagent.llm import LLMError
from socialagent.models import MediaItem
from socialagent.publishers import PublishError
from socialagent.timeutil import iso, tz

UTC = timezone.utc
H = timedelta(hours=1)
BRAND = BrandProfile.from_dict({
    "pillars": [{"name": "A", "weight": 50}, {"name": "B", "weight": 30}, {"name": "C", "weight": 20}],
    "highlights": [{"name": "Travel", "description": "trips"}, {"name": "Food"}],
})


def at(d, h, m=0):
    return datetime(2026, 11, d, h, m, tzinfo=UTC)


# ---------------------------------------------------------------- choose_slot
def slot(existing=(), now=None, windows=("12:30", "19:30"), zone="UTC", gap=180, cap=3, **kw):
    return feed.choose_slot(existing=list(existing), now=now or at(11, 10), windows=list(windows), zone=tz(zone),
                            min_gap=timedelta(minutes=gap), max_per_day=cap, **kw)


def test_slot_picks_next_window_today_then_tomorrow():
    assert slot() == at(11, 12, 30)
    assert slot(now=at(11, 13)) == at(11, 19, 30)
    assert slot(now=at(11, 20)) == at(12, 12, 30)


def test_slot_lead_time_skips_window_that_is_about_to_start():
    assert slot(now=at(11, 12, 29)) == at(11, 19, 30)             # < 2 min of lead: not enough time to undo


def test_slot_respects_min_gap_and_daily_cap():
    assert slot(existing=[at(11, 11)], gap=180) == at(11, 19, 30)             # 12:30 is only 90 min after 11:00
    assert slot(existing=[at(11, 12, 30), at(11, 19, 30)], cap=2) == at(12, 12, 30)
    assert slot(existing=[at(11, 8), at(11, 9)], cap=2) == at(12, 12, 30)     # day already full


def test_slot_uses_local_windows_in_the_owner_timezone():
    # 12:30 in Tokyo (UTC+9) is 03:30 UTC
    assert slot(now=at(11, 0), zone="Asia/Tokyo") == at(11, 3, 30)
    # local "today" is decided by the owner's zone, not UTC: it's already Nov 12 in Tokyo
    assert slot(now=at(11, 16), zone="Asia/Tokyo") == at(12, 3, 30)


def test_slot_jitter_and_empty_windows_and_fallback():
    assert slot(jitter=timedelta(minutes=-7)) == at(11, 12, 23)
    assert slot(windows=[]) == at(11, 12, 0)                                  # defaults to 12:00 / 19:00
    crowded = [at(11, 12) + timedelta(days=d, hours=h) for d in range(40) for h in range(0, 24, 2)]
    out = slot(existing=crowded, cap=99, horizon_days=2)
    assert out == max(crowded) + timedelta(minutes=180)                        # horizon exhausted: queue behind the latest


# ---------------------------------------------------------------- pillar balance & summaries
def _posts(*pillars):
    return [{"submission_id": f"s{i}", "pillar": p} for i, p in enumerate(pillars)]


def test_pillar_status_flags_underrepresented():
    text = feed.pillar_status(BRAND, _posts("A", "A", "A", "A", "B"))
    assert "A 80% (target 50%)" in text and "C 0% (target 20%)" in text
    assert "UNDER-REPRESENTED: C." in text and "B 20% (target 30%)" in text   # exactly 10 pts under is not flagged


def test_pillar_status_dedupes_cross_posts_and_handles_empty():
    cross = [{"submission_id": "s1", "pillar": "A"}, {"submission_id": "s1", "pillar": "A"}, {"submission_id": "s2", "pillar": "B"}]
    assert "last 2 piece(s)" in feed.pillar_status(BRAND, cross)
    assert feed.pillar_status(BRAND, []) == "" and feed.pillar_status(BrandProfile(), _posts("A")) == ""
    assert feed.pillar_status(BRAND, _posts("", "")) == ""


def test_feed_summary_formats_recent_posts():
    rows = [{"publish_at": iso(at(9, 12)), "platform": "instagram", "kind": "reel", "pillar": "A", "caption": "Hello\nworld"},
            {"publish_at": iso(at(11, 12)), "platform": "tiktok", "kind": "video", "pillar": "", "caption": ""}]
    out = feed.feed_summary(rows, at(11, 14), tz("UTC"))
    assert "2d ago" in out and "instagram reel | pillar: A | Hello" in out and "today" in out and "(no caption)" in out


# ---------------------------------------------------------------- highlights
def test_highlight_checklist_groups_by_collection(tmp_path):
    rig = make_rig(tmp_path)
    sid = rig.store.add_submission(source="t", instruction="", media=[])
    for col, h in [("Travel", 1), ("Food", 2), ("Travel", 3)]:
        pid = rig.store.add_post(submission_id=sid, platform="instagram", kind="story", caption="", hashtags=[], media=[],
                                 publish_at=at(10, h), status="published")
        rig.store.add_highlight_pending(pid, col)
    text = feed.highlight_checklist(rig.store, BRAND, tz("UTC"))
    assert text.count("Story from") == 3 and "Travel (trips)" in text and "\nFood" in text
    assert "/highlights_done" in text
    rig.store.mark_highlights_done()
    assert feed.highlight_checklist(rig.store, BRAND, tz("UTC")) == ""


# ---------------------------------------------------------------- scheduler
def test_tick_publishes_due_posts(tmp_path, media_dir):
    rig = make_rig(tmp_path)
    set_plan(rig.llm, one_post(when="now"))
    rig.pipeline.submit([media_dir / "photo.jpg"])
    rig.clock.now = T0 + timedelta(minutes=3)
    assert rig.scheduler.tick()["published"] == 1


def test_trends_refresh_daily_after_6am_local_and_throttles_failures(tmp_path):
    rig = make_rig(tmp_path)
    rig.clock.now = at(11, 5, 59)
    assert "trends" not in rig.scheduler.tick()                               # too early
    rig.clock.now = at(11, 6, 0)
    assert rig.scheduler.tick()["trends"] == 1
    rig.clock.now = at(11, 9)
    assert "trends" not in rig.scheduler.tick()                               # fresh: not stale yet
    rig.clock.now = at(12, 7)                                                 # 25h later: stale again
    real_json = rig.llm.json
    rig.llm.json = lambda **kw: (_ for _ in ()).throw(LLMError("rate limited"))
    assert "rate limited" in rig.scheduler.tick()["trends_error"]
    rig.clock.now = at(12, 8)
    assert "trends_error" not in rig.scheduler.tick()                         # within 3h of the failed attempt: wait
    rig.clock.now = at(12, 10, 30)
    rig.llm.json = real_json
    assert rig.scheduler.tick()["trends"] == 1


def test_trends_fallback_is_announced_to_the_owner(tmp_path):
    rig = make_rig(tmp_path)
    rig.llm.research_error = LLMError("no web search")
    rig.clock.now = at(11, 8)
    rig.scheduler.tick()
    assert any("evergreen" in t for t in rig.notifier.texts)


def test_weekly_review_sunday_evening_once(tmp_path):
    rig = make_rig(tmp_path)
    sunday = at(15, 17)                                                       # 2026-11-15 is a Sunday
    assert sunday.weekday() == 6
    rig.clock.now = sunday
    assert "review" not in rig.scheduler.tick()                               # before 18:00
    rig.clock.now = at(15, 18, 5)
    assert rig.scheduler.tick()["review"] is True
    assert any("Weekly feed review" in t for t in rig.notifier.texts)
    rig.clock.now = at(15, 20)
    assert "review" not in rig.scheduler.tick()                               # only once
    rig.clock.now = at(22, 18, 5)
    assert rig.scheduler.tick()["review"] is True                             # next Sunday


def test_token_refresh_intervals_and_error_reporting(tmp_path):
    rig = make_rig(tmp_path)
    calls = {"ig": 0, "tt": 0}

    def ig():
        calls["ig"] += 1
        return "ok"

    def tt():
        calls["tt"] += 1
        raise PublishError("refresh token revoked")

    rig.scheduler.refresh_instagram, rig.scheduler.refresh_tiktok = ig, tt
    rig.clock.now = at(11, 10)
    out = rig.scheduler.tick()
    assert calls == {"ig": 1, "tt": 1} and "revoked" in out["tiktok_token_error"]
    assert any("Couldn't refresh the TikTok token" in t for t in rig.notifier.texts)
    rig.clock.now = at(11, 20)
    rig.scheduler.tick()
    assert calls == {"ig": 1, "tt": 1}                                        # not due again yet
    rig.clock.now = at(12, 10)
    rig.scheduler.tick()
    assert calls == {"ig": 1, "tt": 2}                                        # TikTok daily, Instagram weekly
    rig.clock.now = at(19, 10)
    rig.scheduler.tick()
    assert calls["ig"] == 2


def test_unexpected_token_error_does_not_crash_the_tick(tmp_path):
    rig = make_rig(tmp_path)
    rig.scheduler.refresh_instagram = lambda: (_ for _ in ()).throw(ConnectionError("offline"))
    rig.clock.now = at(11, 10)
    rig.scheduler.tick()
    assert not any("Couldn't refresh" in t for t in rig.notifier.texts)       # transient: retry silently next time


async def test_run_loop_survives_tick_errors_and_recovers_on_start(tmp_path):
    rig = make_rig(tmp_path)
    seen = {"recover": 0, "ticks": 0}
    rig.pipeline.recover = lambda: seen.__setitem__("recover", seen["recover"] + 1) or 0

    def flaky_tick(now=None):
        seen["ticks"] += 1
        if seen["ticks"] == 1:
            raise RuntimeError("transient")
        return {}

    rig.scheduler.tick = flaky_tick
    task = asyncio.create_task(rig.scheduler.run(interval=0.01))
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen["recover"] == 1 and seen["ticks"] >= 3


# ---------------------------------------------------------------- capcut / jianying (experimental)
pyjy = pytest.importorskip("pyJianYingDraft")


def test_capcut_draft_is_written_with_clip_and_text(tmp_path, media_dir):
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    clip = MediaItem(path=str(media_dir / "landscape.mp4"), kind="video", width=1280, height=720, duration=4.0)
    name = capcut.make_draft(drafts, "socialagent: weird/name?", [clip, clip], overlay_text="Hello 你好")
    assert name == "socialagent_weird_name_"
    content = (drafts / name / "draft_content.json").read_text(encoding="utf-8")
    assert (drafts / name / "draft_meta_info.json").is_file()
    assert "Hello" in content and content.count("landscape.mp4") >= 2


def test_capcut_draft_errors_are_explicit(tmp_path, media_dir):
    clip = MediaItem(path=str(media_dir / "landscape.mp4"), kind="video", duration=4.0)
    with pytest.raises(capcut.CapCutUnavailable, match="does not exist"):
        capcut.make_draft(tmp_path / "missing", "x", [clip])
    (tmp_path / "d").mkdir()
    with pytest.raises(capcut.CapCutUnavailable, match="No video clips"):
        capcut.make_draft(tmp_path / "d", "x", [MediaItem(path="/x.jpg", kind="image")])
    maker = capcut.draft_maker_for(str(tmp_path / "d"))
    from socialagent.models import PlannedPost
    msg = maker("sid1", 0, PlannedPost(platform="instagram", kind="reel", on_screen_text="Hi"), [clip])
    assert "Experimental" in msg and "socialagent_sid1_0" in msg
