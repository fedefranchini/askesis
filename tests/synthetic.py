"""Deterministic synthetic athlete (deliberately generic: not modelled on any real person)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from askesis.core.ids import new_id

ZONE = "Europe/Berlin"
START = date(2025, 3, 3)  # a Monday
HEIGHT, AGE, SEX = 165.0, 34, "female"
W0, SLOPE, KCAL, PROT = 68.0, -0.05, 1900.0, 120.0


def _env(entity, day: date, payload: dict, **times) -> dict:
    t = datetime(day.year, day.month, day.day, 6, 0, tzinfo=UTC)
    rec = dict(id=new_id(), entity_type=entity, tz=ZONE, local_date=day, recorded_at=t,
               source_id="synthetic", payload=payload)
    rec.update(times or {"occurred_at": t})
    return rec


def athlete() -> list[dict]:
    return [_env("athlete_attribute", START, {"key": k, "value": v}) for k, v in
            (("height_cm", HEIGHT), ("age_years", AGE), ("sex_for_formulas", SEX))]


def linear_weights(days: int, start: date = START, w0: float = W0, slope: float = SLOPE) -> list[dict]:
    return [_env("body_weight", start + timedelta(days=i), {"value_kg": round(w0 + slope * i, 4), "fasted": True})
            for i in range(days)]


def constant_intake(days: int, start: date = START, kcal: float = KCAL, prot: float = PROT) -> list[dict]:
    return [_env("nutrition_day", start + timedelta(days=i),
                 {"energy_kcal": kcal, "protein_g": prot, "completeness": "complete"}) for i in range(days)]


def strength_session(day: date) -> list[dict]:
    t = datetime(day.year, day.month, day.day, 17, 0, tzinfo=UTC)
    session = _env("training_session", day, {"session_kind": "strength"}, start_at=t)
    sets = [("bench_press", "panca", 50.0, 8, 2), ("bench_press", "panca", 50.0, 8, 2),
            ("bench_press", "panca", 50.0, 7, 1), ("bench_press", "panca", 50.0, 6, None),
            ("back_squat", "squat", 60.0, 5, 3), ("back_squat", "squat", 30.0, 10, None)]
    out = [session]
    for i, (ex, raw, load, reps, rir) in enumerate(sets, start=1):
        p = {"session_id": session["id"], "exercise_id": ex, "exercise_raw": raw, "sequence": i,
             "set_type": "warmup" if load == 30.0 else "working", "load_kg": load, "reps": reps}
        if rir is not None:
            p["rir"] = rir
        out.append(_env("set_record", day, p, start_at=t))
    return out


def run(day: date, km: float, secs: int, hr: int) -> dict:
    t = datetime(day.year, day.month, day.day, 7, 0, tzinfo=UTC)
    return _env("running_session", day, {"distance_m": km * 1000, "elapsed_s": secs, "avg_hr": hr}, start_at=t,
                end_at=t + timedelta(seconds=secs))
