"""SQLite persistence: submissions, posts, trends, inspiration, highlights, key/value state."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .timeutil import iso, parse_iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source TEXT NOT NULL, instruction TEXT NOT NULL,
  media_json TEXT NOT NULL, status TEXT NOT NULL, plan_json TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY, submission_id TEXT NOT NULL, platform TEXT NOT NULL, kind TEXT NOT NULL,
  caption TEXT NOT NULL, hashtags_json TEXT NOT NULL, media_json TEXT NOT NULL,
  publish_at TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  remote_id TEXT, permalink TEXT, error TEXT, note TEXT, pillar TEXT, highlight TEXT, why TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, method TEXT NOT NULL DEFAULT 'api'
);
CREATE INDEX IF NOT EXISTS idx_posts_due ON posts(status, publish_at);
CREATE TABLE IF NOT EXISTS drafts (
  id TEXT PRIMARY KEY, submission_id TEXT NOT NULL, state TEXT NOT NULL, tone TEXT NOT NULL, lang TEXT NOT NULL,
  instruction TEXT NOT NULL, brief_json TEXT, targets_json TEXT NOT NULL DEFAULT '[]', card_ref TEXT, error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_card ON drafts(card_ref);
CREATE TABLE IF NOT EXISTS trends (
  id INTEGER PRIMARY KEY AUTOINCREMENT, fetched_at TEXT NOT NULL, platform TEXT NOT NULL,
  name TEXT NOT NULL, data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inspiration (
  id INTEGER PRIMARY KEY AUTOINCREMENT, added_at TEXT NOT NULL, source_url TEXT, platform TEXT,
  title TEXT, author TEXT, note TEXT, analysis_json TEXT
);
CREATE TABLE IF NOT EXISTS highlights (
  id INTEGER PRIMARY KEY AUTOINCREMENT, post_id TEXT NOT NULL, collection TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


def new_id() -> str:
    return uuid.uuid4().hex[:10]


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)

    # -- low level ---------------------------------------------------------------
    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, tuple(args))

    def _rows(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, tuple(args)).fetchall()]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- key/value ---------------------------------------------------------------
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        rows = self._rows("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def kv_set(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, iso(utcnow())),
        )

    # -- submissions -------------------------------------------------------------
    def add_submission(self, *, source: str, instruction: str, media: list[dict[str, Any]]) -> str:
        sid = new_id()
        self._exec(
            "INSERT INTO submissions(id,created_at,source,instruction,media_json,status) VALUES(?,?,?,?,?,?)",
            (sid, iso(utcnow()), source, instruction, json.dumps(media), "received"),
        )
        return sid

    def update_submission(self, sid: str, *, status: str | None = None, plan: dict[str, Any] | None = None,
                          error: str | None = None) -> None:
        if status is not None:
            self._exec("UPDATE submissions SET status=? WHERE id=?", (status, sid))
        if plan is not None:
            self._exec("UPDATE submissions SET plan_json=? WHERE id=?", (json.dumps(plan), sid))
        if error is not None:
            self._exec("UPDATE submissions SET error=? WHERE id=?", (error, sid))

    def get_submission(self, sid: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM submissions WHERE id=?", (sid,))
        return rows[0] if rows else None

    # -- posts -------------------------------------------------------------------
    def add_post(self, *, submission_id: str, platform: str, kind: str, caption: str, hashtags: list[str],
                 media: list[dict[str, Any]], publish_at: datetime, status: str, pillar: str = "",
                 highlight: str = "", why: str = "", method: str = "api") -> str:
        pid = new_id()
        now = iso(utcnow())
        self._exec(
            "INSERT INTO posts(id,submission_id,platform,kind,caption,hashtags_json,media_json,publish_at,"
            "status,pillar,highlight,why,created_at,updated_at,method) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, submission_id, platform, kind, caption, json.dumps(hashtags), json.dumps(media),
             iso(publish_at), status, pillar, highlight, why, now, now, method),
        )
        return pid

    def get_post(self, pid: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM posts WHERE id=?", (pid,))
        return _decode_post(rows[0]) if rows else None

    def list_posts(self, statuses: Iterable[str] | None = None, platform: str | None = None,
                   limit: int = 50, newest_first: bool = False) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM posts WHERE 1=1", []
        if statuses:
            st = list(statuses)
            sql += f" AND status IN ({','.join('?' * len(st))})"
            args += st
        if platform:
            sql += " AND platform=?"
            args.append(platform)
        sql += f" ORDER BY publish_at {'DESC' if newest_first else 'ASC'} LIMIT ?"
        args.append(limit)
        return [_decode_post(r) for r in self._rows(sql, args)]

    def due_posts(self, now: datetime) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM posts WHERE status=? AND publish_at<=? ORDER BY publish_at ASC", (
                "scheduled", iso(now)))
        return [_decode_post(r) for r in rows]

    def claim_post(self, pid: str) -> bool:
        """Atomically move scheduled -> publishing. False if someone else got it / it was cancelled."""
        cur = self._exec(
            "UPDATE posts SET status='publishing', updated_at=? WHERE id=? AND status='scheduled'",
            (iso(utcnow()), pid))
        return cur.rowcount == 1

    def set_post(self, pid: str, **fields: Any) -> None:
        allowed = {"status", "attempts", "remote_id", "permalink", "error", "note", "publish_at", "caption"}
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise KeyError(k)
            sets.append(f"{k}=?")
            args.append(iso(v) if isinstance(v, datetime) else v)
        sets.append("updated_at=?")
        args += [iso(utcnow()), pid]
        self._exec(f"UPDATE posts SET {', '.join(sets)} WHERE id=?", args)

    def cancel_post(self, pid: str) -> bool:
        cur = self._exec("UPDATE posts SET status='cancelled', updated_at=? WHERE id=? AND status='scheduled'",
                         (iso(utcnow()), pid))
        return cur.rowcount == 1

    def confirm_posted(self, pid: str) -> bool:
        """Manual method: the owner says they posted it themselves."""
        cur = self._exec("UPDATE posts SET status='published', updated_at=? WHERE id=? AND status='handed_off'",
                         (iso(utcnow()), pid))
        return cur.rowcount == 1

    def times_for(self, platform: str, statuses: Iterable[str], kinds: Iterable[str] | None = None) -> list[datetime]:
        st = list(statuses)
        sql = f"SELECT publish_at FROM posts WHERE platform=? AND status IN ({','.join('?' * len(st))})"
        args: list[Any] = [platform, *st]
        if kinds is not None:
            ks = list(kinds)
            sql += f" AND kind IN ({','.join('?' * len(ks))})"
            args += ks
        return [parse_iso(r["publish_at"]) for r in self._rows(sql, args)]

    # -- drafts --------------------------------------------------------------------
    _DRAFT_FIELDS = {"state", "tone", "lang", "instruction", "card_ref", "error"}

    def add_draft(self, *, submission_id: str, tone: str, lang: str, instruction: str, state: str) -> str:
        did = new_id()
        now = iso(utcnow())
        self._exec("INSERT INTO drafts(id,submission_id,state,tone,lang,instruction,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                   (did, submission_id, state, tone, lang, instruction, now, now))
        return did

    def get_draft(self, did: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM drafts WHERE id=?", (did,))
        return _decode_draft(rows[0]) if rows else None

    def update_draft(self, did: str, **fields: Any) -> None:
        sets, args = [], []
        for k, v in fields.items():
            if k == "brief":
                sets.append("brief_json=?")
                args.append(json.dumps(v))
            elif k == "targets":
                sets.append("targets_json=?")
                args.append(json.dumps(v))
            elif k in self._DRAFT_FIELDS:
                sets.append(f"{k}=?")
                args.append(v)
            else:
                raise KeyError(k)
        sets.append("updated_at=?")
        args += [iso(utcnow()), did]
        self._exec(f"UPDATE drafts SET {', '.join(sets)} WHERE id=?", args)

    def list_drafts(self, states: Iterable[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM drafts WHERE 1=1", []
        if states:
            st = list(states)
            sql += f" AND state IN ({','.join('?' * len(st))})"
            args += st
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [_decode_draft(r) for r in self._rows(sql, args)]

    def find_draft_by_card(self, ref: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM drafts WHERE card_ref=? ORDER BY updated_at DESC LIMIT 1", (ref,))
        return _decode_draft(rows[0]) if rows else None

    # -- trends / inspiration ------------------------------------------------------
    def replace_trends(self, items: list[dict[str, Any]]) -> None:
        now = iso(utcnow())
        with self._lock:
            self._db.execute("DELETE FROM trends")
            for it in items:
                self._db.execute(
                    "INSERT INTO trends(fetched_at,platform,name,data_json) VALUES(?,?,?,?)",
                    (now, it.get("platform", "both"), it.get("name", ""), json.dumps(it)))

    def list_trends(self, limit: int = 20) -> list[dict[str, Any]]:
        return [json.loads(r["data_json"]) | {"fetched_at": r["fetched_at"]}
                for r in self._rows("SELECT * FROM trends ORDER BY id ASC LIMIT ?", (limit,))]

    def add_inspiration(self, *, source_url: str = "", platform: str = "", title: str = "", author: str = "",
                        note: str = "", analysis: dict[str, Any] | None = None) -> int:
        cur = self._exec(
            "INSERT INTO inspiration(added_at,source_url,platform,title,author,note,analysis_json) VALUES(?,?,?,?,?,?,?)",
            (iso(utcnow()), source_url, platform, title, author, note, json.dumps(analysis or {})))
        return int(cur.lastrowid or 0)

    def has_inspiration_url(self, url: str) -> bool:
        return bool(self._rows("SELECT 1 FROM inspiration WHERE source_url=?", (url,)))

    def list_inspiration(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM inspiration ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["analysis"] = json.loads(r.pop("analysis_json") or "{}")
        return rows

    # -- highlights ----------------------------------------------------------------
    def add_highlight_pending(self, post_id: str, collection: str) -> None:
        self._exec("INSERT INTO highlights(post_id,collection,created_at) VALUES(?,?,?)",
                   (post_id, collection, iso(utcnow())))

    def pending_highlights(self) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT h.id, h.collection, h.created_at, p.id AS post_id, p.caption, p.permalink, p.publish_at "
            "FROM highlights h JOIN posts p ON p.id=h.post_id WHERE h.status='pending' ORDER BY h.collection, h.id")

    def mark_highlights_done(self, collection: str | None = None) -> int:
        if collection:
            cur = self._exec("UPDATE highlights SET status='done' WHERE status='pending' AND lower(collection)=lower(?)",
                             (collection,))
        else:
            cur = self._exec("UPDATE highlights SET status='done' WHERE status='pending'")
        return cur.rowcount


def _decode_post(row: dict[str, Any]) -> dict[str, Any]:
    row["hashtags"] = json.loads(row.pop("hashtags_json") or "[]")
    row["media"] = json.loads(row.pop("media_json") or "[]")
    return row


def _decode_draft(row: dict[str, Any]) -> dict[str, Any]:
    row["brief"] = json.loads(row.pop("brief_json") or "null")
    row["targets"] = json.loads(row.pop("targets_json") or "[]")
    return row
