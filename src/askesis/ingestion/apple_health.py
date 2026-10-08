"""Apple Health export (export.zip) → envelopes. Streaming parser: the XML is never extracted to disk.

Mapping and rules (docs/roadmap.md F4b):
- body mass → body_weight; waist circumference → body_measurement (waist_navel); resting HR → resting_hr_daily;
- heart rate variability (SDNN, taken by the Watch at irregular times) → hrv_daily: mean of the day's samples, with
  their number (reduced validity: compare only with itself);
- steps → daily_activity: iPhone and Watch count the same steps, so per day the source with the highest total is
  used, never the sum (engineering choice);
- sleep → sleep_session on the wake day: the source with sleep stages is preferred; asleep and in-bed time kept apart;
  overlapping samples are merged (union of intervals);
- running workouts → running_session; strength workouts → training_session (duration only: no sets in Health);
- dietary energy/protein (e.g. a nutrition app writing to Health) → nutrition_day on the nutrition day (cutoff), as
  `partial`: Health does not say whether a day was fully logged.
Conflicts: a record of another source (e.g. manual) for the same entity and day wins; a changed daily total from a
later export becomes an append-only correction (supersedes). Days not closed at export time are excluded.
Re-importing the same file is idempotent (natural-key fingerprints as source_record_id).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from xml.etree.ElementTree import iterparse

from askesis.config import Config
from askesis.core.ids import new_id
from askesis.core.timeutil import at_local, local_date, now_utc, nutrition_date, sleep_date

SOURCE_ID = "apple_health"
HK = "HKQuantityTypeIdentifier"
TYPES = {
    f"{HK}BodyMass": "weight", f"{HK}WaistCircumference": "waist", f"{HK}StepCount": "steps",
    f"{HK}RestingHeartRate": "rhr", f"{HK}DietaryEnergyConsumed": "kcal", f"{HK}DietaryProtein": "protein",
    f"{HK}HeartRateVariabilitySDNN": "hrv",
    "HKCategoryTypeIdentifierSleepAnalysis": "sleep",
}
ASLEEP = {"HKCategoryValueSleepAnalysisAsleep", "HKCategoryValueSleepAnalysisAsleepUnspecified",
          "HKCategoryValueSleepAnalysisAsleepCore", "HKCategoryValueSleepAnalysisAsleepDeep",
          "HKCategoryValueSleepAnalysisAsleepREM"}
STAGES = ASLEEP - {"HKCategoryValueSleepAnalysisAsleep", "HKCategoryValueSleepAnalysisAsleepUnspecified"}
IN_BED = "HKCategoryValueSleepAnalysisInBed"
RUNNING = "HKWorkoutActivityTypeRunning"
STRENGTH = {"HKWorkoutActivityTypeTraditionalStrengthTraining", "HKWorkoutActivityTypeFunctionalStrengthTraining"}
TO_KG = {"kg": 1.0, "lb": 0.45359237, "g": 0.001}
TO_CM = {"cm": 1.0, "m": 100.0, "in": 2.54, "mm": 0.1}
TO_M = {"m": 1.0, "km": 1000.0, "mi": 1609.344, "ft": 0.3048}
TO_KCAL = {"kcal": 1.0, "Cal": 1.0, "kJ": 1 / 4.184, "cal": 0.001}
TO_S = {"min": 60.0, "s": 1.0, "hr": 3600.0, "h": 3600.0}


def parse_dt(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")


@dataclass
class HealthData:
    export_at: datetime | None = None
    samples: dict[str, list[tuple]] = field(default_factory=lambda: defaultdict(list))
    workouts: list[dict] = field(default_factory=list)
    not_imported: Counter = field(default_factory=Counter)


def _export_xml(zf: zipfile.ZipFile) -> str:
    """Main XML of the export. Its name is localised (export.xml, "dati esportati.xml", …): take the largest XML
    in the export folder, excluding the clinical CDA document."""
    infos = [i for i in zf.infolist() if i.filename.lower().endswith(".xml") and "cda" not in i.filename.lower()
             and i.filename.count("/") <= 1]
    if not infos:
        raise ValueError("file XML principale non trovato nello zip: è un'esportazione di Salute?")
    return max(infos, key=lambda i: i.file_size).filename


def parse(path: Path) -> HealthData:
    data = HealthData()
    with zipfile.ZipFile(path) as zf, zf.open(_export_xml(zf)) as fh:
        stack_workout = None
        for event, el in iterparse(fh, events=("start", "end")):
            tag = el.tag
            if event == "start":
                if tag == "Workout":
                    stack_workout = el
                continue
            if tag == "ExportDate":
                data.export_at = parse_dt(el.get("value"))
            elif tag == "Record":
                kind = TYPES.get(el.get("type", ""))
                if kind is None:
                    data.not_imported[el.get("type", "?")] += 1
                else:
                    data.samples[kind].append((parse_dt(el.get("startDate")), parse_dt(el.get("endDate")),
                                               el.get("value"), el.get("unit"), el.get("sourceName", "?")))
                if stack_workout is None:
                    el.clear()
            elif tag == "Workout":
                data.workouts.append(_workout(el))
                stack_workout = None
                el.clear()
    return data


def _workout(el) -> dict:
    stats = {s.get("type", ""): s for s in el.iter("WorkoutStatistics")}
    dist = stats.get(f"{HK}DistanceWalkingRunning")
    hr = stats.get(f"{HK}HeartRate")
    distance_m = None
    if dist is not None and dist.get("sum"):
        distance_m = float(dist.get("sum")) * TO_M.get(dist.get("unit", "km"), 1000.0)
    elif el.get("totalDistance"):
        distance_m = float(el.get("totalDistance")) * TO_M.get(el.get("totalDistanceUnit", "km"), 1000.0)
    start, end = parse_dt(el.get("startDate")), parse_dt(el.get("endDate"))
    dur = el.get("duration")
    return {"type": el.get("workoutActivityType"), "start": start, "end": end, "source": el.get("sourceName", "?"),
            "elapsed_s": round(float(dur) * TO_S.get(el.get("durationUnit", "min"), 60.0)) if dur
            else round((end - start).total_seconds()),
            "distance_m": distance_m,
            "avg_hr": round(float(hr.get("average"))) if hr is not None and hr.get("average") else None,
            "max_hr": round(float(hr.get("maximum"))) if hr is not None and hr.get("maximum") else None}


def _overlap(a: dict, b: dict) -> float:
    inter = (min(a["end"], b["end"]) - max(a["start"], b["start"])).total_seconds()
    shorter = min((a["end"] - a["start"]).total_seconds(), (b["end"] - b["start"]).total_seconds()) or 1
    return max(0.0, inter) / shorter


def _richness(w: dict) -> tuple:
    return (w["avg_hr"] is not None, w["distance_m"] is not None, w["elapsed_s"])


def dedup_workouts(workouts: list[dict], min_overlap: float = 0.5) -> list[dict]:
    """Apps that also write to Health (e.g. a lifting or running app alongside the Watch) create the same session
    twice. Workouts of the same kind overlapping for at least half of the shorter one are one session: the richest
    copy is kept (heart rate, then distance, then duration)."""
    kept: list[dict] = []
    for w in sorted(workouts, key=_richness, reverse=True):
        same = [k for k in kept if k["type"] == w["type"] or {k["type"], w["type"]} <= STRENGTH]
        if any(_overlap(k, w) >= min_overlap for k in same):
            continue
        kept.append(w)
    return sorted(kept, key=lambda w: w["start"])


def _fp(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]


def _union_seconds(intervals: list[tuple[datetime, datetime]]) -> int:
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return round(total)


@dataclass
class Plan:
    records: list[dict] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)  # (entity, outcome) → n


def build(data: HealthData, cfg: Config, conn: sqlite3.Connection, since: date | None = None,
          now: datetime | None = None, exclude_sources: set[str] | None = None) -> Plan:
    now = now or now_utc()
    if exclude_sources:  # e.g. a device that belongs to someone else
        excl = {x.casefold() for x in exclude_sources}
        data = HealthData(data.export_at,
                          {k: [x for x in v if x[4].casefold() not in excl] for k, v in data.samples.items()},
                          [w for w in data.workouts if w["source"].casefold() not in excl], data.not_imported)
    tz = cfg.timezone
    export_at = data.export_at or now
    open_day = local_date(export_at, tz)  # not closed at export time
    open_nutrition_day = nutrition_date(export_at, tz, cfg.nutrition_cutoff)
    plan = Plan()

    def current(entity: str, day: date) -> list[sqlite3.Row]:
        return conn.execute("SELECT id, source_id, payload_hash, payload FROM v_current WHERE entity_type = ? "
                            "AND local_date = ?", (entity, day.isoformat())).fetchall()

    def add(entity: str, day: date, payload: dict, fp: str, device: str, original: dict, daily: bool,
            last_open: date, **times) -> None:
        if since and day < since:
            return
        if day >= last_open:
            plan.counts[(entity, "escluso: giorno non chiuso all'esportazione")] += 1
            return
        rows = current(entity, day)
        others = [r for r in rows if r["source_id"] != SOURCE_ID]
        if others and (daily or entity == "body_weight" or entity == "running_session"):
            plan.counts[(entity, "saltato: esiste già un dato di un'altra sorgente")] += 1
            return
        mine = [r for r in rows if r["source_id"] == SOURCE_ID]
        rec = {"id": new_id(), "entity_type": entity, "tz": tz, "local_date": day, "recorded_at": now,
               "source_id": SOURCE_ID, "source_record_id": fp, "device_id": device, "entry_method": "imported",
               "payload": payload, "original_values": original} | times
        if daily and mine:
            if json.loads(mine[0]["payload"]) == payload:
                plan.counts[(entity, "già presente")] += 1
                return
            rec["supersedes_id"] = mine[0]["id"]
            plan.counts[(entity, "correzione di un valore importato prima")] += 1
        else:
            if conn.execute("SELECT 1 FROM raw_record WHERE source_id = ? AND source_record_id = ?",
                            (SOURCE_ID, fp)).fetchone():
                plan.counts[(entity, "già presente")] += 1
                return
            plan.counts[(entity, "nuovo")] += 1
        plan.records.append(rec)

    def noon(d: date) -> datetime:
        return at_local(d, time(12, 0), tz)

    for s, _e, v, unit, src in data.samples.get("weight", []):
        kg = round(float(v) * TO_KG.get(unit, 1.0), 2)
        add("body_weight", local_date(s, tz), {"value_kg": kg}, _fp("weight", s, v, unit, src), src,
            {"value": v, "unit": unit}, False, open_day + timedelta(days=1), occurred_at=s)
    for s, _e, v, unit, src in data.samples.get("waist", []):
        cm = round(float(v) * TO_CM.get(unit, 1.0), 1)
        add("body_measurement", local_date(s, tz), {"site": "waist_navel", "readings_cm": [cm]},
            _fp("waist", s, v, unit, src), src, {"value": v, "unit": unit}, False, open_day + timedelta(days=1),
            occurred_at=s)

    steps: dict[date, Counter] = defaultdict(Counter)
    for s, _e, v, _unit, src in data.samples.get("steps", []):
        steps[local_date(s, tz)][src] += float(v)
    for day, by_src in steps.items():
        src, total = max(by_src.items(), key=lambda kv: kv[1])
        add("daily_activity", day, {"steps": round(total)}, _fp("steps", day, src, round(total)), src,
            {"per_source": {k: round(x) for k, x in by_src.items()}, "rule": "max_source_per_day"}, True, open_day,
            occurred_at=noon(day))

    rhr: dict[date, tuple] = {}
    for s, _e, v, _unit, src in data.samples.get("rhr", []):
        d = local_date(s, tz)
        if d not in rhr or s > rhr[d][0]:
            rhr[d] = (s, round(float(v)), src)
    for day, (_s, bpm, src) in rhr.items():
        add("resting_hr_daily", day, {"bpm": bpm}, _fp("rhr", day, bpm, src), src, {"value": bpm}, True, open_day,
            occurred_at=noon(day))

    hrv: dict[date, list[tuple[float, str]]] = defaultdict(list)
    for s, _e, v, _unit, src in data.samples.get("hrv", []):
        hrv[local_date(s, tz)].append((float(v), src))
    for day, xs in hrv.items():
        mean = round(sum(v for v, _ in xs) / len(xs), 1)
        srcs = ",".join(sorted({src for _, src in xs}))
        add("hrv_daily", day, {"sdnn_ms": mean, "n_samples": len(xs), "method": "apple_sdnn_daily_mean"},
            _fp("hrv", day, mean, len(xs), srcs), srcs, {"values_ms": [v for v, _ in xs]}, True, open_day,
            occurred_at=noon(day))

    nights: dict[date, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {"asleep": [], "bed": [],
                                                                                  "stages": False}))
    for s, e, v, _unit, src in data.samples.get("sleep", []):
        n = nights[sleep_date(e, tz)][src]
        if v in ASLEEP:
            n["asleep"].append((s, e))
            n["stages"] = n["stages"] or v in STAGES
        elif v == IN_BED:
            n["bed"].append((s, e))
    for day, by_src in nights.items():
        src, n = max(by_src.items(), key=lambda kv: (kv[1]["stages"], _union_seconds(kv[1]["asleep"])))
        intervals = n["asleep"] + n["bed"]
        payload = {"asleep_s": _union_seconds(n["asleep"]) or None, "in_bed_s": _union_seconds(n["bed"]) or None}
        add("sleep_session", day, {k: v for k, v in payload.items() if v is not None},
            _fp("sleep", day, src, payload), src, {"stages": n["stages"]}, True, open_day + timedelta(days=1),
            start_at=min(i[0] for i in intervals), end_at=max(i[1] for i in intervals))

    food: dict[date, dict] = defaultdict(lambda: {"kcal": 0.0, "protein": 0.0, "sources": set(), "n": 0})
    for kind, field_ in (("kcal", "kcal"), ("protein", "protein")):
        for s, _e, v, unit, src in data.samples.get(kind, []):
            d = nutrition_date(s, tz, cfg.nutrition_cutoff)
            amount = float(v) * (TO_KCAL.get(unit, 1.0) if kind == "kcal" else 1.0)
            food[d][field_] += amount
            food[d]["sources"].add(src)
            food[d]["n"] += 1
    for day, f in food.items():
        payload = {"energy_kcal": round(f["kcal"]), "completeness": "partial",
                   "logging_method": "via_apple_health"}
        if f["protein"]:
            payload["protein_g"] = round(f["protein"], 1)
        add("nutrition_day", day, payload, _fp("food", day, payload), ",".join(sorted(f["sources"])),
            {"samples": f["n"], "completeness_note": "non verificabile da Salute"}, True, open_nutrition_day,
            occurred_at=noon(day))

    for w in dedup_workouts(data.workouts):
        day = local_date(w["start"], tz)
        if w["type"] == RUNNING and w["distance_m"]:
            payload = {"distance_m": round(w["distance_m"], 1), "elapsed_s": w["elapsed_s"]}
            payload |= {k: w[k] for k in ("avg_hr", "max_hr") if w[k]}
            add("running_session", day, payload, _fp("run", w["start"], w["end"], w["source"]), w["source"],
                {"workout": w["type"]}, False, open_day + timedelta(days=1), start_at=w["start"], end_at=w["end"])
        elif w["type"] in STRENGTH:
            add("training_session", day, {"session_kind": "strength"},
                _fp("strength", w["start"], w["end"], w["source"]), w["source"],
                {"workout": w["type"], "elapsed_s": w["elapsed_s"], "note": "da Salute: nessuna serie"}, False,
                open_day + timedelta(days=1), start_at=w["start"], end_at=w["end"])
        else:
            data.not_imported[w["type"] or "Workout"] += 1
    return plan
