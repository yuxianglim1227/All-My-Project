"""Shared fixtures: real (tiny) media generated with ffmpeg so the media tests exercise the real tools."""

import shutil
import subprocess
from pathlib import Path

import pytest

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg/ffprobe not installed")


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("media")
    if not shutil.which("ffmpeg"):
        return d
    # videos
    _ff("-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=4", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "landscape.mp4"))
    _ff("-f", "lavfi", "-i", "testsrc=size=720x1280:rate=30:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "portrait_silent.mp4"))
    # images
    for name, size in {"wide": "2000x500", "tall": "500x2000", "square": "1000x1000", "big": "4000x3000",
                       "tiny": "200x100"}.items():
        _ff("-f", "lavfi", "-i", f"testsrc=size={size}:rate=1:duration=1", "-frames:v", "1", str(d / f"{name}.png"))
    _ff("-f", "lavfi", "-i", "testsrc=size=1000x1000:rate=1:duration=1", "-frames:v", "1", "-q:v", "3", str(d / "photo.jpg"))
    (d / "notes.txt").write_text("not media")
    return d
