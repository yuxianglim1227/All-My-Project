"""Media library: every photo/video you upload (web, phone share, Telegram) lives here once, with folders and tags."""

from __future__ import annotations

import json
import re
import shutil
import threading
from pathlib import Path
from typing import Any

from .. import media
from ..media import MediaError
from ..store import Store, new_id
from ..timeutil import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
  thumb TEXT NOT NULL DEFAULT '', width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
  duration REAL NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT 0, folder TEXT NOT NULL DEFAULT 'All media',
  tags_json TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'upload'
);
"""
THUMB_EDGE = 480
DEFAULT_FOLDER = "All media"
_TAG = re.compile(r"[^\w\- ]+", re.UNICODE)


class AssetError(ValueError):
    """Shown to the owner as-is."""


def clean_tags(tags: Any) -> list[str]:
    if isinstance(tags, str):
        tags = tags.split(",")
    out: list[str] = []
    for t in tags or []:
        t = _TAG.sub("", str(t)).strip()[:40]
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out[:20]


def clean_folder(folder: Any) -> str:
    f = _TAG.sub("", str(folder or "")).strip()[:60]
    return f or DEFAULT_FOLDER


class AssetLibrary:
    def __init__(self, store: Store, root: Path):
        self.store, self.root = store, Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        store._exec(SCHEMA.strip())
        self._lock = threading.Lock()

    # -- add ---------------------------------------------------------------------------
    def add_file(self, src: Path, name: str, *, source: str = "upload", folder: str = DEFAULT_FOLDER,
                 tags: Any = (), move: bool = True) -> dict[str, Any]:
        """Take ownership of `src` (moved unless move=False). Raises AssetError if it is not a photo/video."""
        src = Path(src)
        aid = new_id()
        d = self.root / aid
        d.mkdir(parents=True, exist_ok=False)
        dst = d / ("original" + (src.suffix.lower() or ".bin"))
        try:
            (shutil.move if move else shutil.copyfile)(str(src), str(dst))
            try:
                item = media.probe(dst)
            except MediaError as exc:
                raise AssetError(f"{name}: not a photo or video we can read ({exc}).") from None
            thumb = d / "thumb.jpg"
            try:
                if item.kind == "video":
                    frame = d / "frame.jpg"
                    media.thumbnail(dst, frame, at=min(0.5, max(item.duration / 2, 0.0)))
                    media.vision_jpeg(frame, thumb, max_edge=THUMB_EDGE)
                    frame.unlink(missing_ok=True)
                else:
                    media.vision_jpeg(dst, thumb, max_edge=THUMB_EDGE)
            except MediaError:
                thumb = Path("")  # still usable; the UI shows a placeholder
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        self.store._exec(
            "INSERT INTO assets(id,created_at,name,kind,path,thumb,width,height,duration,size,folder,tags_json,source) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, iso(utcnow()), name[:120] or dst.name, item.kind, str(dst), str(thumb) if str(thumb) not in ("", ".") else "",
             item.width, item.height, item.duration, item.size, clean_folder(folder), json.dumps(clean_tags(tags)), source[:20]))
        return self.get(aid)  # type: ignore[return-value]

    # -- read --------------------------------------------------------------------------
    def get(self, aid: str) -> dict[str, Any] | None:
        rows = self.store._rows("SELECT * FROM assets WHERE id=?", (aid,))
        return _decode(rows[0]) if rows else None

    def list(self, *, folder: str = "", tag: str = "", kind: str = "", q: str = "", limit: int = 60,
             offset: int = 0) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM assets WHERE 1=1", []
        if folder and folder != DEFAULT_FOLDER:
            sql += " AND folder=?"
            args.append(folder)
        if kind in ("image", "video"):
            sql += " AND kind=?"
            args.append(kind)
        if q:
            sql += " AND (name LIKE ? OR note LIKE ?)"
            args += [f"%{q}%", f"%{q}%"]
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?"
        args += [max(1, min(limit, 200)) * (4 if tag else 1), max(0, offset)]
        rows = [_decode(r) for r in self.store._rows(sql, args)]
        if tag:  # tags are a small JSON list; filter in Python
            rows = [r for r in rows if tag.lower() in (t.lower() for t in r["tags"])][:limit]
        return rows

    def folders(self) -> list[dict[str, Any]]:
        rows = self.store._rows("SELECT folder, COUNT(*) AS n FROM assets GROUP BY folder ORDER BY folder")
        total = sum(r["n"] for r in rows)
        return [{"folder": DEFAULT_FOLDER, "count": total}] + [
            {"folder": r["folder"], "count": r["n"]} for r in rows if r["folder"] != DEFAULT_FOLDER]

    # -- change ------------------------------------------------------------------------
    def update(self, aid: str, *, name: str | None = None, folder: str | None = None, tags: Any = None,
               note: str | None = None) -> dict[str, Any]:
        if self.get(aid) is None:
            raise AssetError("That item no longer exists.")
        if name is not None:
            self.store._exec("UPDATE assets SET name=? WHERE id=?", (name.strip()[:120] or "untitled", aid))
        if folder is not None:
            self.store._exec("UPDATE assets SET folder=? WHERE id=?", (clean_folder(folder), aid))
        if tags is not None:
            self.store._exec("UPDATE assets SET tags_json=? WHERE id=?", (json.dumps(clean_tags(tags)), aid))
        if note is not None:
            self.store._exec("UPDATE assets SET note=? WHERE id=?", (note.strip()[:2000], aid))
        return self.get(aid)  # type: ignore[return-value]

    def delete(self, aid: str) -> bool:
        a = self.get(aid)
        if a is None:
            return False
        self.store._exec("DELETE FROM assets WHERE id=?", (aid,))
        folder = (self.root / aid).resolve()
        if folder.parent == self.root.resolve():  # never delete outside the library
            shutil.rmtree(folder, ignore_errors=True)
        return True

    def file_for(self, aid: str, variant: str) -> Path | None:
        """Path of 'original' or 'thumb' for serving, or None."""
        a = self.get(aid)
        if a is None:
            return None
        p = Path(a["path"] if variant == "original" else a["thumb"]) if (variant == "original" or a["thumb"]) else None
        return p if p and p.is_file() else None


def _decode(r: dict[str, Any]) -> dict[str, Any]:
    r = dict(r)
    r["tags"] = json.loads(r.pop("tags_json") or "[]")
    return r


def public_view(a: dict[str, Any]) -> dict[str, Any]:
    """What the browser gets: no server paths."""
    return {k: a[k] for k in ("id", "created_at", "name", "kind", "width", "height", "duration", "size", "folder",
                              "tags", "note", "source")} | {
        "thumb_url": f"/studio/api/assets/{a['id']}/thumb" if a.get("thumb") else "",
        "file_url": f"/studio/api/assets/{a['id']}/file"}
