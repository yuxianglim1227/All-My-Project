"""Feed organisation: when to post, keeping the content mix balanced, and Highlights curation.

Instagram's API cannot create Highlights, so the agent tags each Story with a collection and produces a
short checklist of exactly which archived Stories to tap into which Highlight.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import BrandProfile
from .store import Store
from .timeutil import parse_iso

DEFAULT_WINDOWS = ["12:00", "19:00"]
LIVE_STATUSES = ("scheduled", "pending_approval", "publishing", "published")


def parse_hhmm(value: str) -> time:
    h, _, m = value.strip().partition(":")
    return time(int(h), int(m or 0))


def choose_slot(*, existing: list[datetime], now: datetime, windows: list[str], zone: ZoneInfo,
                min_gap: timedelta, max_per_day: int, jitter: timedelta = timedelta(0),
                lead: timedelta = timedelta(minutes=2), horizon_days: int = 21) -> datetime:
    """Earliest posting-window time (UTC) that is in the future, respects the gap and the daily cap."""
    times = sorted(parse_hhmm(w) for w in (windows or DEFAULT_WINDOWS))
    today = now.astimezone(zone).date()
    for offset in range(horizon_days + 1):
        day = today + timedelta(days=offset)
        taken = sum(1 for e in existing if e.astimezone(zone).date() == day)
        if taken >= max_per_day:
            continue
        for t in times:
            candidate = (datetime.combine(day, t, tzinfo=zone) + jitter).astimezone(now.tzinfo)
            if candidate < now + lead:
                continue
            if all(abs(candidate - e) >= min_gap for e in existing):
                return candidate
    # Nothing free in the horizon (extremely unlikely): queue behind the latest post.
    return max([now, *existing]) + min_gap


def pillar_status(brand: BrandProfile, recent: list[dict[str, Any]], window: int = 30) -> str:
    """One line telling the planner which pillars are over/under-represented (per piece of content)."""
    if not brand.pillars:
        return ""
    seen: dict[str, str] = {}
    for p in recent:
        seen.setdefault(p["submission_id"], p.get("pillar") or "")
    counts = Counter(v for v in list(seen.values())[:window] if v)
    total = sum(counts.values())
    if total == 0:
        return ""
    target_total = sum(p.weight for p in brand.pillars) or 100.0
    parts, under = [], []
    for p in brand.pillars:
        actual = 100.0 * counts.get(p.name, 0) / total
        target = 100.0 * p.weight / target_total
        parts.append(f"{p.name} {actual:.0f}% (target {target:.0f}%)")
        if actual < target - 10:
            under.append(p.name)
    line = f"Pillar balance over the last {total} piece(s): " + ", ".join(parts) + "."
    return line + (f" UNDER-REPRESENTED: {', '.join(under)}." if under else "")


def feed_summary(recent: list[dict[str, Any]], now: datetime, zone: ZoneInfo, limit: int = 9) -> str:
    """Compact list of the most recent grid items so the planner can vary what comes next."""
    lines = []
    for p in recent[:limit]:
        when = parse_iso(p["publish_at"]).astimezone(zone)
        delta = now - parse_iso(p["publish_at"])
        ago = f"{delta.days}d ago" if delta.days >= 1 else ("upcoming" if delta.total_seconds() < 0 else "today")
        lines.append(f"- {ago} ({when:%a %H:%M}) {p['platform']} {p['kind']} | pillar: {p.get('pillar') or '?'} | "
                     f"{(p.get('caption') or '').splitlines()[0][:70] if p.get('caption') else '(no caption)'}")
    return "\n".join(lines)


def highlight_checklist(store: Store, brand: BrandProfile, zone: ZoneInfo) -> str:
    """What the owner should tap into each Highlight (empty string when nothing is pending)."""
    pending = store.pending_highlights()
    if not pending:
        return ""
    desc = {h.name: h.description for h in brand.highlights}
    out = ["Highlights to update - open your Story Archive, select the Stories below and tap Highlight:"]
    by: dict[str, list[dict[str, Any]]] = {}
    for row in pending:
        by.setdefault(row["collection"], []).append(row)
    for name, rows in by.items():
        out.append(f"\n{name}" + (f" ({desc[name]})" if desc.get(name) else ""))
        for r in rows:
            when = parse_iso(r["publish_at"]).astimezone(zone)
            out.append(f"  - Story from {when:%a %d %b, %H:%M}")
    out.append("\nReply /highlights_done when finished (or /highlights_done <name>).")
    return "\n".join(out)
