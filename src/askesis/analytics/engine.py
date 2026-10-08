"""Analytics engine: load RAW as of a knowledge cutoff, compute metrics deterministically, store a run.

Determinism: inputs are the RAW records visible at `cutoff` and the parameter file; wall-clock time is
stored as metadata only. `values_digest` lets any run be reproduced and compared.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from askesis.core.ids import new_id
from askesis.core.timeutil import iso, now_utc
from askesis.store import repository as repo

from . import adherence, body, energy, load, session, training
from .base import MetricValue, r6
from .params import PARAMS_DIR, p

ENGINE_VERSION = "0.5.0"


@dataclass
class Inputs:
    weighins: list = field(default_factory=list)
    waist: list = field(default_factory=list)
    nutrition: list[energy.NutritionDay] = field(default_factory=list)
    sets: list[training.SetRow] = field(default_factory=list)
    runs: list[training.RunRow] = field(default_factory=list)
    steps: dict[date, int] = field(default_factory=dict)
    context: list[tuple[date, str, float]] = field(default_factory=list)
    athlete: energy.AthleteBasics = field(default_factory=lambda: energy.AthleteBasics(None, None, None))
    attr_history: list[tuple[date, str, object]] = field(default_factory=list)  # (valid from, key, value)
    sleep: list[tuple[date, float]] = field(default_factory=list)  # (wake day, seconds asleep)
    plans: list[tuple[date, str, str, dict]] = field(default_factory=list)  # (valid from, recorded_at, kind, content)
    feedback: list[tuple[date, str, float | None, int | None]] = field(default_factory=list)  # post-session
    fingerprint: str = ""
    dates: list[date] = field(default_factory=list)


def _d(s: str) -> date:
    return date.fromisoformat(s)


def load_inputs(conn: sqlite3.Connection, cutoff: datetime | None = None) -> Inputs:
    rows = repo.current(conn, as_of=cutoff)
    inp = Inputs()
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r['id']}:{r['payload_hash']}".encode())
        e, pl, d = r["entity_type"], json.loads(r["payload"]), _d(r["local_date"])
        inp.dates.append(d)
        if e == "body_weight":
            inp.weighins.append((d, r["occurred_at"], pl["value_kg"], pl.get("fasted")))
        elif e == "body_measurement" and pl["site"].startswith("waist"):
            inp.waist.append((d, pl["readings_cm"]))
        elif e == "nutrition_day":
            inp.nutrition.append(energy.NutritionDay(d, pl.get("energy_kcal"), pl.get("protein_g"), pl["completeness"]))
        elif e == "set_record":
            inp.sets.append(training.SetRow(d, pl["session_id"], pl.get("exercise_id"), pl["exercise_raw"],
                                            pl["set_type"], pl["load_kg"], pl.get("load_kind", "external"),
                                            pl["reps"], pl.get("rir"), pl.get("rpe")))
        elif e == "running_session":
            inp.runs.append(training.RunRow(d, pl["distance_m"], pl["elapsed_s"], pl.get("avg_hr")))
        elif e == "daily_activity":
            inp.steps[d] = pl["steps"]
        elif e == "daily_context":
            inp.context.append((d, pl["key"], pl["value"]))
        elif e == "sleep_session" and pl.get("asleep_s"):
            inp.sleep.append((d, float(pl["asleep_s"])))
        elif e == "subjective_checkin" and pl.get("moment") == "post_session":
            inp.feedback.append((d, pl["session_kind"], _num(pl.get("session_rpe_cr10")),
                                 int(pl["session_minutes"]) if pl.get("session_minutes") is not None else None))
        elif e == "athlete_attribute":
            valid = pl.get("valid_from")
            inp.attr_history.append((date.fromisoformat(str(valid)) if valid else d, pl["key"], pl["value"]))
    plan_sql = "SELECT valid_from, recorded_at, kind, content, content_hash FROM plan_version"
    plan_rows = conn.execute(plan_sql + (" WHERE recorded_at <= ?" if cutoff else "") + " ORDER BY id",
                             (iso(cutoff),) if cutoff else ()).fetchall()
    for r in plan_rows:  # plans as known at the cutoff (transaction time)
        h.update(f"plan:{r['content_hash']}".encode())
        inp.plans.append((_d(r["valid_from"]), r["recorded_at"], r["kind"], json.loads(r["content"])))
    inp.athlete = athlete_as_of(inp, max(inp.dates) if inp.dates else date.max)
    inp.fingerprint = h.hexdigest()
    return inp


def athlete_as_of(inp: Inputs, on: date) -> energy.AthleteBasics:
    """Athlete attributes known on `on` (never a value that only becomes valid later)."""
    attrs: dict[str, object] = {}
    for valid, key, value in sorted(inp.attr_history, key=lambda x: x[0]):
        if valid <= on:
            attrs[key] = value
    age = attrs.get("age_years")
    if age is None and attrs.get("birth_date"):
        age = (on - date.fromisoformat(str(attrs["birth_date"]))).days / 365.25
    return energy.AthleteBasics(
        height_cm=_num(attrs.get("height_cm")),
        age_years=_num(age if age is not None else attrs.get("age_years_reported")),
        sex=attrs.get("sex_for_formulas"),
        activity_factor=_num(attrs.get("activity_factor")),
    )


def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def iso_weeks(start: date, end: date) -> list[tuple[date, date]]:
    w = start - timedelta(days=start.weekday())
    out = []
    while w <= end:
        out.append((w, w + timedelta(days=6)))
        w += timedelta(days=7)
    return out


def dq_week(inp: Inputs, daily: dict[date, float], start: date, end: date) -> list[MetricValue]:
    thresholds = p("dq_grade_thresholds")

    def grade(score: float) -> str:
        for g in ("A", "B", "C"):
            if score >= thresholds[g]:
                return g
        return "D"

    weight_cov = sum(1 for d in daily if start <= d <= end) / 7
    nutr_cov = sum(1 for n in inp.nutrition if start <= n.day <= end and n.completeness == "complete") / 7
    comps = {"body": weight_cov, "nutrition": nutr_cov}
    out = [MetricValue("dq_score", 1, f"domain:{k}", start, end, v, "fraction", "MEASUREMENT", 7,
                       detail={"grade": grade(v), "component": "completeness"}) for k, v in comps.items()]
    overall = min(comps.values())
    out.append(MetricValue("dq_score", 1, "domain:overall", start, end, overall, "fraction", "MEASUREMENT", 7,
                           detail={"grade": grade(overall), "rule": "min_of_components"}))
    return out


def compute(inp: Inputs, start: date, end: date) -> list[MetricValue]:
    daily = body.daily_weights(inp.weighins)
    vals: list[MetricValue] = [m for m in body.weight_daily(daily) if start <= m.period_start <= end]
    vals += [m for m in body.waist_sessions(inp.waist) if start <= m.period_start <= end]
    weekly_e1rm: dict[str, list[tuple[date, float]]] = {}
    if inp.sets:
        for ws, we in iso_weeks(min(s.day for s in inp.sets), end):
            for m in training.strength_week(inp.sets, ws, we):
                if m.metric_id == "e1rm_best_week" and m.value is not None:
                    weekly_e1rm.setdefault(m.subject.split(":", 1)[1], []).append((we, m.value))
    vals += session.session_metrics(inp.sets, start, end)
    strength_days = {s.day for s in inp.sets if s.set_type in training.WORK_TYPES}
    run_days = {r.day for r in inp.runs}
    vals += load.session_load(inp.feedback, start, end)
    for ws, we in iso_weeks(start, end):
        for m in (body.weight_ema(daily, we), body.weight_ma7(daily, we),
                  energy.adaptive_tdee(inp.nutrition, daily, athlete_as_of(inp, we), we),
                  training.steps_week(inp.steps, ws, we)):
            if m:
                vals.append(m)
        for days in p("weight_rate_windows"):
            vals += body.weight_rate(daily, we, days)
        vals += energy.intake_mean(inp.nutrition, we, 7)
        vals += training.strength_week(inp.sets, ws, we)
        vals += training.running_week(inp.runs, ws, we)
        vals += training.context_week(inp.context, ws, we)
        vals += dq_week(inp, daily, ws, we)
        vals += adherence.vs_target(inp.nutrition, inp.plans, ws, we)
        sv = adherence.sessions_vs_plan(inp.sets, inp.plans, ws, we)
        sl = adherence.sleep_week(inp.sleep, ws, we)
        vals += [m for m in (sv, sl) if m]
        vals += adherence.waist_change(inp.waist, ws, we)
        vals += adherence.e1rm_change(weekly_e1rm, ws, we)
        vals += load.load_week(inp.feedback, strength_days, run_days, ws, we, max(inp.dates, default=None))
    return sorted(vals, key=lambda m: (m.metric_id, m.subject, m.period_start, m.period_end))


def digest(values: list[MetricValue]) -> str:
    h = hashlib.sha256()
    for m in values:
        h.update(json.dumps([m.metric_id, m.version, m.subject, m.period_start.isoformat(), m.period_end.isoformat(),
                             r6(m.value), r6(m.lo), r6(m.hi), m.n_obs, r6(m.dq)]).encode())
    return h.hexdigest()


def params_fingerprint() -> str:
    h = hashlib.sha256()
    for f in sorted(PARAMS_DIR.glob("*.yaml")):
        h.update(f.read_bytes())
    return h.hexdigest()


def run(conn: sqlite3.Connection, start: date | None = None, end: date | None = None,
        cutoff: datetime | None = None) -> tuple[str, list[MetricValue], str]:
    """Compute and store a metric run. Returns (run_id, values, values_digest)."""
    cutoff = cutoff or now_utc()
    inp = load_inputs(conn, cutoff)
    if not inp.dates and (start is None or end is None):
        return "", [], digest([])
    start = start or min(inp.dates)
    end = end or max(inp.dates)
    values = compute(inp, start, end)
    dg = digest(values)
    run_id = new_id()
    with conn:
        conn.execute(
            """INSERT INTO metric_run(id, knowledge_cutoff, computed_at, input_fingerprint, params_fingerprint,
                   engine_version, period_start, period_end, values_digest) VALUES (?,?,?,?,?,?,?,?,?)""",
            (run_id, iso(cutoff), iso(now_utc()), inp.fingerprint, params_fingerprint(), ENGINE_VERSION,
             start.isoformat(), end.isoformat(), dg),
        )
        conn.executemany(
            """INSERT INTO metric_value(run_id, metric_id, version, subject, period_start, period_end, value, unit,
                   lo, hi, n_obs, dq, epistemic, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(run_id, m.metric_id, m.version, m.subject, m.period_start.isoformat(), m.period_end.isoformat(),
              m.value, m.unit, m.lo, m.hi, m.n_obs, m.dq, m.epistemic,
              json.dumps(m.detail, default=str) if m.detail else None) for m in values],
        )
    return run_id, values, dg


def rebuild(conn: sqlite3.Connection, cutoff: datetime | None = None) -> tuple[str, int, str, bool]:
    """Drop the derived cache, recompute everything, and verify reproducibility by recomputing in memory.

    Returns (run_id, n_values, digest, reproducible)."""
    with conn:
        conn.execute("DELETE FROM metric_value")
        conn.execute("DELETE FROM metric_run")
    cutoff = cutoff or now_utc()
    run_id, values, dg = run(conn, cutoff=cutoff)
    inp = load_inputs(conn, cutoff)
    again = digest(compute(inp, min(inp.dates), max(inp.dates))) if inp.dates else digest([])
    return run_id, len(values), dg, again == dg
