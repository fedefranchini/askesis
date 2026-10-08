"""Foster session load, weekly load, monotony and strain (claim load.monotony_strain, Foster 1998).

Pure functions over the post-session feedback (session-RPE on the CR-10 scale, minutes) and the days with recorded
training.
- session_load: RPE × minutes of one session (arbitrary units, AU); needs both answers.
- training_load_week: sum of the session loads of an ISO week.
- training_monotony_week: mean of the 7 daily loads (rest days = 0, sessions of one day summed) divided by their
  standard deviation (population SD, parameter monotony_sd). Not defined when the SD is zero, nor when a recorded
  session lacks its load, nor while the week is still in progress (an incomplete week would distort it).
- training_strain_week: weekly load × monotony, undefined whenever the monotony is.
A session is "recorded" if there are set records (strength) or a running session on that day, or a feedback for it.
Only data of days ≤ the end of the period are used (no look-ahead).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

import numpy as np

from .base import MetricValue
from .params import p

Feedback = tuple[date, str, float | None, int | None]  # (day, session_kind, rpe_cr10, minutes)


def _usable(f: Feedback) -> bool:
    return f[2] is not None and f[3] is not None


def _formula() -> None:
    if p("session_load_formula") != "cr10_x_minutes":
        raise ValueError(f"session_load_formula non supportata: {p('session_load_formula')}")


def session_load(feedback: Iterable[Feedback], start: date, end: date) -> list[MetricValue]:
    """One value per session with both RPE and minutes in [start, end]."""
    _formula()
    out = []
    for day, kind, rpe, minutes in sorted(feedback, key=lambda f: (f[0], f[1])):
        if not start <= day <= end or not _usable((day, kind, rpe, minutes)):
            continue
        out.append(MetricValue("session_load", 1, f"session:{kind}", day, day, float(rpe) * minutes, "AU",
                               "ESTIMATE", 1, detail={"rpe_cr10": rpe, "minutes": minutes}))
    return out


def load_week(feedback: Iterable[Feedback], strength_days: Iterable[date], run_days: Iterable[date],
              start: date, end: date, known_until: date | None = None) -> list[MetricValue]:
    """Weekly load, monotony and strain for the ISO week start (Monday) .. end (Sunday).

    `known_until`: last day with data; a week still in progress gets its load so far, but no monotony or strain
    (the days still to come would count as rest days)."""
    _formula()
    in_week = [f for f in feedback if start <= f[0] <= end]
    loads: dict[tuple[date, str], float] = {}
    recorded = {(f[0], f[1]) for f in in_week}
    for f in in_week:
        if _usable(f):
            loads[(f[0], f[1])] = loads.get((f[0], f[1]), 0.0) + float(f[2]) * f[3]
    recorded |= {(d, "strength") for d in strength_days if start <= d <= end}
    recorded |= {(d, "run") for d in run_days if start <= d <= end}
    if not recorded:
        return []
    missing = sorted(f"{d.isoformat()}:{k}" for d, k in recorded if (d, k) not in loads)
    n_rec, n_load = len(recorded), len(recorded) - len(missing)
    dq = n_load / n_rec
    total = sum(loads.values())
    base = {"sessions_with_load": n_load, "sessions_recorded": n_rec, "missing_feedback": missing}

    def mv(metric: str, value: float | None, unit: str, extra: dict | None = None) -> MetricValue:
        return MetricValue(metric, 1, "global", start, end, value, unit, "ESTIMATE", n_rec, dq=dq,
                           detail=base | (extra or {}))

    daily = np.array([sum(v for (d, _), v in loads.items() if d == start + timedelta(days=i)) for i in range(7)])
    reason, monotony = None, None
    if known_until is not None and known_until < end:
        reason = "week_in_progress"
    elif missing:
        reason = "missing_feedback"
    else:
        sd = float(daily.std(ddof=0 if p("monotony_sd") == "population" else 1))
        if sd == 0:
            reason = "sd_zero"
        else:
            monotony = float(daily.mean()) / sd
    extra = {"reason": reason} if reason else None
    return [mv("training_load_week", total, "AU"),
            mv("training_monotony_week", monotony, "ratio", extra),
            mv("training_strain_week", None if monotony is None else total * monotony, "AU", extra)]
