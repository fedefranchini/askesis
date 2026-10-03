"""Build canonical envelopes from parsed manual-entry intents.

Day-reference convention for the fast daily line:
- `cibo` and `passi` refer to the PREVIOUS day by default (complete daily totals);
- everything else refers to the entry date (default: today).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from askesis.config import Config
from askesis.core.ids import new_id
from askesis.core.timeutil import at_local
from askesis.parsers.text import Intent
from askesis.reference import find_exercise

NOON = time(12, 0)


def _env(entity, cfg: Config, now: datetime, day: date, payload: dict, **times) -> dict:
    rec = dict(
        id=new_id(), entity_type=entity, tz=cfg.timezone, local_date=day, recorded_at=now,
        source_id=cfg.source_id, entry_method="manual", payload=payload,
    )
    rec.update({k: v for k, v in times.items() if v is not None})
    return rec


def _session_start(day: date, cfg: Config, now: datetime) -> tuple[datetime, bool]:
    """Exact time unknown in the fast line: use now if logging the same day, otherwise local noon."""
    if now.astimezone(at_local(day, NOON, cfg.timezone).tzinfo).date() == day:
        return now, True
    return at_local(day, NOON, cfg.timezone), True


def build(intent: Intent, cfg: Config, day: date, now: datetime) -> list[dict]:
    k, d = intent.kind, intent.data
    if k == "weight":
        return [_env("body_weight", cfg, now, day, {"value_kg": d["value_kg"], "fasted": True},
                     occurred_at=at_local(day, cfg.weigh_time, cfg.timezone),
                     original_values={"time_assumed": True})]
    if k == "food":
        target = day - timedelta(days=1) if d.get("when", "ieri") == "ieri" else day
        payload = {"energy_kcal": d["energy_kcal"], "completeness": "partial" if d.get("partial") else "complete",
                   "logging_method": "app_estimated"}
        if d.get("protein_g") is not None:
            payload["protein_g"] = d["protein_g"]
        return [_env("nutrition_day", cfg, now, target, payload,
                     occurred_at=at_local(target, NOON, cfg.timezone))]
    if k == "waist":
        return [_env("body_measurement", cfg, now, day, {"site": "waist_navel", "readings_cm": d["readings_cm"]},
                     occurred_at=at_local(day, cfg.weigh_time, cfg.timezone),
                     original_values={"time_assumed": True})]
    if k == "gym":
        start, assumed = _session_start(day, cfg, now)
        session = _env("training_session", cfg, now, day, {"session_kind": "strength"}, start_at=start,
                       original_values={"time_assumed": assumed})
        out, seq = [session], 0
        for ex in d["exercises"]:
            ref = find_exercise(ex.name)
            for s in ex.sets:
                seq += 1
                payload = {"session_id": session["id"], "exercise_raw": ex.name, "sequence": seq,
                           "set_type": s.set_type, "load_kg": s.load_kg, "reps": s.reps}
                load_kind = s.load_kind
                if ref:
                    payload["exercise_id"] = ref["id"]
                    if ref.get("load_kind") == "bodyweight" and load_kind == "external" and s.load_kg == 0:
                        load_kind = "bodyweight"
                payload["load_kind"] = load_kind
                if s.rir is not None:
                    payload["rir"] = s.rir
                if s.rpe is not None:
                    payload["rpe"] = s.rpe
                out.append(_env("set_record", cfg, now, day, payload, start_at=start))
        return out
    if k == "run":
        start, assumed = _session_start(day, cfg, now)
        return [_env("running_session", cfg, now, day, dict(d), start_at=start,
                     end_at=start + timedelta(seconds=d["elapsed_s"]), original_values={"time_assumed": assumed})]
    if k == "steps":
        target = day - timedelta(days=1)
        return [_env("daily_activity", cfg, now, target, {"steps": d["steps"]},
                     occurred_at=at_local(target, NOON, cfg.timezone))]
    if k == "sleep":
        wake = at_local(day, cfg.weigh_time, cfg.timezone)
        return [_env("sleep_session", cfg, now, day, {"asleep_s": d["asleep_s"]},
                     start_at=wake - timedelta(seconds=d["asleep_s"]), end_at=wake,
                     original_values={"times_estimated": True})]
    if k == "rhr":
        return [_env("resting_hr_daily", cfg, now, day, {"bpm": d["bpm"], "method": "manual"},
                     occurred_at=at_local(day, cfg.weigh_time, cfg.timezone))]
    if k == "pain":
        return [_env("subjective_checkin", cfg, now, day, {"pain": [d]},
                     occurred_at=at_local(day, NOON, cfg.timezone))]
    if k == "context":
        return [_env("daily_context", cfg, now, day, {"key": d["key"], "value": d["value"]},
                     occurred_at=at_local(day, NOON, cfg.timezone))]
    raise ValueError(f"intent sconosciuto: {k}")
