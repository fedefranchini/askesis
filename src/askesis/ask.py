"""«Chiedi ai dati»: answers to frequent questions built by code from the athlete's own data, read only.

Order, always the same: (1) safety, deterministic — questions about symptoms, pain, illness or the relationship with
food get the fixed safety texts and nothing else; (2) intent, from fixed rules on the normalised question (a small
local model may later be added only as a fallback for questions no rule matches); (3) one tool from a closed list,
returning text plus the numbers it used; (4) the validator: every number with a unit or a decimal must come from the
tool output (`[dati:…]`) or from a stored reference; if anything fails, the answer becomes «non lo so».

Nothing here writes data or changes the plan: changes go through a proposal in Coach and the athlete's «approvo».
What a tool reads (notes, labels) is data shown to the athlete, never an instruction to this code.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from askesis.analytics import adherence, body, engine, illness, session
from askesis.plan import rules as plan_rules
from askesis.plan import store as plan_store
from askesis.reference import exercise_label, find_exercise
from askesis.safety import rules as safety
from askesis.validation import textcheck

WEEKDAYS = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]


@dataclass
class Answer:
    intent: str
    text: str  # Markdown with references (validated); shown with the references turned into source links
    data: dict[str, list[float]] = field(default_factory=dict)
    sources: list[tuple[str, str]] = field(default_factory=list)  # (label, href)
    ok: bool = True
    issues: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ wording
def norm(q: str) -> str:
    q = unicodedata.normalize("NFKD", q.lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", q.replace("’", "'")).strip()


def _f(x: float, nd: int = 1, sign: bool = False) -> str:
    s = f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"
    return s.replace("-", "−").replace(".", ",")


def _d(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d/%m')}"


def _has(q: str, *words: str) -> bool:
    return any(re.search(rf"\b{w}", q) for w in words)


# ------------------------------------------------------------------ 1. safety first (deterministic)
SAFETY = [
    ("safety_cardio", ("petto", "oppression", "svenut", "svenim", "palpitaz", "capogir", "fiato corto",
                       "tachicard", "battito irregolare", "aritmi"),
     "**Stop all'allenamento.** Dolore o oppressione al petto, svenimento o quasi, palpitazioni con capogiro, fiato "
     "corto sproporzionato o un ritmo irregolare segnalato dall'orologio vanno valutati da un medico prima di "
     "riprendere. **Se i sintomi sono in corso o forti: chiama il 112.** Qui non do interpretazioni.",
     [("Safety", "/safety")]),
    ("safety_eating", ("abbuffat", "vomit", "compensar", "senso di colpa", "non riesco a mangiare", "digiun",
                       "ossession", "ho paura di mangiare"),
     "Grazie di averlo scritto. Non è una domanda da risolvere con i numeri: i target restrittivi possono essere "
     "sospesi e il tracciamento ridotto. Parlarne con un professionista (medico, psicologo o dietista) aiuta.",
     [("Safety", "/safety")]),
    ("safety_pain", ("dolore", "fitta", "mi fa male", "infortun", "strappo", "distorsion"),
     "Un dolore va prima di tutto registrato (dove e quanto, da 0 a 10): da una soglia in su la progressione su quel "
     "movimento si ferma da sola. Se è forte, peggiora o non passa, fermati e fatti vedere. Non do diagnosi.",
     [("Safety", "/safety")]),
    ("safety_illness", ("febbre", "influenza", "sto male", "non sto bene", "raffreddor", "mal di gola", "covid",
                        "tosse"),
     "Con febbre o sintomi sotto il collo niente allenamento finché non passa; con sintomi solo sopra il collo "
     "niente allenamento intenso. Puoi segnarlo in Oggi con «Non sto bene oggi»: le sedute di quei giorni non "
     "contano come saltate.",
     [("Oggi", "/")]),
]


def _safety(q: str) -> Answer | None:
    for intent, words, text, src in SAFETY:
        if any(re.search(rf"\b{re.escape(w)}", q) for w in words):  # word starts only: «rispetto» is not «petto»
            return Answer(intent, text, sources=src)
    return None


# ------------------------------------------------------------------ 3. tools (read only)
def _inputs(conn: sqlite3.Connection) -> engine.Inputs:
    return engine.load_inputs(conn)


def weight_since_start(conn, day: date, q: str) -> Answer:
    inp = _inputs(conn)
    daily = {d: v for d, v in body.daily_weights(inp.weighins).items() if d <= day}
    ema = body.weight_ema_series(daily)
    if not ema:
        return Answer("weight_since_start", "Non ci sono ancora pesate registrate: non lo so.")
    last = max(ema)
    row = plan_store.active(conn, "phase", day)
    src = [("Andamenti", "/andamenti"), ("Piano", "/piano")]
    if row is None:
        return Answer("weight_since_start", f"Nessuna fase attiva. Peso di tendenza {_d(last)}: "
                      f"**{_f(ema[last])} kg** [dati:peso].", {"peso": [ema[last]]}, src)
    start = date.fromisoformat(row["valid_from"])
    base = [d for d in ema if d < start]
    if not base:
        return Answer("weight_since_start", f"La fase è iniziata {_d(start)} ma non c'è una pesata precedente da "
                      "usare come partenza: non lo so.", sources=src)
    b = max(base)
    change = ema[last] - ema[b]
    text = (f"Dall'inizio della fase ({_d(start)}) il **peso di tendenza** è passato da **{_f(ema[b])} kg** a "
            f"**{_f(ema[last])} kg** ({_d(last)}): **{_f(change, sign=True)} kg** [dati:peso].\n\n"
            "Il peso di tendenza è una media che filtra le oscillazioni di acqua e cibo da un giorno all'altro.")
    if (last - start).days < 14:
        text += " Nelle prime due settimane la variazione è soprattutto di liquidi e scorte: aspetta prima di trarne " \
                "conclusioni."
    return Answer("weight_since_start", text, {"peso": [ema[b], ema[last], change, abs(change)]}, src)


def nutrition_week(conn, day: date, q: str) -> Answer:
    inp = _inputs(conn)
    monday = day - timedelta(days=day.weekday())
    end = day - timedelta(days=1)  # today's food is still open
    src = [("Andamenti", "/andamenti"), ("Piano", "/piano")]
    if end < monday:
        monday, end = monday - timedelta(days=7), end
        lead = f"La settimana è appena iniziata: guardo quella scorsa ({_d(monday)}–{_d(end)})."
    else:
        lead = f"Questa settimana ({_d(monday)}–{_d(end)})."
    vals = {m.metric_id: m for m in adherence.vs_target(inp.nutrition, inp.plans, monday, end)}
    days = [n for n in inp.nutrition if monday <= n.day <= end]
    complete = [n for n in days if n.completeness == "complete" and n.kcal is not None]
    not_logged = [n for n in days if n.completeness == "not_logged"]
    if not vals:
        return Answer("nutrition_week", lead + " Non c'è un target nutrizionale attivo: non posso confrontare.",
                      sources=src)
    lines = [lead, f"Giorni di cibo completi: {len(complete)}"
             + (f" (più {len(not_logged)} non registrati)" if not_logged else "") + "."]
    data: dict[str, list[float]] = {}
    for mid, label, unit, key in (("intake_vs_target_week", "Energia", "kcal", "energia"),
                                  ("protein_vs_target_week", "Proteine", "g", "proteine")):
        m = vals.get(mid)
        if m is None or m.value is None:
            lines.append(f"- {label}: troppi pochi giorni completi per un confronto.")
            continue
        actual, target, diff = m.detail["actual_mean"], m.detail["target_mean"], m.value
        data[key] = [actual, target, diff, abs(diff)]
        verdict = ("in linea" if (m.lo is not None and m.hi is not None and m.lo <= 0 <= m.hi)
                   else ("sopra il target" if diff > 0 else "sotto il target"))
        lines.append(f"- {label}: media **{_f(actual, 0)} {unit}** al giorno contro **{_f(target, 0)} {unit}** di "
                     f"target ({_f(diff, 0, sign=True)} {unit}), {verdict} [dati:{key}].")
    return Answer("nutrition_week", "\n".join(lines), data, src)


EXERCISE_WORDS = ("panca", "squat", "stacc", "rematore", "lento", "military", "trazion", "pressa", "chest press",
                  "lat machine", "hip thrust", "affondi", "leg press")


def _exercise_in(q: str) -> dict | None:
    """The exercise named in the question, longest name first: «panca inclinata manubri» is not «panca», «stacco
    rumeno» is not «stacco»."""
    words = q.split()
    for n in range(min(5, len(words)), 0, -1):
        hits = [e for i in range(len(words) - n + 1) if (e := find_exercise(" ".join(words[i:i + n])))]
        if hits:
            return hits[0]
    return None


def lift_progress(conn, day: date, q: str) -> Answer:
    ex = _exercise_in(q)
    if ex is None:
        return Answer("lift_progress", "Non ho capito di quale esercizio parli: scrivi il nome, per esempio "
                      "«la panca sta migliorando?».")
    inp = _inputs(conn)
    name, subj = exercise_label(ex["id"]), f"exercise:{ex['id']}"
    vals = [m for m in session.session_metrics(inp.sets, day - timedelta(days=56), day) if m.subject == subj]
    best = sorted((m for m in vals if m.metric_id == "e1rm_session" and m.value is not None),
                  key=lambda m: m.period_end)
    src = [("Andamenti", "/andamenti"), ("Coach", "/coach?cat=giorno")]
    if not best:
        return Answer("lift_progress", f"Nessuna seduta di **{name}** con carico registrato nelle ultime 8 "
                      "settimane: non lo so.", sources=src)
    data = {"e1rm": [m.value for m in best]}
    lines = [f"**{name}**, e1RM stimato (il massimale teorico ricavato dalle serie) nelle ultime sedute:"]
    lines += [f"- {_d(m.period_end)}: **{_f(m.value)} kg** [dati:e1rm]" for m in best[-4:]]
    change = [m for m in vals if m.metric_id == "e1rm_session_change" and m.value is not None]
    if not change:
        lines.append("Una sola seduta finora: serve la prossima per dire se migliora.")
    else:
        c = max(change, key=lambda m: m.period_end)
        data["variazione"] = [x for x in (c.value, abs(c.value), c.lo, c.hi) if x is not None]
        if c.lo is not None and c.lo > 0:
            v = "**sì**, in aumento oltre il rumore delle tue sedute"
        elif c.hi is not None and c.hi < 0:
            v = "**no**, in calo oltre il rumore delle tue sedute"
        else:
            v = "**stabile**: la differenza rientra nel rumore normale tra una seduta e l'altra"
        lines.append(f"Rispetto alla seduta precedente: {_f(c.value, sign=True)} kg, {v} [dati:variazione].")
    return Answer("lift_progress", "\n".join(lines), data, src)


def gym_today(conn, day: date, q: str) -> Answer:
    nx = plan_rules.next_session(conn, day, record=False)
    src = [("Palestra", "/palestra"), ("Piano", "/piano")]
    ill = [e for e in _episodes(conn, day) if e.open_on(day)]
    pre = ("C'è un malessere aperto: oggi niente allenamento intenso"
           + (" (con febbre o sintomi sotto il collo: niente allenamento)" if any(e.fever or e.below_neck for e in ill)
              else "") + ".\n\n") if ill else ""
    if nx["status"] == "blocked_by_safety":
        return Answer("gym_today", "Oggi nessuna prescrizione: c'è un segnale di safety aperto. Vedi Safety.",
                      sources=[("Safety", "/safety")])
    if nx["status"] == "no_programme":
        return Answer("gym_today", "Nessun programma attivo: non c'è una seduta prevista.", sources=src)
    if nx["status"] == "rest_day":
        return Answer("gym_today", pre + f"{_d(day).capitalize()}: nessuna seduta di pesi prevista.", sources=src)
    lines, nums = [pre.strip()] if pre else [], []
    for s in nx["sessions"]:
        lines.append(f"**{s['name']}**{' (scarico)' if nx.get('deload') else ''}:")
        for lift in s["lifts"]:
            load = lift.get("load_kg")
            if load is not None:
                nums.append(load)
            reason = lift.get("reason") or ""
            nums += textcheck._numbers_in(reason)
            lines.append(f"- {exercise_label(lift['exercise'])}: {lift['sets']} × {lift['rep_target']}"
                         + (f" con **{_f(load) if load % 1 else _f(load, 0)} kg**" if load is not None else "")
                         + f", RIR {lift['target_rir']:g} — {reason} [dati:seduta]")
        if s.get("run"):
            lines.append(f"- Corsa: {s['run'].get('kind', '')}" + (f", {s['run']['duration_min']:g} min [dati:seduta]"
                         if s["run"].get("duration_min") else ""))
            if s["run"].get("duration_min"):
                nums.append(s["run"]["duration_min"])
    if any(s["lifts"] for s in nx["sessions"]):
        lines.append("Il carico è un suggerimento: scegli quello che ti lascia le ripetizioni di riserva indicate "
                     "(RIR).")
    return Answer("gym_today", "\n".join(lines), {"seduta": nums or [0.0]}, src)


def _episodes(conn, day: date) -> list:
    return safety._illness_episodes(conn, day)


def sessions_week(conn, day: date, q: str) -> Answer:
    inp = _inputs(conn)
    monday = day - timedelta(days=day.weekday())
    ill = frozenset(illness.ill_days(inp.illness, day))
    m = adherence.sessions_vs_plan(inp.sets, inp.plans, monday, monday + timedelta(days=6), ill)
    src = [("Oggi", "/"), ("Piano", "/piano")]
    if m is None:
        return Answer("sessions_week", "Nessun programma attivo: non ci sono sedute previste.", sources=src)
    done, planned = int(m.value or 0), m.detail["planned"]
    text = f"Questa settimana (da {_d(monday)}): **{done} sedute di pesi fatte** su {planned} previste."
    if m.detail.get("excused_ill"):
        text += f" Altre {m.detail['excused_ill']} cadevano in giorni di malessere: non contano come saltate."
    return Answer("sessions_week", text, sources=src)


def why_calories(conn, day: date, q: str) -> Answer:
    rows = conn.execute("""SELECT local_date, output FROM rule_execution WHERE rule_id LIKE 'calorie%'
                           ORDER BY recorded_at DESC LIMIT 20""").fetchall()
    outs = [(r["local_date"], json.loads(r["output"])) for r in rows]
    prop = next(((d, o) for d, o in outs if o.get("status") == "propose"), None)
    src = [("Coach", "/coach"), ("Piano", "/piano")]
    if prop is None:
        last = outs[0] if outs else None
        text = "Finora **nessuna proposta** di cambiare le calorie."
        if last:
            status = {"waiting": "la regola non è ancora attiva (servono i primi giorni della fase)",
                      "preconditions_not_met": "mancano le condizioni per decidere",
                      "neutral": "la velocità di calo è nella fascia prevista: nessun cambio",
                      "blocked_by_safety": "c'è un segnale di safety aperto: nessuna riduzione",
                      "too_soon": "è passato troppo poco dall'ultima proposta"}.get(last[1].get("status"), "")
            if status:
                text += f" Ultimo controllo ({date.fromisoformat(last[0]).strftime('%d/%m')}): {status}."
            reasons = last[1].get("reasons") or []
            if reasons:
                text += " Motivi: " + "; ".join(reasons) + "."
        nums = textcheck._numbers_in(text)
        if nums:
            text += " [dati:regola]"
        text += ("\n\nLa regola controlla la velocità di calo del peso di tendenza e, se è fuori dalla fascia "
                 "prevista, propone una modifica che vale solo con il tuo «approvo».")
        return Answer("why_calories", text, {"regola": nums}, src)
    d, o = prop
    text = (f"Il {date.fromisoformat(d).strftime('%d/%m')} la regola ha proposto di passare da "
            f"**{_f(o['current_kcal'], 0)} kcal** a **{_f(o['proposed_kcal'], 0)} kcal** al giorno "
            f"({_f(o['change_kcal'], 0, sign=True)} kcal) [dati:proposta].\n\nMotivo: {o.get('notify', '')} "
            "[dati:proposta]\n\nLa proposta vale solo se la approvi in Coach.")
    nums = [o["current_kcal"], o["proposed_kcal"], o["change_kcal"], abs(o["change_kcal"])]
    nums += textcheck._numbers_in(o.get("notify", ""))
    return Answer("why_calories", text, {"proposta": nums}, src)


def plan_now(conn, day: date, q: str) -> Answer:
    phase = plan_store.active(conn, "phase", day)
    target = plan_store.active(conn, "nutrition_target", day)
    src = [("Piano", "/piano")]
    if phase is None and target is None:
        return Answer("plan_now", "Nessun piano attivo oggi.", sources=src)
    lines, nums = [], []
    if phase is not None:
        c = plan_store.content(phase)
        start = date.fromisoformat(phase["valid_from"])
        week = (day - start).days // 7 + 1
        lines.append(f"Fase di {c['phase'].replace('_', ' ')}: settimana {week} di {c['max_duration_weeks']}, "
                     f"iniziata {_d(start)}.")
    if target is not None:
        t = plan_store.content(target)
        lines.append(f"Target: **{_f(t['energy_kcal'], 0)} kcal** e **{_f(t['protein_g'], 0)} g** di proteine al "
                     "giorno [dati:target].")
        nums += [t["energy_kcal"], t["protein_g"]]
    return Answer("plan_now", "\n".join(lines), {"target": nums or [0.0]}, src)


def flags_now(conn, day: date, q: str) -> Answer:
    flags = safety.open_flags(conn)
    if not flags:
        return Answer("flags_now", "Nessun segnale di safety aperto.", sources=[("Safety", "/safety")])
    lines = [f"- [{f['tier']}] {f['message']} [flag:{f['id']}]" for f in flags]
    return Answer("flags_now", "Segnali aperti:\n" + "\n".join(lines), sources=[("Safety", "/safety")])


def refuse_change(conn, day: date, q: str) -> Answer:
    return Answer("refuse_change", "Da qui non cambio il piano né i target: ogni modifica passa da una **proposta in "
                  "Coach** e vale solo con il tuo «approvo».", sources=[("Coach", "/coach")])


HELP = ["Quanto peso ho perso dall'inizio?", "Come sto andando questa settimana con calorie e proteine?",
        "La panca sta migliorando?", "Cosa devo fare oggi in palestra?", "Quante sedute ho fatto questa settimana?",
        "Perché mi avete proposto di cambiare le calorie?"]


def unknown(conn, day: date, q: str) -> Answer:
    return Answer("unknown", "**Non lo so**: per ora rispondo solo a domande su peso, calorie e proteine, esercizi, "
                  "seduta di oggi, sedute della settimana, piano, segnali di safety e proposte sulle calorie.")


# ------------------------------------------------------------------ 2. intent rules (first match wins)
Rule = tuple[str, Callable[[str], bool], Callable]
IMPERATIVE = re.compile(r"\b(cambia|modifica|imposta|aumenta|riduci|abbassa|alza|togli|metti|approvo|approva|"
                        r"accetta|conferma)\b")
RULES: list[Rule] = [
    ("why_calories", lambda q: _has(q, "perche", "motivo", "come mai") and _has(q, "calori", "kcal", "proposta",
                                                                               "propost"), why_calories),
    ("refuse_change", lambda q: bool(IMPERATIVE.search(q)) and _has(q, "calori", "kcal", "proteine", "piano",
                                                                    "target", "programma", "seduta", "carico",
                                                                    "proposta", "approvo", "squat", "panca"),
     refuse_change),
    ("weight_since_start", lambda q: _has(q, "peso", "kg", "chili", "chilo", "dimagr", "perso", "calato", "calo")
     and _has(q, "inizio", "partenza", "iniziato", "da quando", "finora", "perso", "dimagrito", "calato"),
     weight_since_start),
    ("nutrition_week", lambda q: _has(q, "calori", "kcal", "protein", "mangiat", "cibo", "dieta", "aliment"),
     nutrition_week),
    ("sessions_week", lambda q: _has(q, "quante", "quanti", "numero") and _has(q, "sedut", "allenament", "volte"),
     sessions_week),
    ("gym_today", lambda q: (_has(q, "oggi", "stasera", "stamattina", "domani") and _has(q, "palestra", "allen",
                                                                                       "sedut", "scheda", "fare"))
     or _has(q, "cosa devo fare in palestra", "che scheda"), gym_today),
    ("lift_progress", lambda q: _exercise_in(q) is not None, lift_progress),
    ("plan_now", lambda q: _has(q, "target", "fase", "piano", "obiettiv", "settimana numero"), plan_now),
    ("flags_now", lambda q: _has(q, "safety", "segnal", "allarm", "flag"), flags_now),
    ("weight_since_start", lambda q: _has(q, "peso", "pesata", "quanto peso", "dimagr", "sto calando"),
     weight_since_start),
]


def route(q: str) -> tuple[str, Callable]:
    for name, test, tool in RULES:
        if test(q):
            return name, tool
    return "unknown", unknown


def ask(conn: sqlite3.Connection, question: str, day: date) -> Answer:
    q = norm(question)[:300]
    if not q:
        return unknown(conn, day, q)
    ans = _safety(q)
    if ans is None:
        _, tool = route(q)
        ans = tool(conn, day, q)
    res = textcheck.validate(ans.text, conn, ans.data)
    if not res.ok:  # never show an unsupported number
        return Answer(ans.intent, "**Non lo so**: la risposta che avevo composto non supera il controllo dei numeri.",
                      sources=ans.sources, ok=False, issues=[i.render() for i in res.issues])
    return ans


REF = re.compile(r"\s*(?:\[(?:dati|claim|param|record|flag):[^\]]+\]|`[a-z0-9_]+@\d+`)")


def display(text: str) -> str:
    """The Markdown shown to the athlete: references stay in the stored text, sources are listed below."""
    return REF.sub("", text)
