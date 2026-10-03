"""Append-only repository over the RAW store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from askesis.core.ids import new_id
from askesis.core.timeutil import iso, now_utc
from askesis.model.entities import Envelope


def payload_hash(entity_type: str, payload: dict) -> str:
    blob = json.dumps({"e": entity_type, "p": payload}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def ensure_source(conn: sqlite3.Connection, source_id: str, kind: str = "manual", priority: int = 100) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO source(id, kind, name, priority) VALUES (?, ?, ?, ?)",
        (source_id, kind, source_id, priority),
    )


def _ts(dt: datetime | None) -> str | None:
    return iso(dt) if dt else None


def insert(conn: sqlite3.Connection, env: Envelope, batch_id: str) -> None:
    conn.execute(
        """INSERT INTO raw_record (id, entity_type, schema_version, occurred_at, start_at, end_at, tz,
               local_date, recorded_at, source_id, source_record_id, device_id, entry_method,
               ingestion_batch_id, supersedes_id, missing_reason, payload, payload_hash,
               original_values, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            env.id, env.entity_type, env.schema_version, _ts(env.occurred_at), _ts(env.start_at),
            _ts(env.end_at), env.tz, env.local_date.isoformat(), iso(env.recorded_at), env.source_id,
            env.source_record_id, env.device_id, env.entry_method, batch_id, env.supersedes_id,
            env.missing_reason, json.dumps(env.payload, ensure_ascii=False),
            payload_hash(env.entity_type, env.payload),
            json.dumps(env.original_values, ensure_ascii=False) if env.original_values else None,
            env.notes,
        ),
    )


def get(conn: sqlite3.Connection, record_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM raw_record WHERE id = ?", (record_id,)).fetchone()


def is_current(conn: sqlite3.Connection, record_id: str) -> bool:
    return conn.execute("SELECT 1 FROM v_current WHERE id = ?", (record_id,)).fetchone() is not None


def exists_source_record(conn: sqlite3.Connection, source_id: str, source_record_id: str) -> bool:
    q = "SELECT 1 FROM raw_record WHERE source_id = ? AND source_record_id = ?"
    return conn.execute(q, (source_id, source_record_id)).fetchone() is not None


def exists_same_content(conn: sqlite3.Connection, env: Envelope) -> bool:
    """Same source, entity, local date and identical payload => duplicate (idempotent re-entry)."""
    q = """SELECT 1 FROM v_current WHERE source_id = ? AND entity_type = ? AND local_date = ?
           AND payload_hash = ? AND COALESCE(occurred_at, start_at) = ?"""
    when = _ts(env.occurred_at or env.start_at)
    h = payload_hash(env.entity_type, env.payload)
    return conn.execute(q, (env.source_id, env.entity_type, env.local_date.isoformat(), h, when)).fetchone() is not None


def retract(conn: sqlite3.Connection, record_id: str, reason: str) -> str:
    if not is_current(conn, record_id):
        raise ValueError(f"record non corrente o inesistente: {record_id}")
    rid = new_id()
    conn.execute(
        "INSERT INTO retraction(id, record_id, reason, recorded_at) VALUES (?, ?, ?, ?)",
        (rid, record_id, reason, iso(now_utc())),
    )
    return rid


def current(
    conn: sqlite3.Connection,
    entity_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    as_of: datetime | None = None,
) -> list[sqlite3.Row]:
    """Current records; with `as_of`, what the system knew at that instant (transaction time)."""
    params: list = []
    if as_of is None:
        sql = "SELECT * FROM v_current WHERE 1=1"
    else:
        t = iso(as_of)
        sql = """SELECT r.* FROM raw_record r
                 WHERE r.recorded_at <= ?
                   AND NOT EXISTS (SELECT 1 FROM raw_record s
                                   WHERE s.supersedes_id = r.id AND s.recorded_at <= ?)
                   AND NOT EXISTS (SELECT 1 FROM retraction x
                                   WHERE x.record_id = r.id AND x.recorded_at <= ?)"""
        params += [t, t, t]
    if entity_type:
        sql += " AND entity_type = ?"
        params.append(entity_type)
    if date_from:
        sql += " AND local_date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND local_date <= ?"
        params.append(date_to)
    sql += " ORDER BY local_date, COALESCE(occurred_at, start_at), id"
    return conn.execute(sql, params).fetchall()
