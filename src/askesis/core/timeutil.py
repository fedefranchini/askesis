"""Time handling: instants in UTC, IANA time zone kept alongside, derived local dates.

Rules (docs/architecture.md §3):
- instants are stored in UTC; the IANA zone is stored with the record (never fixed offsets);
- daily data use the local date; nutrition days close at a configurable cutoff (e.g. 03:00);
- sleep is assigned to the wake-up date.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def tz(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def now_utc() -> datetime:
    """Current instant. ASKESIS_NOW fixes it for rehearsals on a synthetic database: it is refused unless
    ASKESIS_DB_PATH points to a database outside data/, so the real database never sees a fake clock."""
    fake = os.environ.get("ASKESIS_NOW")
    if fake:
        db = os.environ.get("ASKESIS_DB_PATH", "")
        real_data = Path(__file__).resolve().parents[3] / "data"  # the real database lives here
        if not db or Path(db).resolve().is_relative_to(real_data):
            raise RuntimeError("ASKESIS_NOW è ammesso solo con ASKESIS_DB_PATH verso un database di prova")
        dt = datetime.fromisoformat(fake)
        return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)
    return datetime.now(UTC)


def to_utc(dt: datetime, zone: str) -> datetime:
    """Convert to UTC. Naive datetimes are interpreted as wall-clock time in `zone`."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(zone))
    return dt.astimezone(UTC)


def parse_instant(value: str, zone: str) -> datetime:
    """Parse an ISO-8601 string (with or without offset) and return an aware UTC datetime."""
    return to_utc(datetime.fromisoformat(value), zone)


def local_date(instant: datetime, zone: str) -> date:
    return instant.astimezone(ZoneInfo(zone)).date()


def nutrition_date(instant: datetime, zone: str, cutoff: time) -> date:
    """Local date of the nutrition day: before `cutoff` an intake belongs to the previous day."""
    local = instant.astimezone(ZoneInfo(zone))
    d = local.date()
    return d - timedelta(days=1) if local.time() < cutoff else d


def sleep_date(wake_instant: datetime, zone: str) -> date:
    return local_date(wake_instant, zone)


def at_local(d: date, t: time, zone: str) -> datetime:
    """UTC instant of local wall-clock time `t` on date `d` in `zone`."""
    return datetime.combine(d, t, tzinfo=ZoneInfo(zone)).astimezone(UTC)


def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")
