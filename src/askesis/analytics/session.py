"""Per-session strength metrics: best e1RM per exercise, change vs the previous session, personal records.

Pure functions over set rows. A session on day D is compared only with sessions before D (no look-ahead).
- e1rm_session: best e1RM of the exercise in the session (working sets with external load; lower bound without RIR).
- e1rm_session_change: vs the previous session with that exercise; the noise is the spread of the exercise's past
  session-to-session changes (at least noise_min_points), minimal difference = minimal_difference_z × SD, never below
  the e1RM effect of e1rm_session_noise_floor_reps reps at the session's top load.
- best_reps_session: most reps at the heaviest load, when no e1RM is available (bodyweight, or more reps than the
  e1RM formula allows), with the previous session at that same load.
- strength_record: load (heaviest working load), reps at a given load (more reps than ever at that load), e1RM. A
  record needs at least one earlier session with the exercise: the first session sets the baseline, it is not a record.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

import numpy as np

from .base import MetricValue
from .params import p
from .training import WORK_TYPES, SetRow, e1rm, rir_of


def _key(s: SetRow) -> str:
    return s.exercise_id or f"raw:{s.exercise_raw.lower()}"


def _work(sets: list[SetRow]) -> list[SetRow]:
    return [s for s in sets if s.set_type in WORK_TYPES]


def _best(sets: list[SetRow]) -> tuple[float, bool, SetRow] | None:
    """Best e1RM among the sets: (value, lower_bound, set)."""
    best = None
    for s in sets:
        est = e1rm(s)
        if est and (best is None or est[0] > best[0]):
            best = (est[0], est[1], s)
    return best


def _sessions(sets: list[SetRow]) -> dict[str, dict[date, list[SetRow]]]:
    """exercise → day → working sets (several sessions on one day are merged)."""
    out: dict[str, dict[date, list[SetRow]]] = defaultdict(lambda: defaultdict(list))
    for s in _work(sets):
        out[_key(s)][s.day].append(s)
    return out


def session_metrics(sets: list[SetRow], start: date, end: date) -> list[MetricValue]:
    out: list[MetricValue] = []
    by_ex = _sessions(sets)
    for key, days in sorted(by_ex.items()):
        ordered = sorted(days)
        bests = {d: _best(days[d]) for d in ordered}
        subj = f"exercise:{key}"
        for i, d in enumerate(ordered):
            if not start <= d <= end:
                continue
            today, prev_days = days[d], ordered[:i]
            b = bests[d]
            if b is not None:
                val, lower, s = b
                out.append(MetricValue("e1rm_session", 1, subj, d, d, val, "kg", "ESTIMATE", len(today), detail={
                    "lower_bound": lower, "load_kg": s.load_kg, "reps": s.reps, "rir": rir_of(s),
                    "formula": p("e1rm_formula")}))
            else:  # bodyweight, or too many reps for an e1RM: the most reps at the heaviest load
                load = max(x.load_kg for x in today)
                reps = max(x.reps for x in today if x.load_kg == load)
                prev = [x for pd in prev_days for x in days[pd] if x.load_kg == load]
                detail = {"load_kg": load, "load_kind": today[0].load_kind}
                if prev:
                    last = max(pd for pd in prev_days if any(x.load_kg == load for x in days[pd]))
                    detail |= {"previous_day": last.isoformat(),
                               "previous_reps": max(x.reps for x in days[last] if x.load_kg == load)}
                out.append(MetricValue("best_reps_session", 1, subj, d, d, reps, "reps", "MEASUREMENT", len(today),
                                       detail=detail))
            # change vs the previous session with an e1RM
            with_e1rm = [x for x in prev_days if bests[x] is not None]
            if b is not None and with_e1rm:
                pd = with_e1rm[-1]
                pv, plower, _ = bests[pd]
                diff = b[0] - pv
                series = [bests[x][0] for x in with_e1rm]
                past = [y - x for x, y in zip(series[:-1], series[1:], strict=True)]
                detail = {"previous_day": pd.isoformat(), "previous_kg": pv, "noise_points": len(past),
                          "lower_bound": bool(b[1] or plower)}
                md = None
                if len(past) >= p("noise_min_points"):
                    floor = p("e1rm_session_noise_floor_reps") * b[2].load_kg / 30  # Epley: one rep at that load
                    md = max(p("minimal_difference_z") * float(np.std(past, ddof=1)), floor)
                    detail |= {"minimal_difference_kg": md, "floor_kg": floor}
                else:
                    detail["reason"] = "noise_not_estimable"
                out.append(MetricValue("e1rm_session_change", 1, subj, d, d, diff, "kg", "ESTIMATE", len(series) + 1,
                                       lo=None if md is None else diff - md, hi=None if md is None else diff + md,
                                       detail=detail))
            if not prev_days:
                continue  # the first session is the baseline, not a record
            history = [x for pd in prev_days for x in days[pd]]
            heaviest = max(s.load_kg for s in today)
            prev_heaviest = max(s.load_kg for s in history)
            if today[0].load_kind == "external" and heaviest > prev_heaviest:
                out.append(MetricValue("strength_record", 1, f"{subj}:load", d, d, heaviest, "kg", "MEASUREMENT",
                                       len(history) + len(today), detail={"previous": prev_heaviest}))
            for load in sorted({s.load_kg for s in today}):
                before = [s.reps for s in history if s.load_kg == load]
                reps = max(s.reps for s in today if s.load_kg == load)
                if before and reps > max(before):
                    out.append(MetricValue("strength_record", 1, f"{subj}:reps@{load:g}", d, d, reps, "reps",
                                           "MEASUREMENT", len(before) + 1,
                                           detail={"load_kg": load, "previous_reps": max(before)}))
            prev_e1rm = [bests[x] for x in prev_days if bests[x] is not None]
            if b is not None and not b[1] and prev_e1rm and b[0] > max(x[0] for x in prev_e1rm):
                out.append(MetricValue("strength_record", 1, f"{subj}:e1rm", d, d, b[0], "kg", "ESTIMATE",
                                       len(prev_e1rm) + 1, detail={"previous_kg": max(x[0] for x in prev_e1rm)}))
    return out
