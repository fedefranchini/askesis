"""Intervention registry (docs/architecture.md §8): pre-registered, event-sourced, evaluated from metrics.

Lifecycle: proposed → approved (athlete's verbatim "approvo", a decision row) → activated (plan versions are
created; pre-registration frozen by hash) → amended* → evaluated* → concluded. Rejections are recorded too.
Conclusions use "compatible with", never "proves": an N-of-1 pre/post design cannot establish causality.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from askesis.analytics import engine
from askesis.analytics.params import p
from askesis.core.ids import new_id
from askesis.core.timeutil import iso, now_utc
from askesis.plan import store as plan_store
from askesis.safety import rules as safety


class ExpectedOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric_id: str
    subject: str = "global"
    direction: str  # decrease | increase | stable
    expected_range: tuple[float, float]  # plausible range of the metric value at evaluation
    min_effect: float | None = None  # smallest change vs baseline considered meaningful


class PlanChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    name: str
    valid_from: date
    content: dict


class Prereg(BaseModel):
    """Frozen at activation. Amendments never modify it: they are separate events."""

    model_config = ConfigDict(extra="forbid")
    domain: str
    hypothesis: str
    reason: str
    change_description: str
    plan_changes: list[PlanChange] = []
    expected_outcome: ExpectedOutcome
    success_criteria: str
    stop_criteria: list[str] = Field(min_length=1)
    min_adherence: dict[str, float] = {}  # e.g. {"nutrition_complete_days": 0.7, "weight_days": 0.7}
    start_date: date
    evaluation_date: date
    interim_checks: list[date] = []
    evidence_claims: list[str] = []
    expert_opinion: list[str] = []
    personal_preference: list[str] = []


def _event(conn, iid: str, event: str, payload: dict | None = None, at: datetime | None = None) -> None:
    conn.execute(
        "INSERT INTO intervention_event(id, intervention_id, event, at, payload) VALUES (?,?,?,?,?)",
        (
            new_id(),
            iid,
            event,
            iso(at or now_utc()),
            json.dumps(payload, default=str, ensure_ascii=False) if payload else None,
        ),
    )


def status(conn: sqlite3.Connection, iid: str) -> str | None:
    r = conn.execute("SELECT status FROM v_intervention_status WHERE id = ?", (iid,)).fetchone()
    return r["status"] if r else None


def get(conn: sqlite3.Connection, ref: str | int) -> sqlite3.Row | None:
    q = (
        "SELECT * FROM intervention WHERE number = ?"
        if str(ref).isdigit()
        else "SELECT * FROM intervention WHERE id = ?"
    )
    return conn.execute(q, (int(ref) if str(ref).isdigit() else ref,)).fetchone()


def _baseline(conn, eo: ExpectedOutcome, on: date, cutoff: datetime | None) -> dict:
    inp = engine.load_inputs(conn, cutoff)
    vals = engine.compute(inp, on - timedelta(days=28), on) if inp.dates else []
    match = [m for m in vals if m.metric_id == eo.metric_id and m.subject == eo.subject and m.period_end <= on]
    m = max(match, key=lambda x: x.period_end) if match else None
    return {
        "metric": eo.metric_id,
        "subject": eo.subject,
        "value": m.value if m else None,
        "lo": m.lo if m else None,
        "hi": m.hi if m else None,
        "period_end": m.period_end.isoformat() if m else None,
        "input_fingerprint": inp.fingerprint,
        "knowledge_cutoff": iso(cutoff or now_utc()),
    }


def propose(
    conn: sqlite3.Connection,
    title: str,
    category: str,
    prereg: dict,
    cutoff: datetime | None = None,
    at: datetime | None = None,
) -> str:
    pr = Prereg.model_validate(prereg)
    data = pr.model_dump(mode="json")
    data["baseline"] = _baseline(conn, pr.expected_outcome, pr.start_date, cutoff)
    blob = json.dumps(data, sort_keys=True, ensure_ascii=False)
    iid = new_id()
    number = (conn.execute("SELECT MAX(number) FROM intervention").fetchone()[0] or 0) + 1
    with conn:
        conn.execute(
            """INSERT INTO intervention(id, number, title, category, origin_decision_id, prereg, prereg_hash,
                            created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (iid, number, title, category, None, blob, hashlib.sha256(blob.encode()).hexdigest(), iso(at or now_utc())),
        )
        _event(conn, iid, "proposed", at=at)
    return iid


def _record_decision(conn, question, options, selected, reasoning, claims, confidence, status_, verbatim, at) -> str:
    did = new_id()
    conn.execute(
        """INSERT INTO decision(id, created_at, knowledge_cutoff, metric_run_id, question, options, selected_option,
               reasoning, evidence_claims, confidence, status, athlete_response_verbatim, responded_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            did,
            iso(at),
            iso(at),
            None,
            question,
            json.dumps(options, ensure_ascii=False),
            selected,
            reasoning,
            json.dumps(claims),
            confidence,
            status_,
            verbatim,
            iso(at),
        ),
    )
    return did


def approve(
    conn: sqlite3.Connection,
    iid: str,
    verbatim: str,
    reasoning: str,
    confidence: str,
    options: list[str] | None = None,
    override_reason: str | None = None,
    at: datetime | None = None,
) -> list[str]:
    """Record the athlete's approval, pass the safety gate, activate and create the plan versions."""
    at = at or now_utc()
    row = get(conn, iid)
    if row is None or status(conn, row["id"]) != "proposed":
        raise ValueError("intervento non in stato 'proposed'")
    if "approvo" not in verbatim.lower():
        raise ValueError("serve un 'approvo' esplicito dell'atleta")
    pr = json.loads(row["prereg"])
    safety.gate(conn, row["category"], override_reason)
    active_same_domain = conn.execute(
        """SELECT COUNT(*) FROM v_intervention_status s JOIN intervention i ON i.id = s.id
           WHERE s.status IN ('activated', 'amended', 'evaluated') AND json_extract(i.prereg, '$.domain') = ?""",
        (pr["domain"],),
    ).fetchone()[0]
    if active_same_domain >= p("intervention_max_active_per_domain"):
        raise ValueError(f"già un intervento attivo nel dominio '{pr['domain']}' (attribuzione ambigua)")
    with conn:
        did = _record_decision(
            conn,
            f"Intervento n. {row['number']}: {row['title']}",
            options or [row["title"]],
            row["title"],
            reasoning,
            pr.get("evidence_claims", []),
            confidence,
            "accepted",
            verbatim,
            at,
        )
        _event(conn, row["id"], "approved", {"decision_id": did, "override_reason": override_reason}, at)
        versions = [
            plan_store.add_version(
                conn, c["kind"], c["name"], c["content"], date.fromisoformat(c["valid_from"]), row["id"], at
            )
            for c in pr["plan_changes"]
        ]
        _event(conn, row["id"], "activated", {"plan_versions": versions, "prereg_hash": row["prereg_hash"]}, at)
    return versions


def reject(conn: sqlite3.Connection, iid: str, verbatim: str, reasoning: str, at: datetime | None = None) -> None:
    at = at or now_utc()
    row = get(conn, iid)
    with conn:
        did = _record_decision(
            conn,
            f"Intervento n. {row['number']}: {row['title']}",
            [row["title"]],
            None,
            reasoning,
            [],
            "low",
            "rejected",
            verbatim,
            at,
        )
        _event(conn, row["id"], "rejected", {"decision_id": did}, at)


def amend(conn: sqlite3.Connection, iid: str, text: str, at: datetime | None = None) -> None:
    row = get(conn, iid)
    if status(conn, row["id"]) not in ("activated", "amended", "evaluated"):
        raise ValueError("si possono emendare solo interventi attivi")
    with conn:
        _event(conn, row["id"], "amended", {"text": text}, at)


def _adherence(inp: engine.Inputs, start: date, end: date) -> dict[str, float]:
    days = max(1, (end - start).days + 1)
    weights = {d for d, *_ in inp.weighins if start <= d <= end}
    complete = {n.day for n in inp.nutrition if start <= n.day <= end and n.completeness == "complete"}
    return {"weight_days": len(weights) / days, "nutrition_complete_days": len(complete) / days}


def _confounders(conn, iid: str, start: date, end: date) -> list[str]:
    out = []
    for r in conn.execute(
        """SELECT i.number, i.title, json_extract(i.prereg,'$.start_date') s,
                  json_extract(i.prereg,'$.evaluation_date') e
               FROM intervention i JOIN v_intervention_status st ON st.id = i.id
               WHERE i.id != ? AND st.status NOT IN ('proposed', 'rejected')""",
        (iid,),
    ):
        if r["s"] <= end.isoformat() and r["e"] >= start.isoformat():
            out.append(f"intervento n. {r['number']} sovrapposto ({r['title']})")
    for r in conn.execute(
        """SELECT entity_type, local_date, payload FROM v_current
                             WHERE entity_type IN ('health_event', 'context_event') AND local_date BETWEEN ? AND ?""",
        (start.isoformat(), end.isoformat()),
    ):
        pl = json.loads(r["payload"])
        out.append(f"{r['entity_type']} {r['local_date']}: {pl.get('kind')} {pl.get('description') or ''}".strip())
    return out


def evaluate(
    conn: sqlite3.Connection, iid: str, on: date, cutoff: datetime | None = None, at: datetime | None = None
) -> dict:
    row = get(conn, iid)
    if status(conn, row["id"]) not in ("activated", "amended", "evaluated"):
        raise ValueError("intervento non attivo")
    pr = json.loads(row["prereg"])
    eo = ExpectedOutcome.model_validate(pr["expected_outcome"])
    start = date.fromisoformat(pr["start_date"])
    inp = engine.load_inputs(conn, cutoff)
    vals = engine.compute(inp, start - timedelta(days=28), on) if inp.dates else []
    match = [m for m in vals if m.metric_id == eo.metric_id and m.subject == eo.subject and m.period_end <= on]
    actual = max(match, key=lambda x: x.period_end) if match else None
    adherence = _adherence(inp, start, on)
    confounders = _confounders(conn, row["id"], start, on)
    base = pr["baseline"]["value"]
    result: dict = {
        "on": on.isoformat(),
        "metric": eo.metric_id,
        "baseline": base,
        "actual": actual.value if actual else None,
        "actual_interval": [actual.lo, actual.hi] if actual else None,
        "expected_range": list(eo.expected_range),
        "adherence": adherence,
        "confounders": confounders,
    }
    low = {k: v for k, v in adherence.items() if k in pr["min_adherence"] and v < pr["min_adherence"][k]}
    if actual is None or actual.value is None:
        conclusion = "inconclusive_insufficient_data"
    elif low:
        conclusion = "inconclusive_low_adherence"
    else:
        lo, hi = eo.expected_range
        in_range = lo <= actual.value <= hi
        direction_ok = (
            base is None
            or eo.direction == "stable"
            or (eo.direction == "decrease" and actual.value < base)
            or (eo.direction == "increase" and actual.value > base)
        )
        meaningful = base is None or eo.min_effect is None or abs(actual.value - base) >= eo.min_effect
        conclusion = "effective" if in_range and direction_ok and meaningful else "not_effective"
        if confounders and conclusion in ("effective", "not_effective"):
            conclusion = f"{conclusion}_confounded"
    result["conclusion"] = conclusion
    result["wording"] = {
        "effective": "risultato compatibile con l'esito atteso",
        "not_effective": "risultato non compatibile con l'esito atteso",
        "effective_confounded": "compatibile con l'esito atteso, ma con confondenti: attribuzione incerta",
        "not_effective_confounded": "non compatibile con l'esito atteso, con confondenti: attribuzione incerta",
        "inconclusive_low_adherence": "non valutabile: aderenza sotto la soglia pre-registrata",
        "inconclusive_insufficient_data": "non valutabile: dati insufficienti",
    }[conclusion]
    final = on >= date.fromisoformat(pr["evaluation_date"])
    with conn:
        _event(conn, row["id"], "evaluated", result, at)
        if final:
            _event(conn, row["id"], "concluded", {"conclusion": conclusion}, at)
            conn.execute(
                """INSERT INTO response_profile(id, intervention_id, domain, finding, conclusion, confidence,
                                created_at) VALUES (?,?,?,?,?,?,?)""",
                (
                    new_id(),
                    row["id"],
                    pr["domain"],
                    f"{row['title']}: {result['wording']} (atteso {eo.expected_range}, osservato {result['actual']})",
                    conclusion,
                    "low" if "confounded" in conclusion or "inconclusive" in conclusion else "moderate",
                    iso(at or now_utc()),
                ),
            )
    return result


def why(conn: sqlite3.Connection, ref: str | int) -> dict:
    """Answer: when did we change X, why, what did we know, what happened, what else was going on."""
    row = get(conn, ref)
    if row is None:
        raise ValueError("intervento non trovato")
    pr = json.loads(row["prereg"])
    events = [
        dict(e) | {"payload": json.loads(e["payload"]) if e["payload"] else None}
        for e in conn.execute(
            "SELECT event, at, payload FROM intervention_event WHERE intervention_id=? ORDER BY at, rowid", (row["id"],)
        )
    ]
    decision = None
    for e in events:
        if e["event"] == "approved":
            d = conn.execute("SELECT * FROM decision WHERE id = ?", (e["payload"]["decision_id"],)).fetchone()
            decision = dict(d) | {"evidence_claims": json.loads(d["evidence_claims"])}
    versions = [
        dict(v)
        for v in conn.execute(
            "SELECT kind, name, version_no, valid_from, id FROM plan_version WHERE intervention_id = ? ORDER BY kind",
            (row["id"],),
        )
    ]
    return {
        "number": row["number"],
        "title": row["title"],
        "category": row["category"],
        "prereg": pr,
        "decision": decision,
        "plan_versions": versions,
        "events": events,
    }


def _r(v, nd: int = 2):
    return "—" if v is None else round(v, nd)


def render_why(w: dict) -> str:
    pr, d = w["prereg"], w["decision"]
    L = [f"# Intervento n. {w['number']} — {w['title']}", ""]
    L += [
        f"**Quando:** dal {pr['start_date']} · valutazione pre-registrata il {pr['evaluation_date']}",
        f"**Cosa è cambiato:** {pr['change_description']}",
    ]
    for v in w["plan_versions"]:
        L.append(f"  - {v['kind']} «{v['name']}» v{v['version_no']} valida dal {v['valid_from']}")
    L += ["", f"**Perché (motivo):** {pr['reason']}", f"**Ipotesi:** {pr['hypothesis']}"]
    if d:
        L.append(
            f"**Decisione:** {d['reasoning']} — confidenza {d['confidence']} — risposta dell'atleta: "
            f"«{d['athlete_response_verbatim']}» ({d['responded_at']})"
        )
        if d["evidence_claims"]:
            L.append(f"**Evidenze:** {', '.join(d['evidence_claims'])}")
    for label, key in (("Opinione esperta", "expert_opinion"), ("Preferenza personale", "personal_preference")):
        if pr.get(key):
            L.append(f"**{label}:** {'; '.join(pr[key])}")
    b = pr["baseline"]
    L += [
        "",
        f"**Cosa sapevamo (baseline):** {b['metric']} = {_r(b['value'])} (fino al {b['period_end']}; "
        f"dati noti al {b['knowledge_cutoff']}, impronta {b['input_fingerprint'][:12]})",
        f"**Esito atteso:** {pr['expected_outcome']['metric_id']} {pr['expected_outcome']['direction']}, "
        f"intervallo {pr['expected_outcome']['expected_range']}",
        f"**Criteri di stop:** {'; '.join(pr['stop_criteria'])}",
        "",
        "## Cosa è successo",
    ]
    for e in w["events"]:
        pl = e["payload"] or {}
        if e["event"] == "evaluated":
            ci = pl.get("actual_interval") or [None, None]
            ci_txt = f" (IC {_r(ci[0])}–{_r(ci[1])})" if None not in ci else ""
            L.append(
                f"- valutazione al {pl['on']}: {pl['wording']} — osservato {_r(pl['actual'])}{ci_txt}, "
                f"baseline {_r(pl['baseline'])}, aderenza "
                + ", ".join(f"{k} {v:.0%}" for k, v in pl["adherence"].items())
            )
            for c in pl["confounders"]:
                L.append(f"  - confondente: {c}")
        elif e["event"] == "amended":
            L.append(f"- {e['at'][:10]} emendamento: {pl['text']}")
        else:
            L.append(f"- {e['at'][:10]} {e['event']} (registrato)")
    return "\n".join(L)
