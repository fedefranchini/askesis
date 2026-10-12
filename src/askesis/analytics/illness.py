"""Illness episodes from the athlete's health events: a confounder for the analyses and a signal for safety.

An episode is a `health_event` with kind "illness": `local_date` is the first day; the payload may carry `symptoms`
(list), `fever` and `below_neck` (booleans), `status` ("in_corso" | "risolto") and `end` (YYYY-MM-DD, the last ill
day) once resolved. A resolution is a new version of the same record (fix), so an analysis run at an earlier
knowledge cutoff still sees the episode as it was known then (no look-ahead). An episode without `end` lasts up to
the day being evaluated, and at most `illness_max_open_days` (an episode nobody closed does not grow forever).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from askesis.analytics.params import p

RESOLVED = "risolto"


@dataclass(frozen=True)
class Episode:
    record_id: str
    start: date
    end: date | None  # last ill day, None while still open
    symptoms: tuple[str, ...] = ()
    fever: bool = False
    below_neck: bool = False
    description: str = ""
    resolved: bool = False  # closed by the athlete: no longer open, its days still count as ill days
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    def open_on(self, day: date) -> bool:
        """Still going on (for the safety notice and the page): reported, not closed, not too old."""
        if self.resolved or day < self.start:
            return False
        return day <= self.start + timedelta(days=int(p("illness_max_open_days")) - 1)

    def days(self, until: date) -> list[date]:
        """Ill days known up to `until` (inclusive)."""
        last = self.end if self.end is not None else min(
            until, self.start + timedelta(days=int(p("illness_max_open_days")) - 1))
        last = min(last, until)
        return [self.start + timedelta(days=i) for i in range((last - self.start).days + 1)]


def from_record(record_id: str, local_date: str, payload: dict) -> Episode | None:
    if payload.get("kind") != "illness":
        return None
    start = date.fromisoformat(local_date)
    end = payload.get("end")
    if end is None and payload.get("status") == RESOLVED:
        end = local_date  # resolved without an explicit end: a one-day episode
    return Episode(record_id, start, date.fromisoformat(end) if end else None,
                   tuple(payload.get("symptoms") or ()), bool(payload.get("fever")), bool(payload.get("below_neck")),
                   payload.get("description") or "", payload.get("status") == RESOLVED)


def ill_days(episodes: list[Episode], until: date) -> set[date]:
    out: set[date] = set()
    for e in episodes:
        out.update(e.days(until))
    return out


def open_on(episodes: list[Episode], day: date) -> list[Episode]:
    return [e for e in episodes if e.open_on(day)]
