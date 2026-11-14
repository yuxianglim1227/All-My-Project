import subprocess

import pytest

from conftest import needs_ffmpeg
from socialagent import enhance, media
from socialagent.enhance import LOOKS, Adjust, Stats, curve_points, filter_chain, luma_curve, luma_expr, plan

pytestmark = needs_ffmpeg


def _ff(*args):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


@pytest.fixture(scope="module")
def pics(tmp_path_factory):
    d = tmp_path_factory.mktemp("enh")
    for name, eq in {"dark": "eq=brightness=-0.22:contrast=0.55:saturation=0.6", "blown": "eq=brightness=0.28:contrast=1.5:saturation=0.9",
                     "ok": "eq=saturation=0.8"}.items():
        _ff("-f", "lavfi", "-i", "testsrc2=size=1200x800:rate=1:duration=1", "-vf", eq, "-frames:v", "1", "-q:v", "2", str(d / f"{name}.jpg"))
    _ff("-f", "lavfi", "-i", "color=gray:size=1200x800,format=gray", "-vf", "geq=lum='150+95*X/W':cb=128:cr=128,format=yuvj420p", "-frames:v", "1",
        "-q:v", "2", str(d / "highs.jpg"))                                      # a ramp to ~96% white: bright but not clipped
    _ff("-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30:duration=2", "-f", "lavfi", "-i", "sine=frequency=300:duration=2",
        "-vf", "eq=brightness=-0.2:contrast=0.6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "dark.mp4"))
    return d


# ---------------------------------------------------------------- measure / plan (pure logic)
def test_measure_reports_the_expected_tone(pics):
    dark, blown = enhance.measure(pics / "dark.jpg"), enhance.measure(pics / "blown.jpg")
    assert 0.2 < dark.yavg < 0.3 and dark.ylow < 0.08 and blown.yavg > 0.65 and blown.yhigh > 0.95
    v = enhance.measure(pics / "dark.mp4")                                      # video: averaged over sampled frames
    assert 0.15 < v.yavg < 0.45


def test_plan_responds_to_what_the_picture_needs_and_never_exceeds_the_caps():
    nat = LOOKS["natural"]
    dark = plan(Stats(0.02, 0.22, 0.45, 0.5, 0.1), nat)
    assert dark.exposure > 0 and dark.shadows > 0 and dark.highlights == 0 and dark.denoise
    blown = plan(Stats(0.2, 0.75, 1.0, 1.0, 0.1), nat)
    assert blown.exposure < 0 and blown.highlights > 0 and blown.shadows == 0 and not blown.denoise
    flat = plan(Stats(0.3, 0.5, 0.65, 0.7, 0.1), nat)
    assert flat.contrast > plan(Stats(0.05, 0.5, 0.95, 1.0, 0.1), nat).contrast       # flat pictures get more contrast
    good = plan(Stats(0.1, 0.47, 0.88, 0.95, 0.1), nat)
    assert abs(good.exposure) < 0.01 and good.shadows < 0.02 and good.highlights == 0  # already fine: barely touched
    for stats in (Stats(0, 0, 0, 0, 0), Stats(1, 1, 1, 1, 1), Stats(0.5, 0.5, 0.5, 0.5, 0.5)):
        for look in LOOKS.values():
            a = plan(stats, look, strength=1.5)
            assert abs(a.exposure) <= look.exposure_max * 1.5 + 1e-9 and a.shadows <= look.shadow_max * 1.5 + 1e-9
            assert a.highlights <= look.highlight_max * 1.5 + 1e-9


def test_looks_differ_in_the_direction_you_would_expect():
    s = Stats(0.1, 0.45, 0.85, 0.9, 0.12)
    exp = {k: plan(s, look).exposure for k, look in LOOKS.items()}
    assert exp["bright"] > exp["natural"] > exp["moody"]
    assert plan(s, LOOKS["vivid"]).vibrance > plan(s, LOOKS["natural"]).vibrance > plan(s, LOOKS["moody"]).vibrance
    assert plan(s, LOOKS["natural"], strength=0).exposure == 0 and plan(s, LOOKS["natural"], strength=0).sharpen == 0
    assert plan(s, LOOKS["natural"], strength=9).sharpen == pytest.approx(plan(s, LOOKS["natural"], strength=1.5).sharpen)   # strength is capped


def test_saturated_pictures_get_less_vibrance():
    nat = LOOKS["natural"]
    assert plan(Stats(0.1, 0.5, 0.9, 1, 0.05), nat).vibrance > plan(Stats(0.1, 0.5, 0.9, 1, 0.40), nat).vibrance > 0


@pytest.mark.parametrize("a", [Adjust(0.5, 0.5, 0.5, 0.5, 0, 0), Adjust(-0.5, 0, 0.5, 0.5, 0, 0), Adjust(0, 0, 0, 0, 0, 0), Adjust(0.9, 0.9, 0, 0.9, 0, 0)])
def test_tone_curve_is_always_monotonic_smooth_and_in_range(a):
    pts = curve_points(a)
    assert pts[0] == (0.0, 0.0) and pts[-1] == (1.0, 1.0)
    ys = [y for _, y in pts]
    assert all(0 <= y <= 1 for y in ys) and all(b > a_ for a_, b in zip(ys, ys[1:]))
    sampled = luma_curve(a)
    sy = [y for _, y in sampled]
    assert len(sampled) == 17 and sampled[0] == (0.0, 0.0) and sampled[-1] == (1.0, 1.0)
    assert all(0 <= y <= 1 for y in sy) and all(b >= a_ for a_, b in zip(sy, sy[1:]))          # never inverts tones


def test_identity_adjustment_yields_the_identity_curve_and_no_filters():
    ident = luma_curve(Adjust(0, 0, 0, 0, 0, 0))
    assert all(abs(x - y) < 0.02 for x, y in ident)                                          # y ~ x (control points are slightly constrained)
    assert filter_chain(Adjust(0, 0, 0, 0, 0, 0), video=False) == ""                          # nothing to do: no filter at all


def test_filter_chain_stays_in_yuv_and_respects_the_signal_range():
    a = Adjust(0.02, 0.03, 0.01, 0.03, 0.2, 0.25, denoise=True)
    chain = filter_chain(a, video=True)
    assert chain.startswith("hqdn3d") and "lutyuv=y='clip(16+219*(" in chain and "u='clip(128+(val-128)*(1+0.200" in chain
    assert chain.endswith("unsharp=5:5:0.250:5:5:0.0")
    for banned in ("curves", "vibrance", "gbrp", "rgb"):
        assert banned not in chain                                                         # no RGB round trip: that is what shifted colours
    assert "hqdn3d" not in filter_chain(a, video=False) and "clip(0+255*(" in filter_chain(a, video=False)   # photos: full range
    assert "clip(16+219*(" in luma_expr(a, full_range=False) and "clip(0+255*(" in luma_expr(a, full_range=True)


# ---------------------------------------------------------------- real renders
def test_dark_photo_is_lifted_without_changing_framing_or_clipping(pics, tmp_path):
    before = enhance.measure(pics / "dark.jpg")
    out = enhance.enhance_image(pics / "dark.jpg", tmp_path / "o.jpg", "natural")
    p, after = media.probe(out), enhance.measure(out)
    assert (p.width, p.height, p.vcodec) == (1200, 800, "mjpeg")
    assert after.yavg > before.yavg + 0.04 and after.ylow > before.ylow and after.ymax < 0.98


def test_bright_highlights_are_compressed_but_white_stays_white(pics, tmp_path):
    before = enhance.measure(pics / "highs.jpg")
    after = enhance.measure(enhance.enhance_image(pics / "highs.jpg", tmp_path / "o.jpg", "natural"))
    assert before.yhigh > 0.9 and after.yhigh < before.yhigh - 0.005                         # near-white tones come down a little ...
    assert after.ymax > before.ymax - 0.02                                                   # ... without greying the brightest pixels


def test_fully_clipped_whites_are_left_alone(pics, tmp_path):
    after = enhance.measure(enhance.enhance_image(pics / "blown.jpg", tmp_path / "o.jpg", "natural"))
    assert after.ymax > 0.98                                                                  # clipped data cannot be recovered; do not grey it


def test_vivid_adds_colour_to_a_dull_picture(pics, tmp_path):
    before = enhance.measure(pics / "dark.jpg")
    nat = enhance.measure(enhance.enhance_image(pics / "dark.jpg", tmp_path / "n.jpg", "natural"))
    viv = enhance.measure(enhance.enhance_image(pics / "dark.jpg", tmp_path / "v.jpg", "vivid"))
    assert viv.satavg > nat.satavg > before.satavg - 0.005


def test_a_decent_photo_is_changed_only_a_little(pics, tmp_path):
    before = enhance.measure(pics / "ok.jpg")
    after = enhance.measure(enhance.enhance_image(pics / "ok.jpg", tmp_path / "o.jpg", "natural"))
    assert abs(after.yavg - before.yavg) < 0.04 and abs(after.yhigh - before.yhigh) < 0.05


def test_looks_produce_the_expected_brightness_order(pics, tmp_path):
    avg = {k: enhance.measure(enhance.enhance_image(pics / "ok.jpg", tmp_path / f"{k}.jpg", k)).yavg for k in ("bright", "natural", "moody")}
    assert avg["bright"] > avg["natural"] > avg["moody"]


def test_strength_zero_is_an_exact_noop(pics, tmp_path):
    base = enhance.measure(pics / "dark.jpg")
    zero = enhance.measure(enhance.enhance_image(pics / "dark.jpg", tmp_path / "z.jpg", "natural", strength=0))
    assert abs(zero.yavg - base.yavg) < 0.003 and abs(zero.ylow - base.ylow) < 0.003         # no hidden colour shift


def test_repeated_enhancement_converges_to_the_target_without_overshooting(pics, tmp_path):
    """Each pass is capped, so a very dark photo needs a few; but it must settle near the look's target, never run away."""
    cur, avgs = pics / "dark.jpg", []
    for i in range(6):
        cur = enhance.enhance_image(cur, tmp_path / f"p{i}.jpg", "natural")
        avgs.append(enhance.measure(cur).yavg)
    target = LOOKS["natural"].target
    assert all(b >= a - 0.005 for a, b in zip(avgs, avgs[1:]))                                # brightens monotonically ...
    assert avgs[-1] <= target + 0.04 and abs(avgs[-1] - avgs[-2]) < 0.02                      # ... and settles near the target


def test_png_in_enhanced_jpeg_out_and_big_images_are_capped(tmp_path):
    _ff("-f", "lavfi", "-i", "testsrc2=size=5000x3000:rate=1:duration=1", "-frames:v", "1", str(tmp_path / "big.png"))
    p = media.probe(enhance.enhance_image(tmp_path / "big.png", tmp_path / "o.jpg", "vivid"))
    assert (p.width, p.vcodec) == (4096, "mjpeg")


def test_video_enhancement_keeps_audio_duration_and_aspect(pics, tmp_path):
    before = enhance.measure(pics / "dark.mp4")
    out = enhance.enhance_video(pics / "dark.mp4", tmp_path / "o.mp4", "natural")
    p = media.probe(out)
    assert (p.vcodec, p.acodec, p.width, p.height) == ("h264", "aac", 720, 1280) and abs(p.duration - 2.0) < 0.4
    assert enhance.measure(out).yavg > before.yavg + 0.02
    silent = tmp_path / "silent.mp4"
    _ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent))
    assert media.probe(enhance.enhance_video(silent, tmp_path / "s.mp4", "bright")).acodec == ""   # no audio in, none out


def test_enhance_dispatches_by_file_kind_and_rejects_unknown_looks(pics, tmp_path):
    assert media.probe(enhance.enhance(pics / "dark.jpg", tmp_path / "a.jpg", "natural")).kind == "image"
    assert media.probe(enhance.enhance(pics / "dark.mp4", tmp_path / "a.mp4", "natural")).kind == "video"
    with pytest.raises(ValueError, match="Unknown look"):
        enhance.enhance_image(pics / "dark.jpg", tmp_path / "x.jpg", "sepia")
    with pytest.raises(ValueError):
        enhance.enhance_image(pics / "dark.jpg", tmp_path / "x.jpg", "original")  # 'original' means "do not enhance": callers skip
    assert enhance.is_look("original") and enhance.is_look("moody") and not enhance.is_look("sepia")
    assert enhance.label_of("original") == "Original" and enhance.label_of("vivid") == "Vivid"


def test_previews_are_small_and_compare_is_side_by_side(pics, tmp_path):
    big = tmp_path / "big.png"
    _ff("-f", "lavfi", "-i", "testsrc2=size=3000x2000:rate=1:duration=1", "-frames:v", "1", str(big))
    assert max(media.probe(enhance.preview_image(big, tmp_path / "p.jpg")).width, 0) == 1280
    pv = media.probe(enhance.preview_video(pics / "dark.mp4", tmp_path / "pv.mp4"))
    assert (pv.vcodec, pv.width, pv.height) == ("h264", 720, 1280) and pv.size < 2_000_000
    cmp = media.probe(enhance.compare_image(pics / "dark.jpg", enhance.enhance_image(pics / "dark.jpg", tmp_path / "e.jpg", "natural"), tmp_path / "c.jpg"))
    assert (cmp.width, cmp.height) == (2160, 720)                               # 1080x720 + 1080x720
    cv = media.probe(enhance.compare_image(pics / "dark.mp4", enhance.enhance_video(pics / "dark.mp4", tmp_path / "e.mp4", "vivid"), tmp_path / "cv.jpg"))
    assert cv.kind == "image" and cv.width > cv.height                           # a still frame from each video
