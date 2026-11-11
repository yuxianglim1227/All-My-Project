"""Windows-specific failure modes, simulated on any OS. (Never run on real Windows in the build environment.)"""

import asyncio
import codecs
import logging
import shutil
import sys
from pathlib import Path

import httpx
import pytest

from conftest import needs_ffmpeg
from helpers import ROOT
from socialagent import media
from socialagent.cli import _setup_file_logging, _setup_logging, main
from socialagent.config import BrandProfile, Settings, load_dotenv, read_text_robust


# ---------------------------------------------------------------- config files saved by Windows editors
ENV_TEXT = "DRY_RUN=false\r\nOWNER_TELEGRAM_ID=42   # me\r\nTIKTOK_MODE=inbox\r\n"


@pytest.mark.parametrize("label,raw", [
    ("plain utf-8 + CRLF", ENV_TEXT.encode("utf-8")),
    ("Notepad UTF-8 BOM", codecs.BOM_UTF8 + ENV_TEXT.encode("utf-8")),
    ("PowerShell UTF-16 LE", codecs.BOM_UTF16_LE + ENV_TEXT.encode("utf-16-le")),
    ("UTF-16 BE", codecs.BOM_UTF16_BE + ENV_TEXT.encode("utf-16-be")),
])
def test_env_file_variants_all_parse_identically(tmp_path, label, raw):
    f = tmp_path / ".env"
    f.write_bytes(raw)
    env: dict[str, str] = {}
    assert load_dotenv(f, env) == 3, label
    assert env == {"DRY_RUN": "false", "OWNER_TELEGRAM_ID": "42", "TIKTOK_MODE": "inbox"}, label    # first key survives the BOM
    s = Settings.from_env(env)
    assert s.dry_run is False and s.owner_telegram_id == 42


def test_bom_would_have_broken_the_first_key_without_the_fix(tmp_path):
    """Documents why read_text_robust exists: a naive utf-8 read keeps the BOM glued to the first key."""
    f = tmp_path / ".env"
    f.write_bytes(codecs.BOM_UTF8 + b"DRY_RUN=false\n")
    assert f.read_text(encoding="utf-8").startswith("\ufeff")
    assert read_text_robust(f) == "DRY_RUN=false\n"


def test_non_utf8_file_gets_an_actionable_error(tmp_path):
    f = tmp_path / "brand.yaml"
    f.write_bytes("niche: caf\xe9\n".encode("cp1252"))                       # 'é' saved as a single cp1252 byte
    with pytest.raises(ValueError, match="re-save it as UTF-8"):
        read_text_robust(f)
    with pytest.raises(ValueError, match="re-save it as UTF-8"):
        BrandProfile.load(f)


def test_brand_yaml_with_bom_crlf_and_chinese(tmp_path):
    src = (ROOT / "config" / "brand.yaml").read_text(encoding="utf-8").replace('account_name: "My Account"', 'account_name: "我的账号"')
    f = tmp_path / "brand.yaml"
    f.write_bytes(codecs.BOM_UTF8 + src.replace("\n", "\r\n").encode("utf-8"))
    b = BrandProfile.load(f)
    assert b.account_name == "我的账号" and b.pillar_names and b.posting_windows["tiktok"] == ["18:00", "21:00"]


def test_broken_yaml_is_a_config_error_not_a_traceback(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "brand.yaml"
    bad.write_text("pillars: [unclosed\n  - nope: : :\n")
    with pytest.raises(ValueError, match="not valid YAML"):
        BrandProfile.load(bad)
    (tmp_path / "list.yaml").write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="mapping"):
        BrandProfile.load(tmp_path / "list.yaml")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BRAND_FILE", str(bad))
    assert main(["--env-file", "/nonexistent.env", "queue"]) == 2
    assert "Configuration error" in capsys.readouterr().err


# ---------------------------------------------------------------- ffmpeg discovery & subprocess decoding
@pytest.fixture()
def restore_media_binaries():
    yield
    media.configure("", "")


def test_run_survives_non_utf8_tool_output(restore_media_binaries):
    """cp1252/UTF-16 noise in ffmpeg's stderr must produce a MediaError, never UnicodeDecodeError."""
    code = "import sys; sys.stderr.buffer.write(b'bad \\xff\\xfe bytes \\xe9'); sys.exit(3)"
    with pytest.raises(media.MediaError, match="exit 3") as e:
        media._run([sys.executable, "-c", code])
    assert "bad" in str(e.value)


@needs_ffmpeg
def test_configure_points_at_explicit_binaries(media_dir, restore_media_binaries, tmp_path):
    real = shutil.which("ffmpeg")
    media.configure(real)                                                       # ffprobe found next to it (or on PATH)
    assert media.have_ffmpeg() and media.probe(media_dir / "photo.jpg").kind == "image"
    media.configure(real, shutil.which("ffprobe"))
    assert media.probe(media_dir / "landscape.mp4").kind == "video"

    media.configure(str(tmp_path / "no" / "ffmpeg.exe"))                       # service account without ffmpeg
    assert media.have_ffmpeg() is False
    with pytest.raises(media.MediaToolMissing, match="FFMPEG_BIN") as e:         # ffmpeg is the broken one (probe uses ffprobe)
        media.prepare_story_image(media_dir / "photo.jpg", tmp_path / "x.jpg")
    assert "HEIC" not in str(e.value)                                             # the real cause, not a misleading hint
    with pytest.raises(media.MediaToolMissing):
        media.thumbnail(media_dir / "landscape.mp4", tmp_path / "t.jpg")
    media.configure(real, str(tmp_path / "no" / "ffprobe.exe"))                  # and ffprobe missing breaks probe
    with pytest.raises(media.MediaToolMissing):
        media.probe(media_dir / "photo.jpg")
    media.configure("", "")
    assert media.have_ffmpeg() and media.probe(media_dir / "photo.jpg").kind == "image"


def test_settings_expose_binary_paths():
    s = Settings.from_env({"FFMPEG_BIN": r"C:\ffmpeg\bin\ffmpeg.exe", "FFPROBE_BIN": r"C:\ffmpeg\bin\ffprobe.exe"})
    assert s.ffmpeg_bin.endswith("ffmpeg.exe") and s.ffprobe_bin.endswith("ffprobe.exe")


# ---------------------------------------------------------------- fonts without fontconfig (Windows)
def test_windows_font_discovery_without_fontconfig(tmp_path, monkeypatch):
    fonts = tmp_path / "Windows" / "Fonts"
    fonts.mkdir(parents=True)
    for name in ("arialbd.ttf", "msyhbd.ttc"):
        (fonts / name).write_bytes(b"x")
    monkeypatch.setenv("WINDIR", str(tmp_path / "Windows"))
    monkeypatch.setattr(media, "_fc", lambda *a: "")                           # no fc-list / fc-match on Windows
    assert Path(media.find_font("Hello")).name == "arialbd.ttf"
    assert Path(media.find_font("你好 hello")).name == "msyhbd.ttc"
    # a Server install without CJK fonts: report "no font" rather than drawing empty boxes
    monkeypatch.setattr(media, "cjk_font_candidates", lambda: [str(tmp_path / "missing.ttc")])
    assert media.find_font("你好") is None
    assert media.find_font("Hello") is not None
    # an explicit FONT_PATH always wins
    custom = tmp_path / "custom.ttf"
    custom.write_bytes(b"x")
    assert media.find_font("你好", str(custom)) == str(custom)


@needs_ffmpeg
def test_overlay_still_renders_when_symlinks_are_not_permitted(media_dir, tmp_path, monkeypatch):
    """Windows without 'create symbolic links' privilege raises OSError (WinError 1314): fall back to copying."""
    def deny(self, *a, **k):
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", deny)
    plain = media.prepare_video(media_dir / "portrait_silent.mp4", tmp_path / "p.mp4")
    texty = media.prepare_video(media_dir / "portrait_silent.mp4", tmp_path / "t.mp4", overlay_text="Hook 100%")
    a = media.thumbnail(plain, tmp_path / "a.jpg", 0.5).read_bytes()
    b = media.thumbnail(texty, tmp_path / "b.jpg", 0.5).read_bytes()
    assert a != b


# ---------------------------------------------------------------- logging
@pytest.fixture()
def clean_logging():
    root = logging.getLogger()
    saved = (root.level, list(root.handlers), {n: logging.getLogger(n).level for n in ("httpx", "httpcore", "telegram")})
    yield
    root.setLevel(saved[0])
    root.handlers[:] = saved[1]
    for n, lvl in saved[2].items():
        logging.getLogger(n).setLevel(lvl)


def test_request_urls_with_tokens_never_reach_the_logs(caplog, clean_logging):
    """httpx logs full URLs at INFO; Telegram/Instagram URLs contain the secret token."""
    _setup_logging(verbose=False)
    caplog.set_level(logging.INFO)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    client.get("https://api.telegram.org/bot123456:SUPERSECRET/getMe")
    client.get("https://graph.instagram.com/me", params={"access_token": "IGSECRET"})
    assert "SUPERSECRET" not in caplog.text and "IGSECRET" not in caplog.text


def test_file_logging_writes_a_rotating_log(tmp_path, clean_logging):
    settings = Settings.from_env({"DATA_DIR": str(tmp_path / "data")})
    path = _setup_file_logging(settings)
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("socialagent.test").info("hello from the service ✓ 你好")
    for h in logging.getLogger().handlers:
        h.flush()
    assert path == tmp_path / "data" / "agent.log"
    assert "hello from the service ✓ 你好" in path.read_text(encoding="utf-8")
    assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in logging.getLogger().handlers)


# ---------------------------------------------------------------- no POSIX signal handlers on Windows
async def test_runtime_starts_even_without_signal_handler_support(tmp_path, monkeypatch):
    import socket
    import aiohttp
    from socialagent.runtime import run_forever

    def unsupported(self, *a, **k):
        raise NotImplementedError

    monkeypatch.setattr(type(asyncio.get_running_loop()), "add_signal_handler", unsupported)
    monkeypatch.setattr("socialagent.trends.TrendResearcher.refresh", lambda self, today=None: [])
    brand = tmp_path / "brand.yaml"
    shutil.copyfile(ROOT / "config" / "brand.yaml", brand)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings = Settings.from_env({"DATA_DIR": str(tmp_path / "d"), "BRAND_FILE": str(brand), "HTTP_HOST": "127.0.0.1",
                                  "HTTP_PORT": str(port), "ANTHROPIC_API_KEY": "unused"})
    task = asyncio.create_task(run_forever(settings))
    try:
        async with aiohttp.ClientSession() as http:
            for _ in range(50):
                try:
                    async with http.get(f"http://127.0.0.1:{port}/healthz") as r:
                        assert r.status == 200
                        break
                except aiohttp.ClientConnectorError:
                    await asyncio.sleep(0.1)
            else:
                pytest.fail("server never came up")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
