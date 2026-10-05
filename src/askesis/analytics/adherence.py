"""Plan-aware and noise-aware weekly metrics for the review.

- Adherence ("fundamentals"): intake and protein vs the nutrition target in force each day, strength sessions done vs
  planned, sleep. The target comparison carries a confidence interval: when it excludes zero the difference is beyond
  day-to-day noise.
- Change vs noise ("lagging indicators"): waist change vs the minimal detectable difference estimated from repeated
  readings (claim measure.sem_minimal_difference); e1RM change vs the spread of past week-to-week changes.
  Without enough data the noise is reported as not yet estimable (no default value is invented).

Plan versions are those known at the knowledge cutoff (loaded by the engine) and in force on each day: a version valid
from a later day never affects an earlier period.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
from scipy import stats

from .base import MetricValue
from .params import p

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def plan_on(plans: list[tuple[date, str, str, dict]], kind: str, day: date) -> dict | None:
    """Content of the plan version of `kind` in force on `day` (latest valid_from ≤ day, then latest recorded)."""
    c = [x for x in plans if x[2] == kind and x[0] <= day]
    return max(c, key=lambda x: (x[0], x[1]))[3] if c else None


def _periods_on(plans, day: date) -> list[dict]:
    return [x[3] for x in plans if x[2] == "scheduled_period" and x[0] <= day
            and x[3].get("start", "") <= day.isoformat() <= x[3].get("end", "")]


def _mean_ci(values: list[float]) -> tuple[float, float | None, float | None]:
    k = np.array(values, dtype=float)
    if len(k) < 2:
        return float(k.mean()), None, None
    t = stats.t.ppf(0.5 + p("rate_ci_level") / 2, len(k) - 1)
    se = k.std(ddof=1) / math.sqrt(len(k))
    return float(k.mean()), float(k.mean() - t * se), float(k.mean() + t * se)


def vs_target(nutrition, plans, start: date, end: date) -> list[MetricValue]:
    """Daily (intake − target) over complete days, per energy and protein."""
    out = []
    for metric, field, tkey, unit in (("intake_vs_target_week", "kcal", "energy_kcal", "kcal/day"),
                                      ("protein_vs_target_week", "protein_g", "protein_g", "g/day")):
        diffs, targets, actual = [], [], []
        any_target = False
        for n in nutrition:
            if not start <= n.day <= end:
                continue
            t = plan_on(plans, "nutrition_target", n.day)
            if t is None:
                continue
            any_target = True
            v = getattr(n, field)
            if n.completeness != "complete" or v is None or t.get(tkey) is None:
                continue
            diffs.append(v - t[tkey])
            targets.append(t[tkey])
            actual.append(v)
        if not any_target and plan_on(plans, "nutrition_target", end) is None:
            continue
        target_end = (plan_on(plans, "nutrition_target", end) or {}).get(tkey)
        detail = {"complete_days": len(diffs), "target": target_end, "ci_level": p("rate_ci_level")}
        if len(diffs) < 2:
            out.append(MetricValue(metric, 1, "global", start, end, None, unit, "MEASUREMENT", len(diffs),
                                   detail=detail | {"reason": "insufficient_days"}))
            continue
        mean, lo, hi = _mean_ci(diffs)
        detail |= {"actual_mean": float(np.mean(actual)), "target_mean": float(np.mean(targets)),
                   "ratio": float(np.mean(actual) / np.mean(targets))}
        out.append(MetricValue(metric, 1, "global", start, end, mean, unit, "MEASUREMENT", len(diffs), lo=lo, hi=hi,
                               detail=detail))
    return out


def sessions_vs_plan(sets, plans, start: date, end: date) -> MetricValue | None:
    planned, any_prog = 0, False
    for i in range(7):
        day = start + timedelta(days=i)
        prog = plan_on(plans, "programme", day)
        if prog is None:
            continue
        any_prog = True
        minimal = any(pp.get("modifiers", {}).get("use_minimal_week") for pp in _periods_on(plans, day))
        week = prog.get("minimal_week" if minimal else "microcycle", [])
        planned += sum(1 for s in week if s["day"] == WEEKDAYS[day.weekday()] and s.get("lifts"))
    if not any_prog:
        return None
    done = len({s.session_id for s in sets if start <= s.day <= end and s.set_type != "warmup"})
    return MetricValue("sessions_vs_plan_week", 1, "global", start, end, float(done), "sessions", "MEASUREMENT", done,
                       detail={"planned": planned})


def sleep_week(sleep: list[tuple[date, float]], start: date, end: date) -> MetricValue | None:
    hours = [s / 3600 for d, s in sleep if start <= d <= end and s]
    if not hours:
        return None
    if len(hours) < p("sleep_min_days_for_mean"):
        return MetricValue("sleep_mean_week", 1, "global", start, end, None, "h/night", "MEASUREMENT", len(hours),
                           detail={"reason": "insufficient_days"})
    mean, lo, hi = _mean_ci(hours)
    return MetricValue("sleep_mean_week", 1, "global", start, end, mean, "h/night", "MEASUREMENT", len(hours),
                       lo=lo, hi=hi, detail={"ci_level": p("rate_ci_level")})


def waist_change(waist: list[tuple[date, list[float]]], start: date, end: date) -> list[MetricValue]:
    """Change of the session mean vs the previous session, against the minimal detectable difference."""
    sessions = sorted((d, r) for d, r in waist if d <= end)
    out = []
    for i, (d, readings) in enumerate(sessions):
        if not start <= d <= end or i == 0:
            continue
        prev_d, prev = sessions[i - 1]
        diff = float(np.mean(readings) - np.mean(prev))
        repeated = [r for _, r in sessions[: i + 1] if len(r) >= 2]
        detail = {"previous_session": prev_d.isoformat(), "noise_sessions": len(repeated)}
        md = None
        if len(repeated) >= p("noise_min_sessions"):
            tem = math.sqrt(float(np.mean([np.var(r, ddof=1) for r in repeated])))
            k = min(len(readings), len(prev))
            md = p("minimal_difference_z") * math.sqrt(2) * tem / math.sqrt(k)
            detail |= {"tem_cm": tem, "minimal_difference_cm": md}
        else:
            detail["reason"] = "noise_not_estimable"
        out.append(MetricValue("waist_change", 1, "global", d, d, diff, "cm", "ESTIMATE", len(readings),
                               lo=None if md is None else diff - md, hi=None if md is None else diff + md,
                               detail=detail))
    return out


def e1rm_change(weekly_best: dict[str, list[tuple[date, float]]], start: date, end: date) -> list[MetricValue]:
    """This week's best e1RM vs the previous week with a value; noise = spread of past week-to-week changes."""
    out = []
    for key, series in sorted(weekly_best.items()):
        s = sorted(x for x in series if x[0] <= end)
        if len(s) < 2 or not start <= s[-1][0] <= end:
            continue
        diff = s[-1][1] - s[-2][1]
        past = [b - a for (_, a), (_, b) in zip(s[:-2], s[1:-1], strict=True)]
        detail = {"previous_week_end": s[-2][0].isoformat(), "noise_points": len(past)}
        md = None
        if len(past) >= p("noise_min_points"):
            md = p("minimal_difference_z") * float(np.std(past, ddof=1))
            detail["minimal_difference_kg"] = md
        else:
            detail["reason"] = "noise_not_estimable"
        out.append(MetricValue("e1rm_change_week", 1, f"exercise:{key}", start, end, diff, "kg", "ESTIMATE", len(s),
                               lo=None if md is None else diff - md, hi=None if md is None else diff + md,
                               detail=detail))
    return out
