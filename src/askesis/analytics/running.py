"""Aerobic running efficiency: pace at equal heart rate, from comparable easy runs only.

A run is comparable when it is easy (declared run_type "easy", or no declared type and the programme in force that day
schedules an easy run on that weekday), has an average heart rate, lasts at least run_eff_min_minutes and does not
climb more than run_eff_max_elev_m_per_km (unknown elevation is allowed). Runs are compared only within the same
environment (outdoor / treadmill).
- run_efficiency: metres per heartbeat (speed in m/min ÷ average HR) of each comparable run.
- run_efficiency_change: mean EF of the last k comparable runs − mean EF of the k before, against the minimal
  difference z × √2 × SD / √k, where SD is the sample SD of EF over the comparable runs before the current window.
  Fewer than 2k runs: no metric; fewer than run_eff_min_noise_runs baseline runs: value without interval.
- run_pace_at_ref_hr: pace (s/km) of the current window at the reference HR (median avg HR of the comparable runs
  up to that run), so that a text can cite the number: 1000 / (mean EF × ref HR) min/km.
Declared assumption: within the easy range speed is taken as proportional to heart rate; heat, hydration, terrain,
fatigue and cardiac drift add noise that is not modelled; the noise is the run-to-run variability of comparable
runs, which also contains real changes (conservative).
Each run only sees comparable runs up to itself (no look-ahead).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import numpy as np

from .adherence import WEEKDAYS, plan_on
from .base import MetricValue
from .params import p
from .training import RunRow


@dataclass(frozen=True)
class Comparable:
    run: RunRow
    env: str
    easy_source: str  # "declared" | "plan"
    ef: float  # metres per heartbeat
    speed_m_min: float


def _plan_easy(plans, day: date) -> bool:
    prog = plan_on(plans, "programme", day)
    if prog is None:
        return False
    return any(s.get("day") == WEEKDAYS[day.weekday()] and (s.get("run") or {}).get("kind") == "easy"
               for s in prog.get("microcycle", []))


def comparable_runs(runs: list[RunRow], plans) -> list[Comparable]:
    """Comparable easy runs, in day order."""
    out = []
    for r in sorted(runs, key=lambda r: (r.day, r.distance_m, r.elapsed_s)):
        if r.run_type == "easy":
            source = "declared"
        elif r.run_type is None and _plan_easy(plans, r.day):
            source = "plan"
        else:
            continue
        secs = r.moving_s or r.elapsed_s
        if not r.avg_hr or secs <= 0 or r.distance_m <= 0 or secs < p("run_eff_min_minutes") * 60:
            continue
        if r.elev_gain_m is not None and r.elev_gain_m / (r.distance_m / 1000) > p("run_eff_max_elev_m_per_km"):
            continue
        speed = r.distance_m / (secs / 60)
        out.append(Comparable(r, f"env:{r.environment or 'outdoor'}", source, speed / r.avg_hr, speed))
    return out


def pace_s_km(ef: float, hr: float) -> float:
    """Seconds per km at heart rate `hr` for an efficiency `ef` (m/beat): speed = ef × hr."""
    return 60 * 1000 / (ef * hr)


def efficiency(runs: list[RunRow], plans, start: date, end: date) -> list[MetricValue]:
    k, min_noise = int(p("run_eff_window")), int(p("run_eff_min_noise_runs"))
    by_env: dict[str, list[Comparable]] = defaultdict(list)
    for c in comparable_runs([r for r in runs if r.day <= end], plans):
        by_env[c.env].append(c)
    out: list[MetricValue] = []
    for env, series in sorted(by_env.items()):
        for i, c in enumerate(series):
            if not start <= c.run.day <= end:
                continue
            day = c.run.day
            out.append(MetricValue("run_efficiency", 1, env, day, day, c.ef, "m/beat", "ESTIMATE", 1, detail={
                "speed_m_min": c.speed_m_min, "avg_hr": c.run.avg_hr, "pace_s_per_km": 60000 / c.speed_m_min,
                "easy_source": c.easy_source, "elev_known": c.run.elev_gain_m is not None}))
            upto = series[: i + 1]
            if len(upto) < 2 * k:
                continue
            now, prev, base = upto[-k:], upto[-2 * k:-k], upto[:-k]
            diff = float(np.mean([x.ef for x in now]) - np.mean([x.ef for x in prev]))
            ref_hr = float(np.median([x.run.avg_hr for x in upto]))
            now_s = pace_s_km(float(np.mean([x.ef for x in now])), ref_hr)
            prev_s = pace_s_km(float(np.mean([x.ef for x in prev])), ref_hr)
            detail = {"window": k, "runs_used": len(upto), "pace_at_ref_now_s_km": now_s,
                      "pace_at_ref_prev_s_km": prev_s, "ref_hr": ref_hr}
            md = sd = None
            if len(base) >= min_noise:
                sd = float(np.std([x.ef for x in base], ddof=1))
                md = p("minimal_difference_z") * math.sqrt(2) * sd / math.sqrt(k)
                detail |= {"sd": sd, "minimal_difference": md}
            else:
                detail["reason"] = "noise_not_estimable"
            out.append(MetricValue("run_efficiency_change", 1, env, day, day, diff, "m/beat", "ESTIMATE", len(upto),
                                   lo=None if md is None else diff - md, hi=None if md is None else diff + md,
                                   detail=detail))
            out.append(MetricValue("run_pace_at_ref_hr", 1, env, day, day, now_s, "s/km", "ESTIMATE", len(upto),
                                   detail={"ref_hr": ref_hr, "window": k, "previous_s_km": prev_s}))
    return out


def beyond_noise(m: MetricValue | None) -> str | None:
    """"better" / "worse" / "within" from the interval of run_efficiency_change; None when not estimable.
    A higher EF is a lower pace at equal HR, i.e. an improvement."""
    if m is None or m.value is None or m.lo is None or m.hi is None:
        return None
    return "better" if m.lo > 0 else "worse" if m.hi < 0 else "within"


def mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"
