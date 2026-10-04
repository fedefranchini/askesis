"""Compact read-back of what was understood, shown before/after saving (e.g. "Panca piana: 80 kg × 8 @RIR 2")."""

from __future__ import annotations

from datetime import date

from askesis.core.units import format_duration
from askesis.reference import find_exercise

NEEDS_CONFIRMATION = {"training_session", "set_record", "running_session"}


def _n(v: float, nd: int = 1) -> str:
    s = f"{v:,.{nd}f}".rstrip("0").rstrip(".") if isinstance(v, float) and v != int(v) else f"{int(v):,}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _d(d) -> str:
    d = d if isinstance(d, date) else date.fromisoformat(str(d))
    return f"{d.day}/{d.month}"


def _set(p: dict) -> str:
    kind = p.get("load_kind", "external")
    load = {"bodyweight": "corpo libero", "bodyweight_plus": f"corpo +{_n(p['load_kg'])} kg",
            "assisted": f"assistito −{_n(p['load_kg'])} kg"}.get(kind, f"{_n(p['load_kg'])} kg")
    effort = f" @RIR {_n(p['rir'])}" if "rir" in p else (f" @RPE {_n(p['rpe'])}" if "rpe" in p else " (RIR ?)")
    warm = " [riscaldamento]" if p.get("set_type") == "warmup" else ""
    return f"{load} × {p['reps']}{effort}{warm}"


def lines(records: list[dict]) -> list[str]:
    """Group records into compact human-readable lines, in input order."""
    out: list[str] = []
    exercises: dict[str, list[str]] = {}
    order: list[str] = []
    for r in records:
        e, p, d = r["entity_type"], r["payload"], r["local_date"]
        if e == "set_record":
            ref = find_exercise(p["exercise_raw"])
            name = ref.get("name_it", ref["name"]) if ref else f"{p['exercise_raw']} (non in catalogo)"
            if name not in exercises:
                exercises[name] = []
                order.append(name)
            exercises[name].append(_set(p))
            continue
        if e == "body_weight":
            out.append(f"Peso {_d(d)}: {_n(p['value_kg'], 2)} kg" + (" a digiuno" if p.get("fasted") else ""))
        elif e == "nutrition_day":
            prot = f", {_n(p['protein_g'], 0)} g proteine" if "protein_g" in p else ""
            tag = " (giornata parziale)" if p.get("completeness") == "partial" else ""
            out.append(f"Cibo {_d(d)}: {_n(p.get('energy_kcal', 0), 0)} kcal{prot}{tag}")
        elif e == "body_measurement":
            out.append(f"Vita {_d(d)}: " + " · ".join(f"{_n(x)} cm" for x in p["readings_cm"]))
        elif e == "training_session":
            out.append(f"Sessione pesi {_d(d)}:")
        elif e == "running_session":
            pace = p["elapsed_s"] / (p["distance_m"] / 1000)
            hr = f", FC media {p['avg_hr']}" if "avg_hr" in p else ""
            stop = f", stop: {p['stop_reason']}" if "stop_reason" in p else ""
            out.append(f"Corsa {_d(d)}: {_n(p['distance_m'] / 1000, 2)} km in {format_duration(p['elapsed_s'])} "
                       f"({format_duration(pace)}/km){hr}{stop}")
        elif e == "daily_activity":
            out.append(f"Passi {_d(d)}: {_n(p['steps'], 0)}")
        elif e == "sleep_session":
            out.append(f"Sonno (risveglio {_d(d)}): {format_duration(p['asleep_s'])}")
        elif e == "resting_hr_daily":
            out.append(f"FC a riposo {_d(d)}: {p['bpm']}")
        elif e == "subjective_checkin":
            pains = "; ".join(f"dolore {x.get('region')} {_n(x.get('score_0_10'))}/10" for x in p.get("pain", []) or [])
            out.append(f"Check-in {_d(d)}: " + (pains or ", ".join(f"{k} {v}" for k, v in p.items())))
        elif e == "daily_context":
            out.append(f"Contesto {_d(d)}: {p['key']} = {p['value']}")
        else:
            out.append(f"{e} {_d(d)}")
    ex_lines = [f"  {name}: " + " · ".join(exercises[name]) for name in order]
    session = next((i for i, line in enumerate(out) if line.startswith("Sessione pesi")), None)
    if session is None:
        return out + ex_lines
    return out[: session + 1] + ex_lines + out[session + 1 :]


def needs_confirmation(records: list[dict]) -> bool:
    return any(r["entity_type"] in NEEDS_CONFIRMATION for r in records)
