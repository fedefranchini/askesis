"""Data-quality rules (docs/architecture.md §13).

hard  -> record rejected (kept in rejected_record for audit)
soft  -> record accepted, issue opened for review
Thresholds are provisional defaults [PARAM].
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from askesis.analytics.params import p as p_
from askesis.model.entities import Envelope


@dataclass(frozen=True)
class Issue:
    rule: str
    severity: str  # hard | soft | info
    message: str


def _range(value, lo, hi, rule, label) -> list[Issue]:
    if value is None or lo <= value <= hi:
        return []
    return [Issue(rule, "hard", f"{label}={value} fuori dall'intervallo plausibile [{lo}, {hi}]")]


def hard_checks(env: Envelope) -> list[Issue]:
    p, e = env.payload, env.entity_type
    out: list[Issue] = []
    if e == "body_weight":
        out += _range(p.get("value_kg"), 30, 250, "weight.range", "peso kg")
    elif e == "body_measurement":
        for r in p.get("readings_cm", []):
            out += _range(r, 10, 250, "measurement.range", "circonferenza cm")
    elif e == "nutrition_day":
        out += _range(p.get("energy_kcal"), 0, 15000, "nutrition.energy_range", "kcal")
        out += _range(p.get("protein_g"), 0, 600, "nutrition.protein_range", "proteine g")
    elif e == "set_record":
        out += _range(p.get("load_kg"), 0, 1000, "set.load_range", "carico kg")
        out += _range(p.get("reps"), 0, 100, "set.reps_range", "ripetizioni")
        out += _range(p.get("rir"), 0, 10, "set.rir_range", "RIR")
        out += _range(p.get("rpe"), 1, 10, "set.rpe_range", "RPE")
        out += _range(p.get("pain"), 0, 10, "set.pain_range", "dolore")
    elif e == "running_session":
        out += _range(p.get("distance_m"), 1, 300_000, "run.distance_range", "distanza m")
        out += _range(p.get("elapsed_s"), 1, 48 * 3600, "run.duration_range", "durata s")
        for k in ("avg_hr", "max_hr"):
            out += _range(p.get(k), 25, 230, "run.hr_range", k)
        d, t = p.get("distance_m"), p.get("elapsed_s")
        if d and t and t / (d / 1000) < 150:  # faster than 2:30/km over the whole run
            out.append(Issue("run.pace_impossible", "hard", "passo medio più veloce di 2:30/km"))
    elif e == "daily_activity":
        out += _range(p.get("steps"), 0, 150_000, "steps.range", "passi")
    elif e == "resting_hr_daily":
        out += _range(p.get("bpm"), 25, 150, "rhr.range", "FC a riposo")
    elif e == "sleep_session":
        out += _range(p.get("asleep_s"), 0, 20 * 3600, "sleep.range", "sonno s")
    return out


def soft_checks(conn: sqlite3.Connection, env: Envelope) -> list[Issue]:
    """Checks that need history. Run after insert."""
    out: list[Issue] = []
    if env.entity_type == "body_weight":
        prev = conn.execute(
            """SELECT value_kg FROM v_body_weight WHERE id != ? AND local_date < ?
               ORDER BY local_date DESC LIMIT 1""",
            (env.id, env.local_date.isoformat()),
        ).fetchone()
        if prev:
            v, last = env.payload["value_kg"], prev["value_kg"]
            if abs(v - last) > p_("weight_jump_soft_kg"):
                out.append(Issue("weight.jump", "soft", f"variazione di {v - last:+.1f} kg rispetto all'ultima pesata"))
            if 2.0 < v / last < 2.4:
                out.append(Issue("weight.unit_suspect", "soft", "valore ~2,2× il precedente: possibili libbre"))
    if env.entity_type == "set_record" and env.payload.get("set_type") == "working":
        if env.payload.get("rir") is None and env.payload.get("rpe") is None:
            out.append(Issue("set.proximity_unknown", "info", "serie di lavoro senza RIR/RPE"))
        if env.payload.get("exercise_id") is None:
            out.append(Issue("set.exercise_unknown", "info",
                             f"esercizio non in catalogo: {env.payload.get('exercise_raw')!r}"))
    return out
