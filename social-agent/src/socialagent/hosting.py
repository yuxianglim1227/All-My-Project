"""Public URLs for prepared media.

Instagram downloads media from a public URL (it cannot be uploaded to the Graph API directly in the
flow used here), so prepared files are copied under an unguessable token path and served by the
built-in web server at PUBLIC_BASE_URL/m/<token>/<name>.
"""

from __future__ import annotations

import re
import secrets
import shutil
import time
from pathlib import Path


class MediaHost:
    def __init__(self, public_dir: Path, base_url: str, ttl_days: float = 3.0):
        self.public_dir = Path(public_dir)
        self.base_url = base_url.rstrip("/")
        self.ttl_seconds = ttl_days * 86400
        self.public_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_name(name: str) -> str:
        stem, dot, ext = name.rpartition(".")
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem or name).strip("-")[:40] or "media"
        ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:5].lower() if dot else ""
        return f"{stem}.{ext}" if ext else stem

    def publish(self, path: str | Path) -> str:
        """Copy the file under a random token and return its public URL."""
        src = Path(path)
        if not src.is_file():
            raise FileNotFoundError(src)
        token = secrets.token_urlsafe(18)
        name = self._safe_name(src.name)
        dest = self.public_dir / token
        dest.mkdir(parents=True)
        shutil.copyfile(src, dest / name)
        return f"{self.base_url}/m/{token}/{name}"

    def resolve(self, token: str, name: str) -> Path | None:
        """Map a URL back to a file, refusing anything that escapes the public dir."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", token) or "/" in name or "\\" in name or name.startswith("."):
            return None
        p = (self.public_dir / token / name).resolve()
        try:
            p.relative_to(self.public_dir.resolve())
        except ValueError:
            return None
        return p if p.is_file() else None

    def cleanup(self, now: float | None = None) -> int:
        """Delete token folders older than the TTL. Returns how many were removed."""
        now = time.time() if now is None else now
        removed = 0
        for d in self.public_dir.iterdir():
            if d.is_dir() and now - d.stat().st_mtime > self.ttl_seconds:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        return removed
