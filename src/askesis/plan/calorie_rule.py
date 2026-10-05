"""Calorie-adjustment rule engine (L2): executes knowledge/rules/calorie_adjustment.yaml.

The rule never changes the plan. When every precondition holds and the measured rate of weight change is outside
the target band, it produces a PROPOSAL (data, rule@version, reason) recorded in rule_execution; the athlete applies
it with "approvo" (`apply`), which creates a new nutrition-target version linked to the active intervention.
Every evaluation uses only data dated within the window and known at the cutoff (no look-ahead).
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta

import yaml

from askesis.analytics import body, energy, engine
from askesis.analytics.params import p
from askesis.config import ROOT
from askesis.core.ids import new_id
from askesis.core.timeutil import iso, now_utc

from . import store

RULE_FILE = ROOT / "knowledge" / "rules" / "calorie_adjustment.yaml"


def load_rule() -> dict:
    return yaml.safe_load(RULE_FILE.read_text())


def _grade(score: float) -> str:
    th = p("dq_grade_thresholds")
    return next((g for g in ("A", "B", "C") if score >= th[g]), "D")


def _previous_proposals(conn, rule_ref: str, since: date, before: date) -> list[sqlite3.Row]:
    return [r for r in conn.execute(
        "SELECT id, local_date, output FROM rule_execution WHERE rule_id = ? AND local_date >= ? AND local_date < ?"
        " ORDER BY local_date", (rule_ref, since.isoformat(), before.isoformat()))
        if json.loads(r["output"]).get("status") == "propose"]


def evaluate(conn: sqlite3.Connection, on: date, cutoff: datetime | None = None, record: bool = True,
             rule: dict | None = None) -> dict:
    rule = rule or load_rule()
    ref = f"{rule['id']}@{rule['version']}"
    out: dict = {"rule": ref, "on": on.isoformat(), "level": rule["level"], "reasons": []}

    phase_row = store.active(conn, "phase", on, as_of=cutoff)
    phase = store.content(phase_row) if phase_row else None
    if rule.get("status") != "active" or phase is None or phase["phase"] not in rule["applies_when"]["phase"]:
        return _finish(conn, out | {"status": "not_applicable"}, rule, record, on)
    start = date.fromisoformat(phase_row["valid_from"])
    first = start + timedelta(days=rule["applies_when"]["min_days_since_phase_start"] - 1)
    if on < first:
        return _finish(conn, out | {"status": "waiting", "first_evaluation": first.isoformat()}, rule, record, on)

    window = 14
    w_start = on - timedelta(days=window - 1)
    excluded_until = start + timedelta(days=rule["evaluation"]["window_days"][0] - 2)  # first week excluded
    w_start = max(w_start, excluded_until + timedelta(days=1))
    inp = engine.load_inputs(conn, cutoff)
    daily = {d: w for d, w in body.daily_weights([x for x in inp.weighins if x[0] <= on]).items()}
    in_window = {d: w for d, w in daily.items() if w_start <= d <= on}
    target_row = store.active(conn, "nutrition_target", on, as_of=cutoff)
    target = store.content(target_row) if target_row else None
    complete = [n for n in inp.nutrition if w_start <= n.day <= on and n.completeness == "complete"
                and n.kcal is not None]
    span = (on - w_start).days + 1
    pre = rule["preconditions"]
    rate = next((m for m in body.weight_rate(daily, on, window) if m.metric_id == f"weight_rate_pct_{window}d"),
                None)
    intake_mean = sum(n.kcal for n in complete) / len(complete) if complete else None
    dq = _grade(min(len(in_window) / span, len(complete) / span))
    inputs = {"window": [w_start.isoformat(), on.isoformat()], "weighins": len(in_window),
              "complete_nutrition_days": len(complete), "days": span, "intake_mean_kcal": intake_mean,
              "target_kcal": target.get("energy_kcal") if target else None, "dq_grade": dq,
              "rate_pct_per_week": rate.value if rate else None,
              "rate_ci": [rate.lo, rate.hi] if rate else None, "rate_metric": "weight_rate_pct_14d@1"}
    out["inputs"] = inputs

    reasons = []
    if target is None:
        reasons.append("nessun target nutrizionale attivo")
    if len(complete) / span < pre["nutrition_complete_days_min"]:
        reasons.append(f"giorni di nutrizione completi {len(complete)}/{span}")
    if len(in_window) < math.ceil(pre["weighins_min"] * span / window):
        reasons.append(f"pesate {len(in_window)}/{span}")
    if target and intake_mean is not None and abs(intake_mean - target["energy_kcal"]) / target["energy_kcal"] > \
            pre["intake_vs_target_within"]:
        reasons.append("intake medio lontano dal target: prima l'aderenza, poi il target")
    if "ABCD".index(dq) > "ABCD".index(pre["dq_grade_min"]):
        reasons.append(f"qualità dati {dq}")
    if rate is None or rate.value is None:
        reasons.append("velocità di calo non calcolabile")
    if reasons:
        return _finish(conn, out | {"status": "preconditions_not_met", "reasons": reasons}, rule, record, on)

    lo, hi = rule["decision"]["target_band_pct_per_week"]
    if lo <= rate.value <= hi:
        return _finish(conn, out | {"status": "neutral"}, rule, record, on)
    change = (rule["decision"]["if_slower_than_band"]["change_kcal"] if rate.value > hi
              else rule["decision"]["if_faster_than_band"]["change_kcal"])
    lim = rule["limits"]
    change = max(-lim["max_change_kcal_per_application"], min(lim["max_change_kcal_per_application"], change))
    open_flags = conn.execute("SELECT COUNT(*) FROM v_safety_open").fetchone()[0]
    if change < 0 and open_flags and pre.get("no_open_safety_flags"):
        return _finish(conn, out | {"status": "blocked_by_safety",
                                    "reasons": ["flag di safety aperti: nessuna proposta di riduzione"]}, rule, record,
                       on)
    previous = _previous_proposals(conn, ref, start, on)
    if previous and (on - date.fromisoformat(previous[-1]["local_date"])).days < lim["min_days_between_applications"]:
        return _finish(conn, out | {"status": "too_soon", "last_proposal": previous[-1]["local_date"]}, rule, record,
                       on)
    if len(previous) >= lim["max_applications_per_phase"]:
        return _finish(conn, out | {"status": "max_applications_reached"}, rule, record, on)
    current = target["energy_kcal"]
    proposed = current + change
    ree = energy.mifflin(sum(in_window.values()) / len(in_window), engine.athlete_as_of(inp, on))
    floor = ree * p("safety_low_intake_ratio_to_ree") if ree else None
    if floor is not None and proposed < floor:
        proposed = math.ceil(floor / 50) * 50
        if proposed >= current:
            return _finish(conn, out | {"status": "at_floor", "floor_kcal": floor}, rule, record, on)
    direction = "sotto" if rate.value > hi else "sopra"
    out |= {"status": "propose", "current_kcal": current, "proposed_kcal": proposed, "change_kcal": proposed - current,
            "floor_kcal": floor,
            "notify": f"Proposta: target {'+' if proposed > current else '−'}{abs(proposed - current):g} kcal "
                      f"(velocità {rate.value:+.2f} %/sett, {direction} la fascia) — {ref}"}
    return _finish(conn, out, rule, record, on)


def _finish(conn, out: dict, rule: dict, record: bool, on: date) -> dict:
    if record:
        out["execution_id"] = new_id()
        with conn:
            conn.execute(
                "INSERT INTO rule_execution(id, rule_id, local_date, programme_version_id, inputs, output, recorded_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (out["execution_id"], out["rule"], on.isoformat(), None, json.dumps(out.get("inputs", {})),
                 json.dumps(out, default=str), iso(now_utc())))
    return out


def apply(conn: sqlite3.Connection, execution: str, verbatim: str, valid_from: date,
          at: datetime | None = None, undo: bool = False) -> str:
    """Apply an approved proposal: new nutrition-target version linked to the active intervention + amendment.
    With undo=True, restore the target the proposal replaced (also an L2 change: it needs "approvo")."""
    from askesis.interventions import registry as reg

    if "approvo" not in verbatim.lower():
        raise ValueError("serve un 'approvo' esplicito dell'atleta")
    row = conn.execute("SELECT * FROM rule_execution WHERE id = ? OR id LIKE ?",
                       (execution, f"%{execution}")).fetchone()
    if row is None:
        raise ValueError("esecuzione della regola non trovata")
    out = json.loads(row["output"])
    if out.get("status") != "propose":
        raise ValueError("l'esecuzione non contiene una proposta")
    applied = conn.execute("SELECT json_extract(payload, '$.undo') u FROM intervention_event WHERE event = 'amended' "
                           "AND json_extract(payload, '$.rule_execution') = ?", (row["id"],)).fetchall()
    if not undo and applied:
        raise ValueError("proposta già applicata")
    if undo and (not applied or any(r["u"] for r in applied)):
        raise ValueError("nulla da annullare per questa proposta")
    target_row = store.active(conn, "nutrition_target", valid_from)
    if target_row is None or target_row["intervention_id"] is None:
        raise ValueError("nessun target attivo collegato a un intervento")
    iid = target_row["intervention_id"]
    if reg.status(conn, iid) not in ("activated", "amended", "evaluated"):
        raise ValueError("intervento non attivo")
    kcal = out["current_kcal"] if undo else out["proposed_kcal"]
    content = store.content(target_row) | {"energy_kcal": kcal, "method": f"rule:{out['rule']}" + (":undo" if undo
                                                                                                  else "")}
    at = at or now_utc()
    with conn:
        vid = store.add_version(conn, "nutrition_target", target_row["name"], content, valid_from, iid, at)
        conn.execute(
            "INSERT INTO intervention_event(id, intervention_id, event, at, payload) VALUES (?,?,?,?,?)",
            (new_id(), iid, "amended", iso(at), json.dumps({
                "text": (f"{out['rule']}: annullata, target riportato a {kcal:g} kcal" if undo else
                         f"{out['rule']}: target {out['current_kcal']:g} → {out['proposed_kcal']:g} kcal"),
                "rule_execution": row["id"], "plan_version": vid, "undo": undo,
                "athlete_response_verbatim": verbatim},
                ensure_ascii=False)))
    return vid
