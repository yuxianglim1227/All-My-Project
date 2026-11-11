"""Runtime settings (environment) and the brand profile (YAML)."""

from __future__ import annotations

import codecs
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from .timeutil import tz

log = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on", "y"}
# A "#" starts a comment at the start of the value or after whitespace ("tok#en" keeps its "#").
_INLINE_COMMENT = re.compile(r"(?:^|\s)#.*$")


def read_text_robust(path: str | Path) -> str:
    """Read a text config file the way Windows editors may have saved it.

    Handles a UTF-8 BOM (Notepad) and UTF-16 (PowerShell's ``>`` / ``Out-File``); anything else must be UTF-8.
    A BOM left in place would silently turn the first key into "\\ufeffDRY_RUN" and it would be ignored.
    """
    raw = Path(path).read_bytes()
    try:
        if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return raw.decode("utf-16")
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not valid UTF-8 - re-save it as UTF-8 (Notepad: Save As > Encoding: UTF-8).") from exc


def load_dotenv(path: str | Path = ".env", environ: dict[str, str] | None = None) -> int:
    """Minimal .env loader (KEY=VALUE, quotes, ``# comments``). Existing environment variables win."""
    env = os.environ if environ is None else environ
    p = Path(path)
    if not p.is_file():
        return 0
    loaded = 0
    for raw in read_text_robust(p).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        stripped = value.strip()
        if stripped[:1] in {'"', "'"} and stripped.count(stripped[0]) >= 2:
            value = stripped[1:stripped.index(stripped[0], 1)]
        else:  # strip comments BEFORE trimming, so "KEY=   # note" (blank value) is not read as "# note"
            value = _INLINE_COMMENT.sub("", value).strip()
        if key and key not in env:
            env[key] = value
            loaded += 1
    return loaded


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in _TRUE


def _int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value.strip())
    except ValueError:
        raise ValueError(f"Expected an integer, got {value!r}") from None


def _str(value: str | None, default: str = "") -> str:
    return default if value is None or value.strip() == "" else value.strip()


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("./data")
    brand_file: Path = Path("./config/brand.yaml")
    timezone: str = "UTC"

    # safety
    dry_run: bool = True
    require_approval: bool = False
    undo_window_seconds: int = 120
    collect_window_seconds: int = 8

    # claude
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"
    claude_fallbacks: bool = True

    # inbound
    telegram_token: str = ""
    owner_telegram_id: int | None = None
    telegram_api_base: str = ""
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    api_token: str = ""
    public_base_url: str = "http://localhost:8080"

    # instagram
    ig_user_id: str = ""
    ig_access_token: str = ""
    ig_graph_base: str = "https://graph.instagram.com"
    ig_api_version: str = ""

    # tiktok
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_access_token: str = ""
    tiktok_refresh_token: str = ""
    tiktok_mode: str = "direct"
    tiktok_privacy: str = "SELF_ONLY"

    # editing
    ffmpeg_bin: str = ""
    ffprobe_bin: str = ""
    max_reel_seconds: int = 90
    font_path: str = ""
    jianying_drafts_dir: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        owner = _str(e.get("OWNER_TELEGRAM_ID"))
        mode = _str(e.get("TIKTOK_MODE"), "direct").lower()
        if mode not in {"direct", "inbox"}:
            raise ValueError("TIKTOK_MODE must be 'direct' or 'inbox'")
        effort = _str(e.get("CLAUDE_EFFORT"), "medium").lower()
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("CLAUDE_EFFORT must be low|medium|high|xhigh|max")
        s = cls(
            data_dir=Path(_str(e.get("DATA_DIR"), "./data")),
            brand_file=Path(_str(e.get("BRAND_FILE"), "./config/brand.yaml")),
            timezone=_str(e.get("TIMEZONE"), "UTC"),
            dry_run=_bool(e.get("DRY_RUN"), True),
            require_approval=_bool(e.get("REQUIRE_APPROVAL"), False),
            undo_window_seconds=_int(e.get("UNDO_WINDOW_SECONDS"), 120),
            collect_window_seconds=_int(e.get("COLLECT_WINDOW_SECONDS"), 8),
            claude_model=_str(e.get("CLAUDE_MODEL"), "claude-opus-5-5"),
            claude_effort=effort,
            claude_fallbacks=_bool(e.get("CLAUDE_FALLBACKS"), True),
            telegram_token=_str(e.get("TELEGRAM_BOT_TOKEN")),
            owner_telegram_id=int(owner) if owner else None,
            telegram_api_base=_str(e.get("TELEGRAM_API_BASE")).rstrip("/"),
            http_host=_str(e.get("HTTP_HOST"), "0.0.0.0"),
            http_port=_int(e.get("HTTP_PORT"), 8080),
            api_token=_str(e.get("API_TOKEN")),
            public_base_url=_str(e.get("PUBLIC_BASE_URL"), "http://localhost:8080").rstrip("/"),
            ig_user_id=_str(e.get("IG_USER_ID")),
            ig_access_token=_str(e.get("IG_ACCESS_TOKEN")),
            ig_graph_base=_str(e.get("IG_GRAPH_BASE"), "https://graph.instagram.com").rstrip("/"),
            ig_api_version=_str(e.get("IG_API_VERSION")),
            tiktok_client_key=_str(e.get("TIKTOK_CLIENT_KEY")),
            tiktok_client_secret=_str(e.get("TIKTOK_CLIENT_SECRET")),
            tiktok_access_token=_str(e.get("TIKTOK_ACCESS_TOKEN")),
            tiktok_refresh_token=_str(e.get("TIKTOK_REFRESH_TOKEN")),
            tiktok_mode=mode,
            tiktok_privacy=_str(e.get("TIKTOK_PRIVACY"), "SELF_ONLY").upper(),
            ffmpeg_bin=_str(e.get("FFMPEG_BIN")),
            ffprobe_bin=_str(e.get("FFPROBE_BIN")),
            max_reel_seconds=_int(e.get("MAX_REEL_SECONDS"), 90),
            font_path=_str(e.get("FONT_PATH")),
            jianying_drafts_dir=_str(e.get("JIANYING_DRAFTS_DIR")),
        )
        tz(s.timezone)  # fail fast on a bad zone name
        return s

    # convenience paths --------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def prepared_dir(self) -> Path:
        return self.data_dir / "prepared"

    @property
    def public_dir(self) -> Path:
        return self.data_dir / "public"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.inbox_dir, self.prepared_dir, self.public_dir):
            p.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Brand profile
# ---------------------------------------------------------------------------
@dataclass
class Pillar:
    name: str
    weight: float = 0.0
    description: str = ""


@dataclass
class HighlightSpec:
    name: str
    description: str = ""


@dataclass
class BrandProfile:
    account_name: str = "My Account"
    niche: str = ""
    audience: str = ""
    region: str = "Global"
    voice: str = ""
    languages: dict[str, list[str]] = field(default_factory=lambda: {"instagram": ["en"], "tiktok": ["en"]})
    pillars: list[Pillar] = field(default_factory=list)
    dos: list[str] = field(default_factory=list)
    donts: list[str] = field(default_factory=list)
    hashtags_always: list[str] = field(default_factory=list)
    hashtags_banned: list[str] = field(default_factory=list)
    default_cta: str = ""
    posting_windows: dict[str, list[str]] = field(default_factory=dict)
    min_gap_minutes: dict[str, int] = field(default_factory=dict)
    max_per_day: dict[str, int] = field(default_factory=dict)
    highlights: list[HighlightSpec] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "BrandProfile":
        d = dict(d or {})
        hashtags = d.get("hashtags") or {}
        return cls(
            account_name=str(d.get("account_name") or "My Account"),
            niche=str(d.get("niche") or ""),
            audience=str(d.get("audience") or ""),
            region=str(d.get("region") or "Global"),
            voice=" ".join(str(d.get("voice") or "").split()),
            languages={k: list(v) for k, v in (d.get("languages") or {}).items()} or {"instagram": ["en"], "tiktok": ["en"]},
            pillars=[Pillar(str(p.get("name")), float(p.get("weight") or 0), str(p.get("description") or ""))
                     for p in (d.get("pillars") or []) if p.get("name")],
            dos=[str(x) for x in d.get("dos") or []],
            donts=[str(x) for x in d.get("donts") or []],
            hashtags_always=[_tag(x) for x in hashtags.get("always") or []],
            hashtags_banned=[_tag(x) for x in hashtags.get("banned") or []],
            default_cta=str(d.get("default_cta") or ""),
            posting_windows={k: [str(t) for t in v] for k, v in (d.get("posting_windows") or {}).items()},
            min_gap_minutes={k: int(v) for k, v in (d.get("min_gap_minutes") or {}).items()},
            max_per_day={k: int(v) for k, v in (d.get("max_per_day") or {}).items()},
            highlights=[HighlightSpec(str(h.get("name")), str(h.get("description") or ""))
                        for h in (d.get("highlights") or []) if h.get("name")],
        )

    @classmethod
    def load(cls, path: Path) -> "BrandProfile":
        path = Path(path)
        if not path.exists():
            log.warning("Brand file %s not found; using defaults", path)
            return cls()
        try:
            data = yaml.safe_load(read_text_robust(path)) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"{path} is not valid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a YAML mapping (key: value pairs).")
        return cls.from_dict(data)

    @property
    def pillar_names(self) -> list[str]:
        return [p.name for p in self.pillars]

    @property
    def highlight_names(self) -> list[str]:
        return [h.name for h in self.highlights]

    def prompt_block(self) -> str:
        """Human-readable brand brief embedded in the planner's system prompt."""
        lines = [
            f"Account: {self.account_name}",
            f"Niche: {self.niche}",
            f"Audience: {self.audience}",
            f"Region: {self.region}",
            f"Voice: {self.voice}",
            "Caption languages: " + "; ".join(f"{k}={'+'.join(v)}" for k, v in self.languages.items()),
            "Content pillars (target mix): "
            + "; ".join(f"{p.name} {p.weight:g}% ({p.description})" for p in self.pillars),
            "Do: " + " | ".join(self.dos),
            "Don't: " + " | ".join(self.donts),
            "Always-use hashtags: " + (" ".join(self.hashtags_always) or "(none)"),
            "Banned hashtags: " + (" ".join(self.hashtags_banned) or "(none)"),
            f"Default CTA: {self.default_cta}",
            "Instagram Highlight collections: "
            + "; ".join(f"{h.name} ({h.description})" for h in self.highlights),
        ]
        return "\n".join(lines)


def _tag(value: Any) -> str:
    s = str(value).strip()
    return s if s.startswith("#") else f"#{s}"
