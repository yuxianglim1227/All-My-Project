"""Small, dependency-free time helpers. All stored times are UTC ISO-8601 strings."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    """UTC ISO string with a fixed shape so lexical order == chronological order."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed")
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"naive timestamp: {value!r}")
    return dt.astimezone(timezone.utc)


def tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # unknown zone -> be explicit rather than silently wrong
        raise ValueError(f"Unknown TIMEZONE {name!r}; use an IANA name like 'Europe/London'") from None


def parse_when(value: str, zone: ZoneInfo) -> datetime | None:
    """Parse a model-provided local datetime ('2026-11-12T18:30'). Returns UTC or None."""
    value = (value or "").strip()
    if not value or value.lower() in {"now", "next_slot", "asap"}:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
    return dt.astimezone(timezone.utc)
