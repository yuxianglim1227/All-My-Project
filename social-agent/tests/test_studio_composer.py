"""Composer + calendar: library media -> scheduled posts through the real pipeline; reschedule, edit, cancel, publish."""

from datetime import timedelta

import pytest

from conftest import needs_ffmpeg
from helpers import T0, make_rig
from socialagent.studio.assets import AssetLibrary
from socialagent.studio.composer import ComposeError, Composer


@pytest.fixture()
def rig(tmp_path):
    r = make_rig(tmp_path, env={"EDIT_LEVEL": "enhance"})
    lib = AssetLibrary(r.store, r.settings.assets_dir)
    comp = Composer(settings=r.settings, store=r.store, assets=lib, pipeline=r.pipeline, prefs=r.prefs)
    return r, lib, comp


@needs_ffmpeg
def test_options_list_what_the_media_can_become(rig, media_dir):
    r, lib, comp = rig
    photo = lib.add_file(media_dir / "photo.jpg", "p.jpg", move=False)
    video = lib.add_file(media_dir / "landscape.mp4", "v.mp4", move=False)
    keys = [t["key"] for t in comp.options([photo["id"]])["targets"]]
    assert keys == ["instagram:photo", "instagram:story", "tiktok:photo"]
    assert [t["key"] for t in comp.options([video["id"]])["targets"]] == ["instagram:reel", "instagram:story", "tiktok:video"]
    assert [t["key"] for t in comp.options([photo["id"], video["id"]])["targets"]] == ["instagram:carousel"]
    opts = comp.options([photo["id"]])
    assert opts["default_look"] == "natural" and opts["timezone"] == "UTC" and opts["targets"][0]["suggested"]
    with pytest.raises(ComposeError, match="no longer exists"):
        comp.options(["missing"])


@needs_ffmpeg
def test_schedule_publishes_exactly_the_previewed_master(rig, media_dir):
    r, lib, comp = rig
    photo = lib.add_file(media_dir / "photo.jpg", "p.jpg", move=False)
    preview = comp.preview(photo["id"], "vivid")
    master = comp.master(photo["id"], "vivid")
    assert preview.exists() and master.path.endswith("vivid/master.jpg")

    res = comp.schedule([photo["id"]], [
        {"key": "instagram:photo", "caption": "Monday fuel", "hashtags": ["#coffee", "latte"], "when": "2026-11-12T19:30"},
        {"key": "instagram:story", "caption": "New on the menu!"}], look="vivid")
    posts = {p["kind"]: p for p in res["posts"]}
    assert posts["photo"]["when"].startswith("2026-11-12T19:30") and "#coffee #latte" in posts["photo"]["caption"]
    assert posts["story"]["caption"] == "New on the menu!"                     # the Story sticker text
    sub = r.store.get_submission(r.store.get_post(posts["photo"]["id"])["submission_id"])
    assert '"asset_id"' in sub["media_json"] and master.path in sub["media_json"]   # built from the previewed master

    # it really publishes through the pipeline
    r.clock.now = T0 + timedelta(days=2)
    assert r.pipeline.tick() == 2
    assert {p["status"] for p in r.store.list_posts()} == {"published"}


@needs_ffmpeg
def test_schedule_validation(rig, media_dir):
    r, lib, comp = rig
    photo = lib.add_file(media_dir / "photo.jpg", "p.jpg", move=False)
    with pytest.raises(ComposeError, match="can't be published"):
        comp.schedule([photo["id"]], [{"key": "instagram:reel", "caption": "x"}])
    with pytest.raises(ComposeError, match="caption for: TikTok photo post"):
        comp.schedule([photo["id"]], [{"key": "tiktok:photo", "caption": ""}])
    with pytest.raises(ComposeError, match="place to post"):
        comp.schedule([photo["id"]], [])
    with pytest.raises(ComposeError, match="can't read the time"):
        comp.schedule([photo["id"]], [{"key": "tiktok:photo", "caption": "x", "when": "next tuesday-ish"}])
    with pytest.raises(ComposeError, match="Unknown look"):
        comp.schedule([photo["id"]], [{"key": "tiktok:photo", "caption": "x"}], look="sepia")
    assert r.store.list_posts() == []                                            # nothing half-created


@needs_ffmpeg
def test_calendar_reschedule_edit_cancel_and_files(rig, media_dir):
    r, lib, comp = rig
    photo = lib.add_file(media_dir / "photo.jpg", "p.jpg", move=False)
    pid = comp.schedule([photo["id"]], [{"key": "instagram:photo", "caption": "hi", "when": "2026-11-12T19:30"}],
                        look="original")["posts"][0]["id"]
    items = comp.posts(T0, T0 + timedelta(days=7))
    assert [i["id"] for i in items] == [pid] and items[0]["thumb_url"].endswith(f"{photo['id']}/thumb")

    moved = comp.reschedule(pid, "2026-11-13T09:15")
    assert moved["publish_at"].startswith("2026-11-13T09:15")
    with pytest.raises(ComposeError, match="already passed"):
        comp.reschedule(pid, "2026-11-01T09:00")
    assert comp.edit_caption(pid, "new words")["caption"] == "new words"
    with pytest.raises(ComposeError, match="2,200"):
        comp.edit_caption(pid, "x" * 2201)
    assert comp.media_file(pid, 0).is_file() and comp.media_file(pid, 1) is None and comp.media_file("nope", 0) is None

    assert r.pipeline.cancel(pid)
    with pytest.raises(ComposeError, match="haven't started"):
        comp.reschedule(pid, "2026-11-14T09:00")
    with pytest.raises(ComposeError, match="haven't started"):
        comp.edit_caption(pid, "too late")
    with pytest.raises(ComposeError, match="100 days"):
        comp.posts(T0, T0 + timedelta(days=200))


@needs_ffmpeg
def test_reschedule_cannot_move_a_post_that_is_being_published(rig, media_dir):
    r, lib, comp = rig
    photo = lib.add_file(media_dir / "photo.jpg", "p.jpg", move=False)
    pid = comp.schedule([photo["id"]], [{"key": "instagram:photo", "caption": "hi", "when": "2026-11-12T19:30"}],
                        look="original")["posts"][0]["id"]
    assert r.store.claim_post(pid)                                               # the publisher got it first
    with pytest.raises(ComposeError, match="haven't started"):
        comp.reschedule(pid, "2026-11-15T09:00")
    assert r.store.get_post(pid)["status"] == "publishing"


@needs_ffmpeg
def test_edit_level_none_forces_originals(tmp_path, media_dir):
    r = make_rig(tmp_path, env={"EDIT_LEVEL": "none"})
    lib = AssetLibrary(r.store, r.settings.assets_dir)
    comp = Composer(settings=r.settings, store=r.store, assets=lib, pipeline=r.pipeline, prefs=r.prefs)
    photo = lib.add_file(media_dir / "photo.jpg", "p.jpg", move=False)
    assert comp.master(photo["id"], "vivid").path == photo["path"]
    assert [x["key"] for x in comp.options([photo["id"]])["looks"]] == ["original"]
