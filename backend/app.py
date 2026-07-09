"""
Baby Tracker — backend
FastAPI + SQLite, single process. Serves the JSON API under /api/*
and the static frontend (PWA) for everything else, so the whole app
is one executable listening on one port — easy to run as a Windows
service on your AWS Windows Server.

Run locally:
    pip install -r requirements.txt
    uvicorn app:app --host 0.0.0.0 --port 8000

Env vars (optional, see .env.example):
    ACCESS_CODE   shared passcode all devices must send. Leave blank to disable.
    DB_PATH       path to the SQLite file (default: ./babytracker.db)
"""

import os
import sqlite3
import uuid
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "babytracker.db"))
ACCESS_CODE = os.environ.get("ACCESS_CODE", "").strip()
FRONTEND_DIR = (BASE_DIR.parent / "frontend").resolve()

app = FastAPI(title="Baby Tracker API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # the packaged APK/webview loads from a different origin
    allow_methods=["*"],
    allow_headers=["*"],
)

# ───────────────────────────── DB helpers ─────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS feeds (
    id TEXT PRIMARY KEY,
    time TEXT NOT NULL,
    type TEXT NOT NULL,
    amount_ml INTEGER,
    side TEXT,
    duration_min INTEGER,
    note TEXT,
    by TEXT
);
CREATE TABLE IF NOT EXISTS naps (
    id TEXT PRIMARY KEY,
    start TEXT NOT NULL,
    end TEXT,
    by TEXT
);
CREATE TABLE IF NOT EXISTS meals (
    id TEXT PRIMARY KEY,
    time TEXT NOT NULL,
    slot TEXT,
    foods TEXT,
    reaction TEXT,
    note TEXT,
    by TEXT
);
CREATE TABLE IF NOT EXISTS diapers (
    id TEXT PRIMARY KEY,
    time TEXT NOT NULL,
    kind TEXT,
    by TEXT
);
CREATE TABLE IF NOT EXISTS growth (
    id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    weight_kg REAL,
    height_cm REAL,
    head_cm REAL,
    by TEXT
);
CREATE TABLE IF NOT EXISTS schedule (
    id TEXT PRIMARY KEY,
    time TEXT,
    label TEXT,
    type TEXT
);
CREATE TABLE IF NOT EXISTS meal_plan (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS prep_list (
    id TEXT PRIMARY KEY,
    text TEXT,
    done INTEGER DEFAULT 0
);
"""

DEFAULT_SCHEDULE = [
    ("07:00", "Wake up & morning feed", "feed"),
    ("09:30", "Morning nap", "sleep"),
    ("12:00", "Lunch (solids)", "meal"),
    ("13:00", "Afternoon nap", "sleep"),
    ("17:30", "Dinner (solids)", "meal"),
    ("19:00", "Bath & bedtime feed", "feed"),
]


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        cur = conn.execute("SELECT COUNT(*) AS c FROM schedule")
        if cur.fetchone()["c"] == 0:
            for time_, label, type_ in DEFAULT_SCHEDULE:
                conn.execute(
                    "INSERT INTO schedule (id, time, label, type) VALUES (?,?,?,?)",
                    (str(uuid.uuid4())[:8], time_, label, type_),
                )
        cur = conn.execute("SELECT value FROM settings WHERE key='baby_name'")
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('baby_name', 'Baby')"
            )


init_db()


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat()


def new_id():
    return uuid.uuid4().hex[:10]


# ───────────────────────────── Access control ─────────────────────────────

def require_access(x_access_code: Optional[str] = Header(default=None)):
    """If ACCESS_CODE is set on the server, every /api call must include it."""
    if ACCESS_CODE:
        if not x_access_code or not _const_eq(x_access_code, ACCESS_CODE):
            raise HTTPException(status_code=401, detail="Invalid or missing access code")
    return True


def _const_eq(a: str, b: str) -> bool:
    ha = hashlib.sha256(a.encode()).digest()
    hb = hashlib.sha256(b.encode()).digest()
    return ha == hb


# ───────────────────────────── Models ─────────────────────────────

class FeedIn(BaseModel):
    type: str  # bottle | breast
    amount_ml: Optional[int] = None
    side: Optional[str] = None
    duration_min: Optional[int] = None
    note: Optional[str] = ""
    by: Optional[str] = None


class NapStartIn(BaseModel):
    by: Optional[str] = None


class MealIn(BaseModel):
    slot: str
    foods: List[str] = []
    reaction: str = "some"
    note: Optional[str] = ""
    by: Optional[str] = None


class DiaperIn(BaseModel):
    kind: str  # wet | dirty | mixed
    by: Optional[str] = None


class GrowthIn(BaseModel):
    weight_kg: Optional[float] = None
    height_cm: Optional[float] = None
    head_cm: Optional[float] = None
    by: Optional[str] = None


class ScheduleIn(BaseModel):
    time: str
    label: str
    type: str


class MealPlanIn(BaseModel):
    value: str


class PrepIn(BaseModel):
    text: str


class PrepPatchIn(BaseModel):
    done: bool


class SettingsIn(BaseModel):
    baby_name: str


# ───────────────────────────── Full-state read ─────────────────────────────

@app.get("/api/health")
def health():
    return {"ok": True, "time": now_iso()}


@app.get("/api/state", dependencies=[Depends(require_access)])
def get_state():
    with db() as conn:
        feeds = [dict(r) for r in conn.execute("SELECT * FROM feeds ORDER BY time DESC")]
        naps = [dict(r) for r in conn.execute("SELECT * FROM naps WHERE end IS NOT NULL ORDER BY start DESC")]
        active = conn.execute("SELECT * FROM naps WHERE end IS NULL ORDER BY start DESC LIMIT 1").fetchone()
        meals_raw = conn.execute("SELECT * FROM meals ORDER BY time DESC")
        meals = []
        for r in meals_raw:
            m = dict(r)
            m["foods"] = m["foods"].split("||") if m["foods"] else []
            meals.append(m)
        diapers = [dict(r) for r in conn.execute("SELECT * FROM diapers ORDER BY time DESC")]
        growth = [dict(r) for r in conn.execute("SELECT * FROM growth ORDER BY date DESC")]
        schedule = [dict(r) for r in conn.execute("SELECT * FROM schedule ORDER BY time ASC")]
        meal_plan = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM meal_plan")}
        prep_list = [dict(r) for r in conn.execute("SELECT * FROM prep_list")]
        baby_name_row = conn.execute("SELECT value FROM settings WHERE key='baby_name'").fetchone()

    return {
        "babyName": baby_name_row["value"] if baby_name_row else "Baby",
        "feeds": feeds,
        "naps": naps,
        "activeNap": dict(active) if active else None,
        "meals": meals,
        "diapers": diapers,
        "growth": growth,
        "schedule": schedule,
        "mealPlan": meal_plan,
        "prepList": prep_list,
        "serverTime": now_iso(),
    }


# ───────────────────────────── Feeds ─────────────────────────────

@app.post("/api/feeds", dependencies=[Depends(require_access)])
def add_feed(body: FeedIn):
    fid = new_id()
    with db() as conn:
        conn.execute(
            "INSERT INTO feeds (id, time, type, amount_ml, side, duration_min, note, by) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (fid, now_iso(), body.type, body.amount_ml, body.side, body.duration_min, body.note, body.by),
        )
    return {"id": fid}


@app.delete("/api/feeds/{feed_id}", dependencies=[Depends(require_access)])
def delete_feed(feed_id: str):
    with db() as conn:
        conn.execute("DELETE FROM feeds WHERE id=?", (feed_id,))
    return {"ok": True}


# ───────────────────────────── Naps ─────────────────────────────

@app.post("/api/naps/start", dependencies=[Depends(require_access)])
def start_nap(body: NapStartIn):
    with db() as conn:
        existing = conn.execute("SELECT id FROM naps WHERE end IS NULL").fetchone()
        if existing:
            raise HTTPException(400, "A nap is already in progress")
        nid = new_id()
        conn.execute(
            "INSERT INTO naps (id, start, end, by) VALUES (?,?,NULL,?)",
            (nid, now_iso(), body.by),
        )
    return {"id": nid}


@app.post("/api/naps/end", dependencies=[Depends(require_access)])
def end_nap():
    with db() as conn:
        row = conn.execute("SELECT id FROM naps WHERE end IS NULL ORDER BY start DESC LIMIT 1").fetchone()
        if not row:
            raise HTTPException(400, "No nap in progress")
        conn.execute("UPDATE naps SET end=? WHERE id=?", (now_iso(), row["id"]))
    return {"ok": True}


@app.delete("/api/naps/{nap_id}", dependencies=[Depends(require_access)])
def delete_nap(nap_id: str):
    with db() as conn:
        conn.execute("DELETE FROM naps WHERE id=?", (nap_id,))
    return {"ok": True}


# ───────────────────────────── Meals ─────────────────────────────

@app.post("/api/meals", dependencies=[Depends(require_access)])
def add_meal(body: MealIn):
    mid = new_id()
    with db() as conn:
        conn.execute(
            "INSERT INTO meals (id, time, slot, foods, reaction, note, by) VALUES (?,?,?,?,?,?,?)",
            (mid, now_iso(), body.slot, "||".join(body.foods), body.reaction, body.note, body.by),
        )
    return {"id": mid}


@app.delete("/api/meals/{meal_id}", dependencies=[Depends(require_access)])
def delete_meal(meal_id: str):
    with db() as conn:
        conn.execute("DELETE FROM meals WHERE id=?", (meal_id,))
    return {"ok": True}


# ───────────────────────────── Diapers ─────────────────────────────

@app.post("/api/diapers", dependencies=[Depends(require_access)])
def add_diaper(body: DiaperIn):
    did = new_id()
    with db() as conn:
        conn.execute(
            "INSERT INTO diapers (id, time, kind, by) VALUES (?,?,?,?)",
            (did, now_iso(), body.kind, body.by),
        )
    return {"id": did}


@app.delete("/api/diapers/{diaper_id}", dependencies=[Depends(require_access)])
def delete_diaper(diaper_id: str):
    with db() as conn:
        conn.execute("DELETE FROM diapers WHERE id=?", (diaper_id,))
    return {"ok": True}


# ───────────────────────────── Growth ─────────────────────────────

@app.post("/api/growth", dependencies=[Depends(require_access)])
def add_growth(body: GrowthIn):
    gid = new_id()
    with db() as conn:
        conn.execute(
            "INSERT INTO growth (id, date, weight_kg, height_cm, head_cm, by) VALUES (?,?,?,?,?,?)",
            (gid, now_iso(), body.weight_kg, body.height_cm, body.head_cm, body.by),
        )
    return {"id": gid}


@app.delete("/api/growth/{growth_id}", dependencies=[Depends(require_access)])
def delete_growth(growth_id: str):
    with db() as conn:
        conn.execute("DELETE FROM growth WHERE id=?", (growth_id,))
    return {"ok": True}


# ───────────────────────────── Schedule ─────────────────────────────

@app.post("/api/schedule", dependencies=[Depends(require_access)])
def add_schedule(body: ScheduleIn):
    sid = new_id()
    with db() as conn:
        conn.execute(
            "INSERT INTO schedule (id, time, label, type) VALUES (?,?,?,?)",
            (sid, body.time, body.label, body.type),
        )
    return {"id": sid}


@app.delete("/api/schedule/{item_id}", dependencies=[Depends(require_access)])
def delete_schedule(item_id: str):
    with db() as conn:
        conn.execute("DELETE FROM schedule WHERE id=?", (item_id,))
    return {"ok": True}


# ───────────────────────────── Meal plan grid ─────────────────────────────

@app.put("/api/meal-plan/{key}", dependencies=[Depends(require_access)])
def set_meal_plan(key: str, body: MealPlanIn):
    with db() as conn:
        conn.execute(
            "INSERT INTO meal_plan (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, body.value),
        )
    return {"ok": True}


# ───────────────────────────── Prep checklist ─────────────────────────────

@app.post("/api/prep", dependencies=[Depends(require_access)])
def add_prep(body: PrepIn):
    pid = new_id()
    with db() as conn:
        conn.execute("INSERT INTO prep_list (id, text, done) VALUES (?,?,0)", (pid, body.text))
    return {"id": pid}


@app.patch("/api/prep/{prep_id}", dependencies=[Depends(require_access)])
def patch_prep(prep_id: str, body: PrepPatchIn):
    with db() as conn:
        conn.execute("UPDATE prep_list SET done=? WHERE id=?", (1 if body.done else 0, prep_id))
    return {"ok": True}


@app.delete("/api/prep/{prep_id}", dependencies=[Depends(require_access)])
def delete_prep(prep_id: str):
    with db() as conn:
        conn.execute("DELETE FROM prep_list WHERE id=?", (prep_id,))
    return {"ok": True}


# ───────────────────────────── Settings ─────────────────────────────

@app.put("/api/settings", dependencies=[Depends(require_access)])
def set_settings(body: SettingsIn):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('baby_name', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (body.baby_name,),
        )
    return {"ok": True}


@app.post("/api/auth/check", dependencies=[Depends(require_access)])
def auth_check():
    """Frontend calls this once with the code the user typed in, to verify it."""
    return {"ok": True}


# ───────────────────────────── Frontend static files ─────────────────────────────
# Mounted last so it never shadows the /api routes above.

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
