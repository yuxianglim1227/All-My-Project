import json
import os
import subprocess
import time

import pytest

from conftest import needs_ffmpeg
from socialagent import media
from socialagent.hosting import MediaHost

pytestmark = needs_ffmpeg


def streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"]


# ---------------------------------------------------------------- probe
def test_probe_video_and_image(media_dir):
    v = media.probe(media_dir / "landscape.mp4")
    assert (v.kind, v.width, v.height, v.has_audio) == ("video", 1280, 720, True)
    assert 3.5 < v.duration < 4.5
    s = media.probe(media_dir / "portrait_silent.mp4")
    assert (s.kind, s.has_audio) == ("video", False)
    i = media.probe(media_dir / "wide.png")
    assert (i.kind, i.width, i.height, i.duration) == ("image", 2000, 500, 0.0)
    assert media.probe(media_dir / "photo.jpg").kind == "image"


def test_probe_rejects_missing_and_non_media(media_dir):
    with pytest.raises(media.MediaError):
        media.probe(media_dir / "nope.mp4")
    with pytest.raises(media.MediaError):
        media.probe(media_dir / "notes.txt")


# ---------------------------------------------------------------- video
def test_prepare_video_is_vertical_h264_aac(media_dir, tmp_path):
    out = media.prepare_video(media_dir / "landscape.mp4", tmp_path / "o.mp4")
    st = {s["codec_type"]: s for s in streams(out)}
    assert (st["video"]["codec_name"], st["video"]["pix_fmt"]) == ("h264", "yuv420p")
    assert (st["video"]["width"], st["video"]["height"]) == (1080, 1920)
    assert st["audio"]["codec_name"] == "aac"
    assert 3.5 < media.probe(out).duration < 4.6


def test_prepare_video_trim_and_cap(media_dir, tmp_path):
    trimmed = media.prepare_video(media_dir / "landscape.mp4", tmp_path / "t.mp4", trim_start=1, trim_end=3)
    assert 1.7 < media.probe(trimmed).duration < 2.4
    capped = media.prepare_video(media_dir / "landscape.mp4", tmp_path / "c.mp4", max_seconds=1.5)
    assert 1.2 < media.probe(capped).duration < 1.9
    # nonsense trim values must not crash or produce an empty file
    odd = media.prepare_video(media_dir / "landscape.mp4", tmp_path / "x.mp4", trim_start=99, trim_end=1)
    assert media.probe(odd).duration > 0.3


def test_prepare_video_silent_source_stays_valid(media_dir, tmp_path):
    out = media.prepare_video(media_dir / "portrait_silent.mp4", tmp_path / "s.mp4")
    assert media.probe(out).has_audio is False
    assert (media.probe(out).width, media.probe(out).height) == (1080, 1920)


def test_prepare_video_overlay_changes_pixels_and_handles_cjk(media_dir, tmp_path):
    plain = media.prepare_video(media_dir / "portrait_silent.mp4", tmp_path / "p.mp4")
    texty = media.prepare_video(media_dir / "portrait_silent.mp4", tmp_path / "t.mp4",
                                overlay_text="Wait for it... it's: 100% worth it!")
    cjk = media.prepare_video(media_dir / "portrait_silent.mp4", tmp_path / "z.mp4", overlay_text="等一下 你一定要看到最后")
    a = media.thumbnail(plain, tmp_path / "a.jpg", 0.5).read_bytes()
    b = media.thumbnail(texty, tmp_path / "b.jpg", 0.5).read_bytes()
    assert a != b, "overlay text should alter the frame"
    assert cjk.stat().st_size > 0


def test_overlay_survives_percent_and_backslash(media_dir, tmp_path):
    """Regression: drawtext's default expansion silently drew nothing for text containing '%'."""
    plain = media.prepare_video(media_dir / "portrait_silent.mp4", tmp_path / "p.mp4")
    base = media.thumbnail(plain, tmp_path / "a.jpg", 0.5).read_bytes()
    for text in ("50% off today", r"back\slash and %{pts}"):
        out = media.prepare_video(media_dir / "portrait_silent.mp4", tmp_path / "o.mp4", overlay_text=text)
        assert media.thumbnail(out, tmp_path / "b.jpg", 0.5).read_bytes() != base, text


def test_cjk_font_selection_prefers_real_fonts_over_unifont():
    paths = ["/u/unifont.otf", "/x/Whatever.ttf", "/w/wqy-zenhei.ttc", "/n/NotoSansCJK-Bold.ttc"]
    assert media.pick_cjk_font(paths) == "/n/NotoSansCJK-Bold.ttc"
    assert media.pick_cjk_font(["/u/unifont.otf", "/w/wqy-zenhei.ttc"]) == "/w/wqy-zenhei.ttc"
    assert media.pick_cjk_font(["/u/unifont.otf"]) == "/u/unifont.otf"       # last resort
    assert media.pick_cjk_font(["/u/unifont.otf", "/x/Plain.ttf"]) == "/x/Plain.ttf"
    assert media.pick_cjk_font([]) is None


def test_prepare_video_rejects_images(media_dir, tmp_path):
    with pytest.raises(media.MediaError):
        media.prepare_video(media_dir / "wide.png", tmp_path / "x.mp4")


# ---------------------------------------------------------------- images
@pytest.mark.parametrize("name,expected", [("tall", (1080, 1350)), ("wide", (1080, 566))])
def test_feed_image_clamps_extreme_aspect(media_dir, tmp_path, name, expected):
    out = media.prepare_feed_image(media_dir / f"{name}.png", tmp_path / f"{name}.jpg")
    p = media.probe(out)
    assert (p.width, p.height) == expected
    assert 0.8 <= p.width / p.height <= 1.91


def test_feed_image_leaves_good_aspect_but_limits_width(media_dir, tmp_path):
    sq = media.probe(media.prepare_feed_image(media_dir / "square.png", tmp_path / "sq.jpg"))
    assert (sq.width, sq.height) == (1000, 1000)
    big = media.probe(media.prepare_feed_image(media_dir / "big.png", tmp_path / "big.jpg"))
    assert (big.width, big.height) == (1440, 1080)


def test_story_image_is_1080x1920_jpeg(media_dir, tmp_path):
    out = media.prepare_story_image(media_dir / "square.png", tmp_path / "st.jpg")
    p = media.probe(out)
    assert (p.width, p.height, p.kind) == (1080, 1920, "image")
    assert streams(out)[0]["codec_name"] == "mjpeg"


def test_story_image_overlay_text(media_dir, tmp_path):
    plain = media.prepare_story_image(media_dir / "square.png", tmp_path / "a.jpg")
    texty = media.prepare_story_image(media_dir / "square.png", tmp_path / "b.jpg", overlay_text="Best day ever: 100%")
    assert plain.read_bytes() != texty.read_bytes()
    assert (media.probe(texty).width, media.probe(texty).height) == (1080, 1920)


def test_vision_jpeg_caps_size_and_never_upscales(media_dir, tmp_path):
    big = media.probe(media.vision_jpeg(media_dir / "big.png", tmp_path / "v.jpg"))
    assert max(big.width, big.height) == 1568
    tiny = media.probe(media.vision_jpeg(media_dir / "tiny.png", tmp_path / "t.jpg"))
    assert (tiny.width, tiny.height) == (200, 100)


def test_plain_jpeg_and_bad_input(media_dir, tmp_path):
    p = media.probe(media.prepare_plain_jpeg(media_dir / "big.png", tmp_path / "p.jpg", max_edge=2000))
    assert (p.width, p.height) == (2000, 1500)
    with pytest.raises(media.MediaError):
        media.prepare_story_image(media_dir / "notes.txt", tmp_path / "bad.jpg")


def test_extract_frames(media_dir, tmp_path):
    frames = media.extract_frames(media_dir / "landscape.mp4", tmp_path / "fr", count=3)
    assert len(frames) == 3 and all(f.stat().st_size > 0 for f in frames)


# ---------------------------------------------------------------- text wrapping
def test_wrap_text_latin_and_cjk():
    assert media.wrap_text("short") == "short"
    wrapped = media.wrap_text("one two three four five six seven eight nine ten", max_cells=12)
    assert all(len(line) <= 12 for line in wrapped.splitlines()) and "\n" in wrapped
    cjk = media.wrap_text("一二三四五六七八九十", max_cells=6)      # 2 cells per char -> 3 chars per line
    assert cjk.splitlines() == ["一二三", "四五六", "七八九", "十"]
    assert media.wrap_text("") == ""


# ---------------------------------------------------------------- hosting
def test_media_host_publish_resolve_cleanup(tmp_path):
    f = tmp_path / "My Reel (final)!.mp4"
    f.write_bytes(b"data")
    host = MediaHost(tmp_path / "public", "https://agent.example.com/", ttl_days=1)
    url = host.publish(f)
    assert url.startswith("https://agent.example.com/m/") and url.endswith("/My-Reel-final.mp4")
    _, _, token, name = url.rsplit("/", 3)
    assert host.resolve(token, name).read_bytes() == b"data"
    assert host.publish(f) != url                         # fresh unguessable token each time

    # traversal / junk are refused
    assert host.resolve("..", "x") is None
    assert host.resolve(token, "../../etc/passwd") is None
    assert host.resolve(token, ".hidden") is None
    assert host.resolve("short", name) is None
    assert host.resolve(token, "missing.mp4") is None

    old = time.time() - 3 * 86400
    os.utime(host.public_dir / token, (old, old))
    assert host.cleanup() == 1
    assert host.resolve(token, name) is None
    with pytest.raises(FileNotFoundError):
        host.publish(tmp_path / "ghost.mp4")
