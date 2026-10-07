"""Shared application services: one implementation behind every interface (CLI, local dashboard, future app/MCP).

Logging a day: parse the dictation line → build envelopes → preview (compact read-back) → save append-only →
evaluate safety. Interfaces only render the results.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from askesis import checkin
from askesis.config import Config
from askesis.core.timeutil import now_utc
from askesis.ingestion import manual, readback
from askesis.ingestion.pipeline import Receipt, ingest
from askesis.model.entities import Envelope
from askesis.parsers.text import parse_day
from askesis.safety import rules as safety


@dataclass
class DayPreview:
    records: list[dict]
    lines: list[str]
    needs_confirmation: bool  # workouts, runs and imports are saved only after an explicit confirmation


@dataclass
class DaySaved:
    receipt: Receipt
    lines: list[str]
    flags: list[dict] = field(default_factory=list)


def build_day(line: str, cfg: Config, day: date, now: datetime | None = None) -> list[dict]:
    """Records for a dictation line (raises ParseError on invalid input)."""
    now = now or now_utc()
    return [rec for it in parse_day(line, cfg.context_aliases) for rec in manual.build(it, cfg, day, now)]


def prepare(conn: sqlite3.Connection, records: list[dict]) -> list[dict]:
    """Questionnaire answers for a day and moment already answered become a merged new version (supersession)."""
    return checkin.merge(conn, records)


def preview(records: list[dict]) -> DayPreview:
    rows = [Envelope.model_validate(rec).model_dump(mode="json") for rec in records]
    return DayPreview(records, readback.lines(rows), readback.needs_confirmation(records))


def save(conn: sqlite3.Connection, records: list[dict], adapter: str) -> DaySaved:
    """Append-only ingestion, then safety evaluation on the latest day touched."""
    r = ingest(conn, records, adapter)
    lines = readback.lines([e.model_dump(mode="json") for e in r.inserted])
    flags: list[dict] = []
    if r.inserted:
        safety.evaluate(conn, max(e.local_date for e in r.inserted))
        flags = [dict(f) | {"actions": json.loads(f["actions"])} for f in safety.open_flags(conn)]
    return DaySaved(r, lines, flags)
