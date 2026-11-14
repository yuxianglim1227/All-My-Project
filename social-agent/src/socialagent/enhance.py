"""Minimal, natural-looking photo/video enhancement (Phase 2, "enhance" edit level).

What it does: measures the picture's tone with ffmpeg's ``signalstats`` and nudges exposure, shadows, highlights,
contrast, colour vibrance and clarity *toward a target*, each with a hard cap, so an already-good photo barely changes
and a flat/dark one gets a modest lift. A "look" (natural / vivid / bright / moody) sets the target and the caps.

What it deliberately does NOT do: no face or skin retouching, no reshaping, no cropping, no generative changes.
People look like themselves; only light and colour are polished. Tuned numerically on synthetic images, not
aesthetically on real photos: judge the preview, switch looks, or pick Original.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import media
from .media import MediaError

ORIGINAL = "original"


@dataclass(frozen=True)
class Look:
    key: str
    label: str
    emoji: str
    description: str
    target: float  # mean luma (0..1) that exposure nudges toward
    exposure_max: float
    shadow_max: float
    highlight_max: float
    contrast_bias: float  # always-on S-curve amount
    contrast_max: float  # extra contrast added when the picture is flat
    vibrance: float
    sharpen: float


LOOKS: dict[str, Look] = {look.key: look for look in (
    Look("natural", "Natural", "✨", "Gentle, true-to-life polish", 0.47, 0.08, 0.06, 0.05, 0.015, 0.04, 0.20, 0.25),
    Look("vivid", "Vivid", "🌈", "Punchier colour and contrast", 0.48, 0.08, 0.06, 0.05, 0.035, 0.06, 0.45, 0.35),
    Look("bright", "Bright", "☀️", "Light and airy", 0.58, 0.12, 0.08, 0.04, 0.0, 0.02, 0.15, 0.20),
    Look("moody", "Moody", "🌙", "Deeper tones, cinematic mood", 0.38, 0.07, 0.02, 0.06, 0.05, 0.06, 0.05, 0.25),
)}
LOOK_KEYS = [*LOOKS, ORIGINAL]


def is_look(key: str) -> bool:
    return key in LOOKS or key == ORIGINAL


def label_of(key: str) -> str:
    return "Original" if key == ORIGINAL else LOOKS[key].label


@dataclass(frozen=True)
class Stats:
    ylow: float  # 10th-percentile luma (0..1): how deep the shadows are
    yavg: float
    yhigh: float  # 90th-percentile luma: how bright the highlights are
    ymax: float
    satavg: float


@dataclass(frozen=True)
class Adjust:
    exposure: float  # + brightens midtones
    shadows: float  # + lifts shadows
    highlights: float  # + pulls highlights down
    contrast: float
    vibrance: float
    sharpen: float
    denoise: bool = False


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Measure -> plan -> render
# ---------------------------------------------------------------------------
_STAT = re.compile(r"lavfi\.signalstats\.(YLOW|YAVG|YHIGH|YMAX|SATAVG)=([0-9.]+)")


def measure(path: str | Path, *, samples: int = 5) -> Stats:
    """Tone statistics of a photo, or the average over a few frames of a video."""
    item = media.probe(path)
    sample = f"fps={samples}/{max(item.duration, 0.5):.3f}," if item.kind == "video" else ""
    proc = media._run([media._FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf",
                       f"{sample}scale=320:-2:in_range=auto:out_range=full,format=yuv444p,signalstats,metadata=print:file=-",
                       "-f", "null", "-"])
    acc: dict[str, list[float]] = {}
    for key, val in _STAT.findall((proc.stdout or "") + (proc.stderr or "")):
        acc.setdefault(key, []).append(float(val) / 255.0)
    if len(acc) < 5:
        raise MediaError(f"Could not measure {Path(path).name} for enhancement")
    mean = {k: sum(v) / len(v) for k, v in acc.items()}
    return Stats(mean["YLOW"], mean["YAVG"], mean["YHIGH"], mean["YMAX"], mean["SATAVG"])


def plan(stats: Stats, look: Look, strength: float = 1.0) -> Adjust:
    """How much to change, bounded by the look's caps. strength 0 = nothing, 1 = normal, up to 1.5."""
    s = _clamp(strength, 0.0, 1.5)
    exposure = _clamp((look.target - stats.yavg) * 0.6, -look.exposure_max, look.exposure_max) * s
    shadows = _clamp((0.12 - stats.ylow) * 0.8, 0.0, look.shadow_max) * s          # crushed blacks -> lift
    highlights = _clamp((stats.yhigh - 0.90) * 0.8, 0.0, look.highlight_max) * s   # near-clipping highlights -> pull down
    flat = _clamp((0.60 - (stats.yhigh - stats.ylow)) * 0.5, 0.0, look.contrast_max)
    contrast = (look.contrast_bias + flat) * s
    room = _clamp(1.0 - stats.satavg / 0.30, 0.3, 1.0)                              # already colourful -> less vibrance
    return Adjust(exposure, shadows, highlights, contrast, look.vibrance * room * s, look.sharpen * s,
                  denoise=stats.yavg < 0.28 and s > 0)


def curve_points(a: Adjust) -> list[tuple[float, float]]:
    """The 5 control points of the tone curve; always strictly increasing and inside 0..1."""
    raw = [0.0, 0.25 + a.shadows - a.contrast * 0.5 + a.exposure * 0.9, 0.5 + a.exposure,
           0.75 - a.highlights + a.contrast * 0.5 + a.exposure * 0.7, 1.0]
    xs, ys = [0.0, 0.25, 0.5, 0.75, 1.0], [0.0]
    for i in range(1, 4):
        ys.append(_clamp(raw[i], ys[-1] + 0.02, 0.98 - 0.02 * (3 - i)))
    ys.append(1.0)
    return list(zip(xs, ys))


def _pchip(pts: list[tuple[float, float]], x: float) -> float:
    """Monotone cubic (Fritsch-Carlson) through the control points: smooth, never overshoots."""
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    n = len(xs)
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    d = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]
    m = [d[0]] + [(d[i - 1] + d[i]) / 2 if d[i - 1] * d[i] > 0 else 0.0 for i in range(1, n - 1)] + [d[-1]]
    for i in range(n - 1):
        if d[i] == 0:
            m[i] = m[i + 1] = 0.0
            continue
        al, be = m[i] / d[i], m[i + 1] / d[i]
        if al * al + be * be > 9:
            t = 3 / (al * al + be * be) ** 0.5
            m[i], m[i + 1] = t * al * d[i], t * be * d[i]
    i = next((k for k in range(n - 1) if xs[k] <= x <= xs[k + 1]), n - 2)
    t = (x - xs[i]) / h[i]
    return ((2 * t**3 - 3 * t**2 + 1) * ys[i] + (t**3 - 2 * t**2 + t) * h[i] * m[i]
            + (-2 * t**3 + 3 * t**2) * ys[i + 1] + (t**3 - t**2) * h[i] * m[i + 1])


def luma_curve(a: Adjust, n: int = 17) -> list[tuple[float, float]]:
    """The tone curve sampled at n points (smooth, monotonic). Linear between samples is visually identical."""
    pts = curve_points(a)
    out, prev = [], 0.0
    for i in range(n):
        x = i / (n - 1)
        y = _clamp(_pchip(pts, x), prev, 1.0)
        out.append((x, y))
        prev = y
    out[0], out[-1] = (0.0, 0.0), (1.0, 1.0)
    return out


def _is_identity(a: Adjust) -> bool:
    return max(abs(a.exposure), a.shadows, a.highlights, abs(a.contrast)) < 0.002


def luma_expr(a: Adjust, full_range: bool) -> str:
    """lutyuv expression for Y: piecewise-linear through the sampled curve, in the signal's own legal range."""
    lo, hi = (0, 255) if full_range else (16, 235)
    pts = luma_curve(a)
    dx = 1 / (len(pts) - 1)
    x = f"(val-{lo})/{hi - lo}"
    terms = "+".join(f"{(pts[i + 1][1] - pts[i][1]) / dx:.4f}*clip({x}-{pts[i][0]:.4f},0,{dx:.4f})" for i in range(len(pts) - 1))
    return f"clip({lo}+{hi - lo}*({terms}),{lo},{hi})"


def chroma_expr(vibrance: float, full_range: bool) -> str:
    """Boost dull colours more than strong ones (vibrance), by scaling U/V around neutral. No RGB round trip."""
    lo, hi = (0, 255) if full_range else (16, 240)
    return f"clip(128+(val-128)*(1+{vibrance:.3f}*(1-abs(val-128)/112)),{lo},{hi})"


def filter_chain(a: Adjust, *, video: bool, full_range: bool | None = None) -> str:
    """Everything stays in YUV (no YUV<->RGB conversion): an identity adjustment is an exact identity.

    Photos are processed as full-range (JPEG) and videos as limited-range, matching what the render commands feed in.
    """
    full = (not video) if full_range is None else full_range
    parts = []
    if video and a.denoise:
        parts.append("hqdn3d=1.2:1.2:4:4")  # light touch for low-light footage only
    lut = []
    if not _is_identity(a):
        lut.append(f"y='{luma_expr(a, full)}'")
    if a.vibrance > 0.005:
        e = chroma_expr(a.vibrance, full)
        lut += [f"u='{e}'", f"v='{e}'"]
    if lut:
        parts.append("lutyuv=" + ":".join(lut))
    if a.sharpen > 0.01:
        parts.append(f"unsharp=5:5:{a.sharpen:.3f}:5:5:0.0")
    return ",".join(parts)


def enhance_image(src: str | Path, dst: str | Path, look_key: str, *, strength: float = 1.0, max_edge: int = 4096) -> Path:
    look = _look(look_key)
    chain = filter_chain(plan(measure(src), look, strength), video=False)
    vf = (f"scale='min({max_edge},iw)':'min({max_edge},ih)':force_original_aspect_ratio=decrease:in_range=auto:out_range=full,"
          f"format=yuvj444p" + (f",{chain}" if chain else ""))
    return media._image_to_jpeg(Path(src), Path(dst), vf)


def enhance_video(src: str | Path, dst: str | Path, look_key: str, *, strength: float = 1.0, max_edge: int = 1920) -> Path:
    look = _look(look_key)
    item = media.probe(src)
    chain = filter_chain(plan(measure(src), look, strength), video=True)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    media._run([media._FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(Path(src).resolve()),
                "-vf", f"scale='min({max_edge},iw)':'min({max_edge},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2:"
                       f"in_range=auto:out_range=tv,format=yuv420p" + (f",{chain}" if chain else "") + ",format=yuv420p",
                "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-r", f"{_fps(item)}",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst.resolve())])
    return dst


def _fps(item: media.MediaItem) -> str:
    return "30"  # platforms re-encode to <= 30 fps anyway; keeps encode time predictable


def enhance(src: str | Path, dst: str | Path, look_key: str, *, strength: float = 1.0) -> Path:
    """Photo or video, by what the file is."""
    fn = enhance_video if media.probe(src).kind == "video" else enhance_image
    return fn(src, dst, look_key, strength=strength)


def _look(key: str) -> Look:
    if key not in LOOKS:
        raise ValueError(f"Unknown look {key!r} (use one of {', '.join(LOOKS)}; 'original' means no enhancement)")
    return LOOKS[key]


# ---------------------------------------------------------------------------
# Previews (small, fast to send) and before/after
# ---------------------------------------------------------------------------
def preview_image(src: str | Path, dst: str | Path, max_edge: int = 1280) -> Path:
    return media.vision_jpeg(src, dst, max_edge=max_edge)


def preview_video(src: str | Path, dst: str | Path, *, max_seconds: int = 60) -> Path:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    media._run([media._FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(Path(src).resolve()), "-t", str(max_seconds),
                "-vf", "scale='if(gt(iw,ih),-2,min(720,iw))':'if(gt(iw,ih),min(720,ih),-2)',format=yuv420p",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "29", "-maxrate", "1600k", "-bufsize", "3200k",
                "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(dst.resolve())])
    return dst


def compare_image(original: str | Path, enhanced: str | Path, dst: str | Path, *, height: int = 720) -> Path:
    """Before | After side by side (one frame from each if they are videos)."""
    def seek(p: str | Path) -> list[str]:
        return ["-ss", "0.5"] if media.probe(p).kind == "video" else []

    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    media._run([media._FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *seek(original), "-i", str(Path(original).resolve()),
                *seek(enhanced), "-i", str(Path(enhanced).resolve()), "-filter_complex",
                f"[0:v]scale=-2:{height}[a];[1:v]scale=-2:{height}[b];[a][b]hstack,format=yuvj420p", "-frames:v", "1",
                "-q:v", "3", str(dst.resolve())])
    return dst
