"""Running personal records and the timeline of all records (strength and running).

Pure functions. A run on day D is compared only with runs on earlier days (no look-ahead); the first run of a
category is the baseline, not a record; of several runs on one day only the best can be a record.
- run:distance / run:duration: longest distance / elapsed time ever.
- run:pace@<X>km:<env>: fastest average pace of a whole run of at least X km (moving time when present), per
  environment. Only totals are recorded (no splits): this is not a best effort within a run.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from askesis.reference import exercise_label

from .base import MetricValue
from .params import p
from .review import LABEL, _fmt
from .running import mmss
from .training import RunRow


def _pace(r: RunRow) -> float:
    return (r.moving_s or r.elapsed_s) / (r.distance_m / 1000)


def run_records(runs: list[RunRow], start: date, end: date) -> list[MetricValue]:
    by_day: dict[date, list[RunRow]] = defaultdict(list)
    for r in runs:
        if r.distance_m > 0 and r.elapsed_s > 0:
            by_day[r.day].append(r)
    days = sorted(by_day)
    out: list[MetricValue] = []
    for i, d in enumerate(days):
        if not start <= d <= end or i == 0:
            continue  # the first day is the baseline for distance and duration
        today = by_day[d]
        hist = [r for pd in days[:i] for r in by_day[pd]]
        n = len(hist) + len(today)
        far = max(today, key=lambda r: r.distance_m)
        if far.distance_m > max(r.distance_m for r in hist):
            out.append(MetricValue("run_record", 1, "run:distance", d, d, far.distance_m / 1000, "km",
                                   "MEASUREMENT", n,
                                   detail={"previous_km": max(r.distance_m for r in hist) / 1000}))
        long = max(today, key=lambda r: r.elapsed_s)
        if long.elapsed_s > max(r.elapsed_s for r in hist):
            out.append(MetricValue("run_record", 1, "run:duration", d, d, long.elapsed_s / 60, "min",
                                   "MEASUREMENT", n, detail={"previous_min": max(r.elapsed_s for r in hist) / 60}))
    for env in sorted({r.environment or "outdoor" for r in runs}):
        for x in p("run_record_pace_min_km"):
            elig = [r for r in runs if (r.environment or "outdoor") == env and r.distance_m >= x * 1000
                    and r.elapsed_s > 0]
            eld = sorted({r.day for r in elig})
            for i, d in enumerate(eld):
                if i == 0 or not start <= d <= end:
                    continue  # the first eligible run is the baseline
                best = min(_pace(r) for r in elig if r.day == d)
                prev = min(_pace(r) for r in elig if r.day < d)
                if best < prev:
                    out.append(MetricValue("run_record", 1, f"run:pace@{x}km:{env}", d, d, best, "s/km",
                                           "MEASUREMENT", sum(1 for r in elig if r.day <= d),
                                           detail={"previous_s_km": prev}))
    return sorted(out, key=lambda m: (m.period_start, m.subject))


def _kg(v: float) -> str:
    return _fmt(v, 1).removesuffix(",0")


def _strength_text(m: MetricValue) -> str:
    key, kind = m.subject.removeprefix("exercise:").rsplit(":", 1)
    name = exercise_label(key)
    if kind == "load":
        return f"{name}: carico più alto, {_kg(m.value)} kg (prima {_kg(m.detail['previous'])} kg)"
    if kind == "e1rm":
        return f"{name}: e1RM stimato {_fmt(m.value)} kg (prima {_fmt(m.detail['previous_kg'])} kg)"
    load = _kg(m.detail["load_kg"])
    return f"{name}: {m.value:g} ripetizioni a {load} kg (prima {m.detail['previous_reps']:g})"


def _run_text(m: MetricValue) -> str:
    parts = m.subject.split(":")
    if parts[1] == "distance":
        return f"Corsa: distanza più lunga, {_fmt(m.value)} km (prima {_fmt(m.detail['previous_km'])} km)"
    if parts[1] == "duration":
        return f"Corsa: durata più lunga, {round(m.value)} min (prima {round(m.detail['previous_min'])} min)"
    km, env = parts[1].removeprefix("pace@").removesuffix("km"), parts[2]
    text = (f"Corsa: passo medio più veloce su almeno {km} km, {mmss(m.value)}/km "
            f"(prima {mmss(m.detail['previous_s_km'])}/km)")
    return text + (" · tapis roulant" if env == "treadmill" else "")


def timeline(values: list[MetricValue]) -> list[dict]:
    items = []
    for m in values:
        if m.metric_id == "strength_record":
            kind, text = "forza", _strength_text(m)
        elif m.metric_id == "run_record":
            kind, text = "corsa", _run_text(m)
        else:
            continue
        items.append({"date": m.period_start.isoformat(), "kind": kind, "text": text, "ref": m.ref,
                      "epistemic": LABEL[m.epistemic], "_k": m.subject})
    items.sort(key=lambda i: (i["date"], i["_k"]), reverse=True)
    for i in items:
        del i["_k"]
    return items
