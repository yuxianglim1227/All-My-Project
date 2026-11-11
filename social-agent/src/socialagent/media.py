"""ffmpeg-based media inspection and editing (headless; no CapCut required).

Everything shells out to ffmpeg/ffprobe with argument lists (never a shell string), so file names
cannot inject commands.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from .models import MediaItem

log = logging.getLogger(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}


def _windows_fonts_dir() -> str:
    return os.path.join(os.environ.get("WINDIR") or os.environ.get("SystemRoot") or "C:/Windows", "Fonts")


def latin_font_candidates() -> list[str]:
    win = _windows_fonts_dir()
    return [os.path.join(win, n) for n in ("arialbd.ttf", "segoeuib.ttf", "arial.ttf")] + [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/Library/Fonts/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc"]


def cjk_font_candidates() -> list[str]:
    """Chinese-capable fonts by platform (Windows: Microsoft YaHei etc. - may be absent on a bare Server install)."""
    win = _windows_fonts_dir()
    return [os.path.join(win, n) for n in ("msyhbd.ttc", "msyh.ttc", "simhei.ttf", "simsun.ttc", "msjh.ttc")] + [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc"]


class MediaError(RuntimeError):
    """ffmpeg/ffprobe failed or the input is not usable media."""


class MediaToolMissing(MediaError):
    """ffmpeg/ffprobe could not be started at all (not installed / not on the service account's PATH)."""


_FFMPEG = "ffmpeg"
_FFPROBE = "ffprobe"


def configure(ffmpeg: str = "", ffprobe: str = "") -> None:
    """Use specific binaries (e.g. C:\\ffmpeg\\bin\\ffmpeg.exe). Needed when the service account has no PATH.

    If only ``ffmpeg`` is given, ffprobe is looked for next to it.
    """
    global _FFMPEG, _FFPROBE
    _FFMPEG = ffmpeg.strip() or "ffmpeg"
    if ffprobe.strip():
        _FFPROBE = ffprobe.strip()
    elif ffmpeg.strip():
        sibling = Path(ffmpeg.strip()).with_name("ffprobe" + Path(ffmpeg.strip()).suffix)
        _FFPROBE = str(sibling) if sibling.exists() else "ffprobe"
    else:
        _FFPROBE = "ffprobe"


def have_ffmpeg() -> bool:
    return bool(shutil.which(_FFMPEG) and shutil.which(_FFPROBE))


def _run(cmd: list[str], *, cwd: str | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    try:
        # Explicit UTF-8: the Windows default (cp1252) can raise UnicodeDecodeError on non-ASCII ffmpeg output.
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, cwd=cwd)
    except FileNotFoundError as exc:
        raise MediaToolMissing(f"{cmd[0]} is not installed or not reachable (install ffmpeg, or set FFMPEG_BIN to its full path)") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{cmd[0]} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-6:])
        raise MediaError(f"{cmd[0]} failed (exit {proc.returncode}): {tail}")
    return proc


# ---------------------------------------------------------------------------
# Inspect
# ---------------------------------------------------------------------------
def probe(path: str | Path) -> MediaItem:
    path = Path(path)
    if not path.is_file():
        raise MediaError(f"File not found: {path}")
    proc = _run([_FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)])
    info = json.loads(proc.stdout or "{}")
    streams = info.get("streams") or []
    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if vstream is None:
        raise MediaError(f"{path.name} has no video/image stream (is it a supported photo or video?)")
    width, height = int(vstream.get("width") or 0), int(vstream.get("height") or 0)
    rotation = _rotation(vstream)
    if rotation in (90, 270):
        width, height = height, width
    fmt_name = (info.get("format") or {}).get("format_name", "")
    is_image = path.suffix.lower() in IMAGE_EXTS or fmt_name == "image2" or fmt_name.endswith("_pipe")
    duration = 0.0 if is_image else float((info.get("format") or {}).get("duration") or vstream.get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return MediaItem(path=str(path), kind="image" if is_image else "video", width=width, height=height,
                     duration=duration, has_audio=has_audio)


def _rotation(vstream: dict) -> int:
    for sd in vstream.get("side_data_list") or []:
        if "rotation" in sd:
            return int(round(float(sd["rotation"]))) % 360
    tag = (vstream.get("tags") or {}).get("rotate")
    return int(tag) % 360 if tag else 0


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
def _blur_pad(w: int, h: int) -> str:
    """Fit the picture inside w x h and fill the bars with a blurred, zoomed copy of itself."""
    bw, bh = max(w // 10, 2), max(h // 10, 2)
    return (
        "split=2[bg][fg];"
        f"[bg]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=4:2,scale={w}:{h}[b];"
        f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[f];"
        "[b][f]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )


def wrap_text(text: str, max_cells: int = 26) -> str:
    """Hard-wrap for drawtext (which does not wrap). CJK characters count as two cells."""
    lines: list[str] = []
    for para in (text or "").strip().splitlines() or [""]:
        cur, cells = "", 0
        for word in _tokens(para):
            wcells = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in word)
            if cur and cells + wcells > max_cells:
                lines.append(cur.rstrip())
                cur, cells = "", 0
            cur += word
            cells += wcells
        lines.append(cur.rstrip())
    return "\n".join(lines)


def _tokens(s: str) -> list[str]:
    """Split into wrap units: words (with trailing space) for latin text, single characters for CJK."""
    out, buf = [], ""
    for ch in s:
        if unicodedata.east_asian_width(ch) in "WF":
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
        else:
            buf += ch
            if ch == " ":
                out.append(buf)
                buf = ""
    if buf:
        out.append(buf)
    return out


def _has_cjk(text: str) -> bool:
    return any(unicodedata.east_asian_width(c) in "WF" for c in text)


_CJK_PREFERENCE = ("noto", "source han", "sourcehan", "pingfang", "wqy", "heiti", "droid", "simhei", "msyh")


def pick_cjk_font(paths: list[str]) -> str | None:
    """Choose the best font from fontconfig's list of fonts that claim Chinese coverage."""
    paths = [p for p in paths if p]
    for key in _CJK_PREFERENCE:
        hit = next((p for p in paths if key in p.lower()), None)
        if hit:
            return hit
    # Unifont covers the glyphs but looks like a bitmap font: last resort only.
    return next((p for p in paths if "unifont" not in p.lower()), paths[0] if paths else None)


def _fc(*args: str) -> str:
    if not shutil.which(args[0]):
        return ""
    try:
        return subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10).stdout
    except Exception:  # fontconfig missing/odd: callers fall back to static candidates
        return ""


def find_font(text: str = "", configured: str = "") -> str | None:
    """A font file able to draw `text`, or None.

    CJK text needs a font that really has the glyphs; `fc-match` happily returns a Latin-only
    font for ':lang=zh', so we ask `fc-list` which fonts claim Chinese support instead.
    """
    if configured and Path(configured).is_file():
        return configured
    if _has_cjk(text):
        listed = [ln.split(":")[0].strip() for ln in _fc("fc-list", ":lang=zh", "file").splitlines()]
        pick = pick_cjk_font([p for p in listed if Path(p).is_file()])
        if pick:
            return pick
        return next((p for p in cjk_font_candidates() if Path(p).is_file()), None)  # None => skip overlay, never draw tofu boxes
    out = _fc("fc-match", "-f", "%{file}", "sans:bold").strip()
    if out and Path(out).is_file():
        return out
    return next((p for p in latin_font_candidates() if Path(p).is_file()), None)


def _drawtext(tmp: str, overlay_text: str, font_path: str, width: int) -> str:
    """Filter fragment (leading comma) drawing wrapped hook text near the top, or '' if impossible.

    Files are created inside the private `tmp` dir and referenced by relative name (run ffmpeg with
    cwd=tmp), which sidesteps filtergraph path escaping entirely.
    """
    if not overlay_text.strip():
        return ""
    text = wrap_text(overlay_text)
    font = find_font(text, font_path)
    if not font:
        log.warning("No usable font found; skipping on-screen text %r", text[:40])
        return ""
    Path(tmp, "t.txt").write_text(text, encoding="utf-8")
    try:
        Path(tmp, "f.ttf").symlink_to(font)
    except OSError:
        shutil.copyfile(font, Path(tmp, "f.ttf"))
    size = max(int(width * 0.058), 24)
    # expansion=none: otherwise a '%' or '\\' in the text makes drawtext log an error and draw
    # nothing while ffmpeg still exits 0 (a silent failure).
    return (f",drawtext=fontfile=f.ttf:textfile=t.txt:expansion=none:fontsize={size}:fontcolor=white:borderw=4:"
            f"bordercolor=black@0.85:line_spacing=12:x=(w-text_w)/2:y=h*0.12")


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def _image_to_jpeg(src: Path, dst: Path, vf: str, *, complex_graph: bool = False, cwd: str | None = None) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    src, dst = src.resolve(), dst.resolve()
    cmd = [_FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if complex_graph:
        cmd += ["-filter_complex", f"[0:v]{vf},format=yuvj420p[v]", "-map", "[v]"]
    else:
        cmd += ["-vf", f"{vf},format=yuvj420p"]
    cmd += ["-frames:v", "1", "-q:v", "2", str(dst)]
    try:
        _run(cmd, cwd=cwd)
    except MediaToolMissing:
        raise  # not the picture's fault: keep the real cause (no misleading HEIC hint)
    except MediaError as exc:
        raise MediaError(f"Could not read/convert image {src.name} (HEIC? re-send as JPEG/PNG). {exc}") from exc
    return dst


def prepare_feed_image(src: str | Path, dst: str | Path) -> Path:
    """JPEG for an Instagram feed photo/carousel item (aspect clamped to 4:5 .. 1.91:1, max 1440 wide)."""
    item = probe(src)
    ratio = item.width / item.height if item.height else 1.0
    if 0.8 <= ratio <= 1.91:
        return _image_to_jpeg(Path(src), Path(dst), "scale='min(1440,iw)':-2")
    w, h = (1080, 1350) if ratio < 0.8 else (1080, 566)
    return _image_to_jpeg(Path(src), Path(dst), _blur_pad(w, h), complex_graph=True)


def prepare_story_image(src: str | Path, dst: str | Path, *, overlay_text: str = "", font_path: str = "") -> Path:
    """1080x1920 JPEG with a blurred background (Stories are 9:16), optional hook text."""
    with tempfile.TemporaryDirectory(prefix="sa_ov_") as tmp:
        graph = _blur_pad(1080, 1920) + _drawtext(tmp, overlay_text, font_path, 1080)
        return _image_to_jpeg(Path(src), Path(dst), graph, complex_graph=True, cwd=tmp)


def prepare_plain_jpeg(src: str | Path, dst: str | Path, max_edge: int = 2048) -> Path:
    """Original framing, JPEG, long edge limited (TikTok photo posts)."""
    vf = f"scale='min({max_edge},iw)':'min({max_edge},ih)':force_original_aspect_ratio=decrease"
    return _image_to_jpeg(Path(src), Path(dst), vf)


def vision_jpeg(src: str | Path, dst: str | Path, max_edge: int = 1568) -> Path:
    """Small JPEG suitable for sending to Claude (never upscales)."""
    vf = f"scale='min({max_edge},iw)':'min({max_edge},ih)':force_original_aspect_ratio=decrease"
    return _image_to_jpeg(Path(src), Path(dst), vf)


def extract_frames(src: str | Path, out_dir: str | Path, count: int = 4) -> list[Path]:
    """Evenly spaced JPEG frames from a video, for Claude to look at."""
    item = probe(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[Path] = []
    dur = max(item.duration, 0.1)
    for i in range(count):
        t = dur * (i + 0.5) / count
        dst = out_dir / f"{Path(src).stem}_f{i}.jpg"
        _run([_FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(src),
              "-frames:v", "1", "-vf", "scale='min(1024,iw)':-2,format=yuvj420p", "-q:v", "3", str(dst)])
        if dst.is_file():
            frames.append(dst)
    return frames


def thumbnail(src: str | Path, dst: str | Path, at: float = 0.5) -> Path:
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    _run([_FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(src),
          "-frames:v", "1", "-vf", "format=yuvj420p", "-q:v", "2", str(dst)])
    return Path(dst)


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------
def prepare_video(src: str | Path, dst: str | Path, *, trim_start: float = 0.0, trim_end: float = 0.0,
                  max_seconds: float = 90.0, overlay_text: str = "", font_path: str = "",
                  width: int = 1080, height: int = 1920) -> Path:
    """H.264/AAC MP4, 9:16, blurred-background padding, optional hook text, faststart.

    trim_end is an absolute time in the source (0 = until the end). Output is capped at max_seconds.
    """
    item = probe(src)
    if item.kind != "video":
        raise MediaError(f"{Path(src).name} is not a video")
    start = max(0.0, min(trim_start, max(item.duration - 0.5, 0.0)))
    end = trim_end if trim_end > start else item.duration
    length = min(max(end - start, 0.5), max_seconds)

    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    graph = _blur_pad(width, height)

    with tempfile.TemporaryDirectory(prefix="sa_ov_") as tmp:
        graph += _drawtext(tmp, overlay_text, font_path, width)
        cmd = [_FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
        if start > 0:
            cmd += ["-ss", f"{start:.2f}"]
        cmd += ["-i", str(Path(src).resolve()), "-t", f"{length:.2f}",
                "-filter_complex", f"[0:v]{graph},format=yuv420p[v]", "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", "10M", "-bufsize", "20M",
                "-r", "30", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart",
                str(dst.resolve())]
        _run(cmd, cwd=tmp)
    return dst
