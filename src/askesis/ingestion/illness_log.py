"""Reporting an illness from any interface (dashboard, CLI): one `health_event` per episode, kind "illness".

Symptoms come from a short fixed list, so fever and "below the neck" (chest, whole body, stomach) are derived, not
typed: they decide the safety message (plan stop criterion: fever or symptoms below the neck → no training until it
passes). Updating the symptoms and closing the episode ("Sto meglio") create new versions of the same record, so
earlier analyses keep what was known then. Food not logged because of the illness is recorded as such
(`completeness: not_logged`), never as an estimate or as zero.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

from askesis.analytics import illness
from askesis.config import Config
from askesis.core.ids import new_id
from askesis.core.timeutil import at_local
from askesis.ingestion import manual

# key -> (label, below the neck, fever)
SYMPTOMS: dict[str, tuple[str, bool, bool]] = {
    "raffreddore": ("raffreddore", False, False),
    "gola": ("mal di gola", False, False),
    "testa": ("mal di testa", False, False),
    "febbre": ("febbre", True, True),
    "tosse": ("tosse di petto", True, False),
    "dolori": ("dolori muscolari diffusi", True, False),
    "stomaco": ("stomaco o intestino", True, False),
    "stanchezza": ("stanchezza marcata", True, False),
}


class IllnessError(ValueError):
    pass


def _payload(keys: list[str], note: str) -> dict:
    keys = [k for k in SYMPTOMS if k in keys]
    if not keys and not note.strip():
        raise IllnessError("scegli almeno un sintomo o scrivi una nota")
    labels = [SYMPTOMS[k][0] for k in keys]
    return {"kind": "illness", "symptoms": labels, "fever": any(SYMPTOMS[k][2] for k in keys),
            "below_neck": any(SYMPTOMS[k][1] for k in keys), "status": "in_corso",
            "description": ", ".join(labels + ([note.strip()] if note.strip() else []))}


def keys_of(symptoms: list[str]) -> list[str]:
    """Symptom keys from stored labels (to prefill the form)."""
    by_label = {v[0]: k for k, v in SYMPTOMS.items()}
    return [by_label[s] for s in symptoms if s in by_label]


def not_logged_food(cfg: Config, now: datetime, day: date, reason: str) -> dict:
    rec = manual._env("nutrition_day", cfg, now, day, {"completeness": "not_logged"},
                      occurred_at=at_local(day, manual.NOON, cfg.timezone))
    rec["missing_reason"] = reason
    return rec


def report(cfg: Config, now: datetime, day: date, keys: list[str], note: str = "",
           food_days: tuple[date, ...] = ()) -> list[dict]:
    """A new episode starting on `day`, plus the nutrition days the athlete says will not be logged."""
    pl = _payload(keys, note)
    rec = manual._env("health_event", cfg, now, day, pl, occurred_at=at_local(day, manual.NOON, cfg.timezone))
    return [rec] + [not_logged_food(cfg, now, d, f"malessere ({pl['description']})") for d in food_days]


def _version(row: sqlite3.Row, now: datetime, payload: dict) -> dict:
    rec = {k: row[k] for k in ("entity_type", "occurred_at", "start_at", "end_at", "tz", "local_date", "source_id",
                               "entry_method") if row[k] is not None}
    return rec | {"id": new_id(), "recorded_at": now, "supersedes_id": row["id"], "payload": payload}


def update(row: sqlite3.Row, now: datetime, keys: list[str], note: str = "") -> dict:
    return _version(row, now, json.loads(row["payload"]) | _payload(keys, note))


def recover(row: sqlite3.Row, now: datetime, today: date) -> dict:
    """'Sto meglio' today: the last ill day was yesterday (or the first day, if it is the same day)."""
    start = date.fromisoformat(row["local_date"])
    end = max(start, today - timedelta(days=1))
    return _version(row, now, json.loads(row["payload"]) | {"status": illness.RESOLVED, "end": end.isoformat()})


def open_episode(conn: sqlite3.Connection, day: date) -> tuple[sqlite3.Row, illness.Episode] | None:
    """The illness still open on `day`, if any (the most recent one)."""
    rows = conn.execute("""SELECT * FROM v_current WHERE entity_type = 'health_event' AND local_date <= ?
                           ORDER BY local_date DESC""", (day.isoformat(),)).fetchall()
    for r in rows:
        e = illness.from_record(r["id"], r["local_date"], json.loads(r["payload"]))
        if e is not None and e.open_on(day):
            return r, e
    return None
