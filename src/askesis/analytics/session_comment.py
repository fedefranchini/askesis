"""Comment after an imported strength session, written by code from stored metrics (no free numbers).

For each exercise: the session's best e1RM compared with the previous session (improved / stable / down, against the
exercise's own noise), the personal records reached, and at most one point of attention. Every number cites its
metric or record, so the text passes the validator before it is shown (reports/seduta-YYYY-MM-DD.md, Coach page).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from askesis.analytics.base import MetricValue
from askesis.analytics.params import p
from askesis.plan import rules as plan_rules
from askesis.reference import exercise_label


def _n(v: float, nd: int = 1) -> str:
    return f"{v:.{nd}f}".replace(".", ",")


def _kg(v: float) -> str:
    return f"{v:g}".replace(".", ",")


def _dm(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day}/{d.month}"


def _verdict(ch: MetricValue) -> tuple[str, str]:
    """(verdict, explanation) for a change metric."""
    md = ch.detail.get("minimal_difference_kg")
    sign = "+" if ch.value >= 0 else "−"
    delta = f"{sign}{_n(abs(ch.value))} kg"
    if ch.detail.get("lower_bound"):
        return "non confrontabile", f"{delta}, ma senza RIR la stima è solo un minimo"
    if md is None:
        return ("in aumento" if ch.value > 0 else "in calo" if ch.value < 0 else "uguale"), \
            f"{delta}; rumore non ancora stimabile (servono almeno {p('noise_min_points')} confronti)"
    if abs(ch.value) <= md:
        return "stabile", f"{delta}, dentro il rumore (±{_n(md)} kg)"
    return ("migliorato" if ch.value > 0 else "calato"), f"{delta}, oltre il rumore (±{_n(md)} kg)"


def render(conn: sqlite3.Connection, values: list[MetricValue], day: date) -> str | None:
    """Markdown comment for the strength session of `day`, or None if no working sets were recorded that day."""
    on_day = [m for m in values if m.period_start == day and m.period_end == day]
    e1 = {m.subject: m for m in on_day if m.metric_id == "e1rm_session"}
    reps = {m.subject: m for m in on_day if m.metric_id == "best_reps_session"}
    ch = {m.subject: m for m in on_day if m.metric_id == "e1rm_session_change"}
    recs = [m for m in on_day if m.metric_id == "strength_record"]
    subjects = sorted(set(e1) | set(reps), key=lambda s: _order(conn, day).get(s.split(":", 1)[1], 99))
    if not subjects:
        return None
    L = [f"# Commento seduta — {day.isoformat()}", "",
         "Per ogni esercizio: la serie migliore della seduta come e1RM stimato, confrontata con la seduta precedente "
         "e con il rumore delle tue sedute passate.", "", "## Esercizi", ""]
    downs = []
    for subj in subjects:
        name = exercise_label(subj.split(":", 1)[1])
        if subj in e1:
            m = e1[subj]
            top = f"{_kg(m.detail['load_kg'])} kg × {m.detail['reps']}"
            if subj in ch:
                c = ch[subj]
                verdict, why = _verdict(c)
                if verdict == "calato":
                    downs.append(name)
                L.append(f"- **{name}**: {verdict}. e1RM {_n(m.value)} kg (serie migliore {top}) contro "
                         f"{_n(c.detail['previous_kg'])} kg del {_dm(c.detail['previous_day'])}: {why}. "
                         "`e1rm_session@1` `e1rm_session_change@1`")
            else:
                L.append(f"- **{name}**: prima seduta registrata, fa da riferimento. e1RM {_n(m.value)} kg "
                         f"(serie migliore {top}). `e1rm_session@1`")
        else:
            L.append(f"- **{name}**: serie migliore {int(reps[subj].value)} ripetizioni (corpo libero: nessun e1RM).")
    L += ["", "## Record", ""]
    if recs:
        for r in sorted(recs, key=lambda r: r.subject):
            parts = r.subject.split(":")
            name, kind = exercise_label(parts[1]), parts[2]
            if kind == "load":
                prev = _kg(r.detail["previous"])
                L.append(f"- **{name}**: carico più alto di sempre, {_kg(r.value)} kg (prima {prev} kg). "
                         "`strength_record@1`")
            elif kind == "e1rm":
                L.append(f"- **{name}**: e1RM più alto di sempre, {_n(r.value)} kg (prima "
                         f"{_n(r.detail['previous_kg'])} kg). `strength_record@1`")
            else:
                L.append(f"- **{name}**: {int(r.value)} ripetizioni con {_kg(r.detail['load_kg'])} kg, mai così "
                         f"tante a questo carico (prima {r.detail['previous_reps']}). `strength_record@1`")
    else:
        L.append("- Nessun record in questa seduta.")
    L += ["", "## Punto di attenzione", "", f"- {_attention(conn, day, downs)}"]
    return "\n".join(L)


def _order(conn: sqlite3.Connection, day: date) -> dict[str, int]:
    """Exercise order as recorded in the session."""
    rows = conn.execute(
        "SELECT json_extract(payload, '$.exercise_id') ex, json_extract(payload, '$.exercise_raw') raw, "
        "MIN(json_extract(payload, '$.sequence')) seq FROM v_current WHERE entity_type = 'set_record' AND "
        "local_date = ? GROUP BY ex, raw", (day.isoformat(),)).fetchall()
    return {(r["ex"] or f"raw:{str(r['raw']).lower()}"): r["seq"] for r in rows}


def _attention(conn: sqlite3.Connection, day: date, downs: list[str]) -> str:
    """At most one point, by priority: pain, reps below the planned range, a drop beyond the noise."""
    thr = p("safety_pain_threshold")
    pain = conn.execute(
        "SELECT id, json_extract(payload, '$.exercise_id') ex, json_extract(payload, '$.exercise_raw') raw, "
        "json_extract(payload, '$.pain') pain FROM v_current WHERE entity_type = 'set_record' AND local_date = ? "
        "AND json_extract(payload, '$.pain') >= ? ORDER BY pain DESC LIMIT 1", (day.isoformat(), thr)).fetchone()
    if pain:
        name = exercise_label(pain["ex"]) if pain["ex"] else pain["raw"]
        return (f"dolore registrato durante {name} [record:{pain['id'][-8:]}]. Lo valutano i controlli di safety: "
                "se persiste o peggiora, sospendi il movimento e chiedi una valutazione.")
    planned = plan_rules.next_session(conn, day, record=False)
    for s in planned.get("sessions", []) if planned.get("status") == "session" else []:
        for lift in s["lifts"]:
            lo = lift["rep_range"][0]
            got = [json.loads(r["payload"])["reps"] for r in conn.execute(
                "SELECT payload FROM v_current WHERE entity_type = 'set_record' AND local_date = ? AND "
                "json_extract(payload, '$.exercise_id') = ? AND json_extract(payload, '$.set_type') != 'warmup'",
                (day.isoformat(), lift["exercise"]))]
            short = sum(1 for x in got if x < lo)
            if got and short * 2 > len(got):
                return (f"{exercise_label(lift['exercise'])}: {short} serie su {len(got)} sotto le {lo} ripetizioni "
                        "previste. Se si ripete alla prossima seduta, ne parliamo nella review.")
    if downs:
        return (f"{downs[0]}: calo oltre il rumore. Una seduta sola non basta per concludere: guarda sonno e "
                "check-in di oggi e la prossima seduta.")
    return "nessuno in questa seduta."
