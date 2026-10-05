"""Values derived at activation: declared (formula + data cut-off) when an intervention is proposed, computed and
frozen when it is activated.

A pre-registration can declare, e.g., "start weight = weight_ma7@1 on Sunday; energy target = adaptive_tdee@1
minus the deficit for a declared %BW/week". The formulas are part of the frozen pre-registration (hash at proposal).
The numbers are computed by `compute` at activation, from data up to `data_until` only (no look-ahead), and stored
in the append-only `activated` event with their own hash: after that they never change.

Plan contents refer to a derived value with the string "$derived:<name>".
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from askesis.analytics import engine
from askesis.analytics.params import p
from askesis.core.timeutil import iso, now_utc

PLACEHOLDER = "$derived:"


class Derivation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    method: Literal["metric", "energy_target", "energy_start", "protein_target"]
    args: dict
    unit: str
    description: str


class DerivedSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_until: date  # last day of data used; activation happens from the day after
    values: list[Derivation]


class DerivationError(ValueError):
    pass


def _round(v: float, step: float | None) -> float:
    return round(v / step) * step if step else v


def compute(
    conn: sqlite3.Connection, spec: DerivedSpec, data_until: date | None = None, cutoff: datetime | None = None,
    preview: bool = False,
) -> dict:
    """Compute every derived value from data dated ≤ data_until and known at `cutoff`.

    `data_until` defaults to the spec's. A preview (provisional values shown with the proposal) passes the data
    known so far and may use `args.preview_fallback` (another metric, latest value) when the declared metric is not
    yet computable; activation never does."""
    until = data_until or spec.data_until
    inp = engine.load_inputs(conn, cutoff)
    metrics = engine.compute(inp, until - timedelta(days=56), until) if inp.dates else []
    out: dict[str, dict] = {}

    def ref(name: str) -> dict:
        if name not in out:
            raise DerivationError(f"valore derivato '{name}' usato prima di essere definito")
        return out[name]

    for d in spec.values:
        a = d.args
        if d.method == "metric":
            match = [
                m
                for m in metrics
                if m.metric_id == a["metric"] and m.version == a.get("version", 1)
                and m.subject == a.get("subject", "global") and m.period_end == until
            ]
            value = getattr(match[0], a.get("field", "value")) if match else None
            source = f"{a['metric']}@{a.get('version', 1)}"
            for fallback in ([a["metric"], a.get("preview_fallback")] if preview and value is None else []):
                fb = [m for m in metrics if fallback and m.metric_id == fallback and m.period_end <= until
                      and m.subject == a.get("subject", "global") and getattr(m, a.get("field", "value")) is not None]
                if fb:  # preview only: latest available value of the metric, then of the declared fallback
                    match = [max(fb, key=lambda m: m.period_end)]
                    value = getattr(match[0], a.get("field", "value"))
                    source = f"{fallback}@{match[0].version} (anteprima: ultimo valore disponibile)"
                    break
            if value is None and a.get("fallback"):  # declared in the pre-registration, so frozen like the rest
                value, source, match = a["fallback"]["value"], f"{a['fallback']['source']} (ripiego dichiarato)", []
            if value is None:
                raise DerivationError(f"{d.name}: {a['metric']}@{a.get('version', 1)} non calcolabile al {until}")
            res = {"value": float(value), "source": source, "field": a.get("field", "value"),
                   "period_end": match[0].period_end.isoformat() if match else None}
            if match and match[0].detail:
                res["detail"] = match[0].detail
        elif d.method == "energy_target":
            tdee, weight = ref(a["tdee"]), ref(a["weight"])
            density = p("tissue_energy_density")
            deficit = a["rate_pct_per_week"] / 100 * weight["value"] * density / 7
            value = _round(tdee["value"] - deficit, a.get("round_to"))
            ree = (tdee.get("detail") or {}).get("prior_ree")
            floor = ree * p("safety_low_intake_ratio_to_ree") if ree else None
            if floor is not None and value < floor:
                raise DerivationError(f"{d.name}: {value:.0f} kcal sotto la soglia di safety ({floor:.0f} kcal)")
            res = {"value": value, "deficit_kcal": deficit, "tissue_energy_density": density,
                   "rate_pct_per_week": a["rate_pct_per_week"], "floor_kcal": floor}
        elif d.method == "energy_start":
            # start = min(formula target, habitual intake − step): the lower of two independent estimates, so that the
            # start is a deficit also when the formula overestimates maintenance
            formula, intake = ref(a["formula_target"]), ref(a["intake"])
            candidates = {"formula_target": formula["value"], "intake_minus_step": intake["value"] - a["step_kcal"]}
            bound = min(candidates, key=candidates.get)
            value = _round(candidates[bound], a.get("round_to"))
            ree = (ref(a["tdee"]).get("detail") or {}).get("prior_ree") if a.get("tdee") else None
            floor = ree * p("safety_low_intake_ratio_to_ree") if ree else None
            if floor is not None and value < floor:
                raise DerivationError(f"{d.name}: {value:.0f} kcal sotto la soglia di safety ({floor:.0f} kcal)")
            res = {"value": value, "candidates": candidates, "bound": bound, "step_kcal": a["step_kcal"],
                   "floor_kcal": floor}
        else:  # protein_target
            weight = ref(a["weight"])
            res = {"value": _round(weight["value"] * a["g_per_kg"], a.get("round_to")), "g_per_kg": a["g_per_kg"],
                   "basis_kg": weight["value"]}
        out[d.name] = res
    return {"data_until": until.isoformat(), "knowledge_cutoff": iso(cutoff or now_utc()),
            "input_fingerprint": inp.fingerprint, "values": out}


def frozen_hash(result: dict) -> str:
    return hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()


def substitute(obj, values: dict[str, dict]):
    """Replace "$derived:<name>" strings in plan contents with the computed values."""
    if isinstance(obj, str) and obj.startswith(PLACEHOLDER):
        name = obj[len(PLACEHOLDER):]
        if name not in values:
            raise DerivationError(f"valore derivato sconosciuto: {name}")
        v = values[name]["value"]
        return int(v) if float(v).is_integer() else v
    if isinstance(obj, dict):
        return {k: substitute(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, values) for v in obj]
    return obj


def placeholders(obj) -> set[str]:
    if isinstance(obj, str) and obj.startswith(PLACEHOLDER):
        return {obj[len(PLACEHOLDER):]}
    if isinstance(obj, dict):
        return set().union(*(placeholders(v) for v in obj.values())) if obj else set()
    if isinstance(obj, list):
        return set().union(*(placeholders(v) for v in obj)) if obj else set()
    return set()


def finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
