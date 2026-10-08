"""Strength, running, activity and context metrics (docs/architecture.md §5.3–5.4). Pure functions."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import numpy as np

from askesis.reference import catalog

from .base import MetricValue
from .params import p

WORK_TYPES = {"working", "top", "backoff", "amrap", "drop", "myo", "cluster"}


@dataclass(frozen=True)
class SetRow:
    day: date
    session_id: str
    exercise_id: str | None
    exercise_raw: str
    set_type: str
    load_kg: float
    load_kind: str
    reps: int
    rir: float | None
    rpe: float | None


def rir_of(s: SetRow) -> float | None:
    if s.rir is not None:
        return s.rir
    if s.rpe is not None:
        return 10 - s.rpe  # convention: RIR = 10 − RPE (rt.rir_scale)
    return None


def is_hard(s: SetRow) -> bool | None:
    """True/False for working sets with known proximity; None when proximity is unknown."""
    if s.set_type not in WORK_TYPES:
        return False
    r = rir_of(s)
    return None if r is None else r <= p("hard_set_max_rir")


def e1rm(s: SetRow) -> tuple[float, bool] | None:
    """(estimate, is_lower_bound). Only for external loads and reps+RIR within the valid range."""
    if s.load_kind != "external" or s.load_kg <= 0 or s.set_type == "warmup":
        return None
    r = rir_of(s)
    reps_eq = s.reps + (r or 0)
    if reps_eq > p("e1rm_max_reps_equivalent") or s.reps < 1:
        return None
    return s.load_kg * (1 + reps_eq / 30), r is None


def _muscles(exercise_id: str | None) -> dict[str, float]:
    if exercise_id is None:
        return {}
    for ex in catalog()["exercises"]:
        if ex["id"] == exercise_id:
            frac = p("secondary_muscle_fraction")
            return {m: (1.0 if f >= 1 else frac) for m, f in ex.get("muscles", {}).items()}
    return {}


def strength_week(sets: list[SetRow], start: date, end: date) -> list[MetricValue]:
    week = [s for s in sets if start <= s.day <= end]
    out: list[MetricValue] = []
    if not week:
        return out
    sessions = {s.session_id for s in week}
    hard = [s for s in week if is_hard(s)]
    unknown = [s for s in week if is_hard(s) is None]
    out.append(MetricValue("sessions_strength", 1, "global", start, end, len(sessions), "sessions", "MEASUREMENT",
                           len(sessions)))
    out.append(MetricValue("hard_sets", 1, "global", start, end, len(hard), "sets", "MEASUREMENT", len(week),
                           detail={"proximity_unknown_sets": len(unknown)}))
    vol: dict[str, float] = defaultdict(float)
    uncatalogued = 0
    for s in hard:
        m = _muscles(s.exercise_id)
        if not m:
            uncatalogued += 1
        for muscle, f in m.items():
            vol[muscle] += f
    for muscle, v in sorted(vol.items()):
        out.append(MetricValue("volume_per_muscle_week", 1, f"muscle:{muscle}", start, end, v, "fractional_hard_sets",
                               "ESTIMATE", len(hard), detail={"uncatalogued_hard_sets": uncatalogued}))
    best: dict[str, tuple[float, bool, int]] = {}
    for s in week:
        est = e1rm(s)
        key = s.exercise_id or f"raw:{s.exercise_raw.lower()}"
        if est and (key not in best or est[0] > best[key][0]):
            best[key] = (est[0], est[1], 1)
    for key, (val, lower, _) in sorted(best.items()):
        out.append(MetricValue("e1rm_best_week", 1, f"exercise:{key}", start, end, val, "kg", "ESTIMATE", 1,
                               detail={"lower_bound": lower, "formula": p("e1rm_formula")}))
    return out


@dataclass(frozen=True)
class RunRow:
    day: date
    distance_m: float
    elapsed_s: int
    avg_hr: int | None
    moving_s: int | None = None
    run_type: str | None = None
    environment: str | None = None
    elev_gain_m: float | None = None


def running_week(runs: list[RunRow], start: date, end: date) -> list[MetricValue]:
    week = [r for r in runs if start <= r.day <= end]
    if not week:
        return []
    km = sum(r.distance_m for r in week) / 1000
    secs = sum(r.elapsed_s for r in week)
    out = [
        MetricValue("run_volume_km", 1, "global", start, end, km, "km", "MEASUREMENT", len(week)),
        MetricValue("run_time_min", 1, "global", start, end, secs / 60, "min", "MEASUREMENT", len(week)),
        MetricValue("run_count", 1, "global", start, end, len(week), "runs", "MEASUREMENT", len(week)),
    ]
    hr = [(r.avg_hr, r.elapsed_s) for r in week if r.avg_hr]
    if hr:
        mean_hr = sum(h * t for h, t in hr) / sum(t for _, t in hr)
        out.append(MetricValue("run_avg_hr", 1, "global", start, end, mean_hr, "bpm", "MEASUREMENT", len(hr)))
    return out


def steps_week(steps: dict[date, int], start: date, end: date) -> MetricValue | None:
    vals = [v for d, v in steps.items() if start <= d <= end]
    if not vals:
        return None
    ok = len(vals) >= p("steps_min_days_for_mean")
    return MetricValue("steps_mean_week", 1, "global", start, end, float(np.mean(vals)) if ok else None, "steps/day",
                       "MEASUREMENT", len(vals), dq=len(vals) / 7)


def context_week(ctx: list[tuple[date, str, float]], start: date, end: date) -> list[MetricValue]:
    by_key: dict[str, list[float]] = defaultdict(list)
    for d, k, v in ctx:
        if start <= d <= end and isinstance(v, (int, float)):
            by_key[k].append(float(v))
    return [MetricValue("context_mean_week", 1, f"context:{k}", start, end, float(np.mean(v)), "units/day",
                        "MEASUREMENT", len(v), dq=len(v) / 7) for k, v in sorted(by_key.items())]
