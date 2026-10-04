"""Level-1 rules: executed without approval because they are written into the active programme version.

- double_progression@1: add load after the top of the rep range is reached at (or easier than) the target
  RIR in N consecutive exposures; otherwise aim for one more rep at the same load.
- planned deload: in a pre-planned deload week, fewer sets and more reps in reserve.
Every execution is recorded in rule_execution (inputs → output).
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from askesis.analytics.params import p
from askesis.core.ids import new_id
from askesis.core.timeutil import iso, now_utc
from askesis.reference import catalog, find_exercise

from . import store
from .model import WEEKDAYS, LiftItem, Programme

RULE_DP = "double_progression@1"


@dataclass
class Exposure:
    """Working sets of one exercise in one session."""

    day: date
    loads: list[float]
    reps: list[int]
    rirs: list[float | None]


@dataclass
class Prescription:
    exercise: str
    sets: int
    load_kg: float | None
    rep_target: int
    rep_range: tuple[int, int]
    target_rir: float
    reason: str


def _increment(exercise: str, load: float) -> float:
    ref = find_exercise(exercise) or next((e for e in catalog()["exercises"] if e["id"] == exercise), None)
    inc = float(ref["increment_kg"]) if ref and ref.get("increment_kg") else 2.5
    lo, hi = p("double_progression_load_increase")
    return min(max(inc, lo * load), hi * load)


def double_progression(item: LiftItem, history: list[Exposure]) -> Prescription:
    lo, hi = item.rep_range
    if not history:
        return Prescription(item.exercise, item.sets, None, lo, item.rep_range, item.target_rir,
                            "calibrazione: nessuno storico, scegliere un carico che lasci il RIR target")
    last = history[-1]
    load = max(last.loads)
    needed = p("double_progression_exposures")
    recent = history[-needed:]

    def topped(e: Exposure) -> bool:
        working = [(r, q) for r, q, ld in zip(e.reps, e.rirs, e.loads, strict=True) if ld == max(e.loads)]
        return (len(working) >= item.sets
                and all(r >= hi and q is not None and q >= item.target_rir for r, q in working))

    if len(recent) == needed and all(topped(e) for e in recent):
        inc = _increment(item.exercise, load)
        return Prescription(item.exercise, item.sets, round(load + inc, 2), lo, item.rep_range, item.target_rir,
                            f"top del range ({hi}) a RIR ≥ {item.target_rir:g} in {needed} esposizioni: +{inc:g} kg")
    if any(q is None for q in last.rirs):
        return Prescription(item.exercise, item.sets, load, min(hi, max(last.reps)), item.rep_range, item.target_rir,
                            "RIR mancante nell'ultima esposizione: carico invariato (progressione non valutabile)")
    target = min(hi, max(last.reps) + 1)
    return Prescription(item.exercise, item.sets, load, target, item.rep_range, item.target_rir,
                        f"stesso carico, obiettivo {target} ripetizioni")


def apply_deload(rx: Prescription) -> Prescription:
    rx.sets = max(1, math.ceil(rx.sets * p("deload_volume_factor")))
    rx.target_rir += p("deload_rir_increase")
    rx.reason += " · settimana di deload pianificata"
    return rx


def exposures(conn: sqlite3.Connection, exercise: str, before: date, limit: int = 4) -> list[Exposure]:
    ref = find_exercise(exercise)
    ex_id = ref["id"] if ref else None
    rows = conn.execute(
        """SELECT local_date, session_id, load_kg, reps, rir, rpe, set_type FROM v_set_record
           WHERE (exercise_id = ? OR (exercise_id IS NULL AND lower(exercise_raw) = lower(?)))
             AND local_date < ? AND set_type != 'warmup'
           ORDER BY local_date, start_at""",
        (ex_id, exercise, before.isoformat()),
    ).fetchall()
    by_session: dict[str, Exposure] = {}
    for r in rows:
        e = by_session.setdefault(r["session_id"], Exposure(date.fromisoformat(r["local_date"]), [], [], []))
        rir = r["rir"] if r["rir"] is not None else (10 - r["rpe"] if r["rpe"] is not None else None)
        e.loads.append(r["load_kg"])
        e.reps.append(r["reps"])
        e.rirs.append(rir)
    return sorted(by_session.values(), key=lambda e: e.day)[-limit:]


def next_session(conn: sqlite3.Connection, on: date, record: bool = True) -> dict:
    """Prescription for the session planned on `on`, applying L1 rules and pre-planned periods."""
    row = store.active(conn, "programme", on)
    if row is None:
        return {"status": "no_programme"}
    prog = Programme.model_validate(store.content(row))
    periods = store.periods_on(conn, on)
    use_minimal = any(pp["modifiers"].get("use_minimal_week") for pp in periods)
    week = prog.minimal_week if use_minimal else prog.microcycle
    planned = [s for s in week if s.day == WEEKDAYS[on.weekday()]]
    monday = on - timedelta(days=on.weekday())
    deload = monday in prog.deload_weeks
    volume_factor = min([pp["modifiers"].get("volume_factor", 1.0) for pp in periods] or [1.0])
    out = {"status": "rest_day" if not planned else "session", "date": on.isoformat(), "programme_version": row["id"],
           "minimal_week": use_minimal, "deload": deload, "periods": [pp["label"] for pp in periods], "sessions": []}
    for s in planned:
        lifts = []
        for item in s.lifts:
            hist = exposures(conn, item.exercise, on)
            rx = double_progression(item, hist)
            if deload:
                rx = apply_deload(rx)
            if volume_factor < 1.0:
                rx.sets = max(1, math.ceil(rx.sets * volume_factor))
                rx.reason += f" · periodo programmato (volume ×{volume_factor:g})"
            lifts.append(asdict(rx))
            if record:
                conn.execute(
                    """INSERT INTO rule_execution(id, rule_id, local_date, programme_version_id, inputs, output,
                           recorded_at) VALUES (?,?,?,?,?,?,?)""",
                    (new_id(), prog.progression_rule, on.isoformat(), row["id"],
                     json.dumps({"item": item.model_dump(mode="json"), "history": [asdict(h) for h in hist],
                                 "deload": deload, "volume_factor": volume_factor}, default=str),
                     json.dumps(asdict(rx)), iso(now_utc())),
                )
        out["sessions"].append({"name": s.name, "lifts": lifts, "run": s.run.model_dump() if s.run else None})
    if record:
        conn.commit()
    return out
