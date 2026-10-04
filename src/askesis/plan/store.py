"""Immutable, versioned plan objects with point-in-time queries (docs/architecture.md §14)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime

from askesis.core.ids import new_id
from askesis.core.timeutil import iso, now_utc

from .model import SCHEMAS


def add_version(conn: sqlite3.Connection, kind: str, name: str, content: dict, valid_from: date,
                intervention_id: str | None, recorded_at: datetime | None = None) -> str:
    validated = SCHEMAS[kind].model_validate(content).model_dump(mode="json")
    blob = json.dumps(validated, sort_keys=True, ensure_ascii=False)
    row = conn.execute("SELECT MAX(version_no) FROM plan_version WHERE kind=? AND name=?", (kind, name)).fetchone()
    vid = new_id()
    conn.execute(
        """INSERT INTO plan_version(id, kind, name, version_no, valid_from, content, content_hash, intervention_id,
               recorded_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (vid, kind, name, (row[0] or 0) + 1, valid_from.isoformat(), blob, hashlib.sha256(blob.encode()).hexdigest(),
         intervention_id, iso(recorded_at or now_utc())),
    )
    return vid


def active(conn: sqlite3.Connection, kind: str, on: date, as_of: datetime | None = None) -> sqlite3.Row | None:
    """Version in force on `on`; with `as_of`, what we believed at that instant (transaction time)."""
    sql = """SELECT * FROM plan_version WHERE kind = ? AND valid_from <= ?"""
    params: list = [kind, on.isoformat()]
    if as_of is not None:
        sql += " AND recorded_at <= ?"
        params.append(iso(as_of))
    sql += " ORDER BY valid_from DESC, recorded_at DESC, version_no DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def periods_on(conn: sqlite3.Connection, on: date) -> list[dict]:
    rows = conn.execute("SELECT content FROM plan_version WHERE kind = 'scheduled_period'").fetchall()
    out = []
    for r in rows:
        c = json.loads(r["content"])
        if c["start"] <= on.isoformat() <= c["end"]:
            out.append(c)
    return out


def history(conn: sqlite3.Connection, kind: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM plan_version" + (" WHERE kind = ?" if kind else "") + " ORDER BY kind, valid_from, version_no"
    return conn.execute(sql, (kind,) if kind else ()).fetchall()


def content(row: sqlite3.Row) -> dict:
    return json.loads(row["content"])
