"""Weekly review with a traffic light ("semaforo"), used only for weeks inside a phase (see `context`).

Eight areas in a fixed priority order (safety, data, energy, protein, sessions, weight rate, sleep, load). Each area
declares its criterion, then shows data → trend → interpretation → action. Everything is deterministic and generated
by code: every number carries its `metric@version`, colours are never given alone (the status word is always printed)
and actions never change the plan (a change can only be a proposal to approve with «approvo»).
Weeks before the phase keep the previous format (`review.render`, untouched).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta

from askesis.interventions import registry
from askesis.plan import store

from . import review, running
from .base import MetricValue
from .params import p
from .review import NOT_ESTIMABLE, _cite, _fmt, _interval, _sign

VERDE, GIALLO, ROSSO, NV, INFO = "verde", "giallo", "rosso", "non valutabile", "informativo"
EMOJI = {VERDE: "🟢", GIALLO: "🟡", ROSSO: "🔴", NV: "⚪", INFO: "ℹ️"}
GRADE_STATUS = {"A": VERDE, "B": VERDE, "C": GIALLO, "D": ROSSO}
RANK = {ROSSO: 0, GIALLO: 1}
PLAN_NOTE = "solo come proposta da approvare con «approvo»"
LOAD_REASON = {"week_in_progress": "settimana ancora in corso", "missing_feedback": "manca il feedback di una seduta",
               "sd_zero": "carico identico tutti i giorni (deviazione zero)"}


def context(conn, week_start: date) -> dict | None:
    """Phase data when a phase with a linked intervention is in force on `week_start`, otherwise None."""
    row = store.active(conn, "phase", week_start)
    if row is None or not row["intervention_id"]:
        return None
    iv = registry.get(conn, row["intervention_id"])
    if iv is None:
        return None
    expected = (json.loads(iv["prereg"]) or {}).get("expected_outcome")
    return {"phase_start": date.fromisoformat(row["valid_from"]), "phase_name": row["name"],
            "intervention_number": iv["number"], "expected": expected}


# ---------------------------------------------------------------- classification (pure)
def classify_safety(issues: list[tuple[str, str]] | None) -> tuple[str, str]:
    n = sum(1 for sev, _ in (issues or []) if sev.startswith("safety"))
    return (ROSSO, "flags") if n else (VERDE, "none")


def classify_data(grades: list[str]) -> tuple[str, str]:
    if not grades:
        return NV, "missing"
    worst = max(grades)  # "A" < "B" < "C" < "D"
    return GRADE_STATUS[worst], worst


def classify_energy(m: MetricValue | None) -> tuple[str, str]:
    if m is None or m.value is None or m.lo is None or m.hi is None:
        return NV, "missing"
    if m.lo <= 0 <= m.hi:
        return VERDE, "within"
    return GIALLO, "above" if m.lo > 0 else "below"


def classify_protein(m: MetricValue | None) -> tuple[str, str]:
    if m is None or m.value is None or m.hi is None:
        return NV, "missing"
    return (GIALLO, "below") if m.hi < 0 else (VERDE, "ok")


def classify_sessions(m: MetricValue | None) -> tuple[str, str]:
    if m is None or m.value is None:
        return NV, "missing"
    missed = m.detail.get("planned", 0) - m.value
    if missed <= 0:
        return VERDE, "all"
    return (GIALLO, "few") if missed <= p("review_sessions_missed_yellow") else (ROSSO, "many")


def classify_velocity(m: MetricValue | None, expected: dict | None, window_start: date,
                      phase_start: date) -> tuple[str, str]:
    """Key: no_range | pre_phase | missing | inside | overlap_<above|below> | outside_<above|below>."""
    if not expected or expected.get("metric_id") != "weight_rate_pct_14d" or not expected.get("expected_range"):
        return NV, "no_range"
    if window_start < phase_start:
        return NV, "pre_phase"
    if m is None or m.value is None or m.lo is None or m.hi is None:
        return NV, "missing"
    lo, hi = sorted(expected["expected_range"])
    if lo <= m.value <= hi:
        return VERDE, "inside"
    side = "above" if m.value > hi else "below"
    return (GIALLO, f"overlap_{side}") if m.hi >= lo and m.lo <= hi else (ROSSO, f"outside_{side}")


def classify_strength(changes: list[list[MetricValue | None]]) -> tuple[str, str]:
    """`changes`: per exercise, e1rm_change_week for this week and the previous ones (newest first, None if absent).
    Rosso when an exercise is down beyond noise for `review_strength_red_weeks` consecutive weeks (the phase's
    exit criterion), giallo when any is down beyond noise this week, verde when at least one is estimable."""
    def down(m: MetricValue | None) -> bool:
        return m is not None and m.hi is not None and m.hi < 0

    now = [c[0] for c in changes if c and c[0] is not None and c[0].lo is not None]
    if not now:
        return NV, "missing"
    k = p("review_strength_red_weeks")
    if any(len(c) >= k and all(down(m) for m in c[:k]) for c in changes):
        return ROSSO, "down_long"
    return (GIALLO, "down") if any(down(m) for m in now) else (VERDE, "ok")


def classify_sleep(m: MetricValue | None) -> tuple[str, str]:
    if m is None or m.value is None:
        return NV, "missing"
    return (VERDE, "ok") if m.value >= p("sleep_recommended_min_h") else (GIALLO, "short")


# ---------------------------------------------------------------- area blocks
@dataclass
class Area:
    name: str
    status: str
    reason: str  # short, number-free (table)
    criterion: str
    data: str
    trend: str
    interp: str
    action: str
    extra: list[str] | None = None  # extra list items right after the heading (e.g. safety flags)


def _word(cur: float, prev: float, nd: int) -> str:
    c, q = round(cur, nd), round(prev, nd)
    return "invariato" if c == q else "più alto" if c > q else "più basso"


class _Series:
    """Weekly metric lookups: this week and the previous one (no look-ahead: only periods ending ≤ week end)."""

    def __init__(self, values: list[MetricValue], week_end: date):
        self.values, self.end = values, week_end

    def get(self, metric: str, subject: str = "global", back: int = 0) -> MetricValue | None:
        end = self.end - timedelta(days=7 * back)
        return next((m for m in self.values if m.metric_id == metric and m.subject == subject
                     and m.period_end == end), None)

    def earlier(self, metric: str, subject: str = "global") -> bool:
        return any(m.metric_id == metric and m.subject == subject and m.period_end < self.end for m in self.values)

    def trend(self, metric: str, nd: int, unit: str = "", subject: str = "global") -> str:
        """Comparison with the previous week, wording only (a difference of two values is not a stored number)."""
        cur, prev = self.get(metric, subject), self.get(metric, subject, 1)
        if cur is None and (prev is None or prev.value is None):
            return "nessun confronto (nessun dato)"
        if prev is None or prev.value is None:
            if prev is None and not self.earlier(metric, subject):
                return "prima settimana con questo dato"
            return "nessun confronto (dato della settimana precedente non disponibile)"
        u = f" {unit}" if unit else ""
        if cur is None or cur.value is None:
            return f"nessun confronto; settimana precedente: {_fmt(prev.value, nd)}{u} — {_cite(prev)}"
        return (f"{_word(cur.value, prev.value, nd)} rispetto alla settimana precedente "
                f"(prima: {_fmt(prev.value, nd)}{u}) — {_cite(prev)}")


def _safety(issues, s: _Series) -> Area:
    flags = [msg for sev, msg in issues or [] if sev.startswith("safety")]
    status, _ = classify_safety(issues)
    if flags:
        data, interp = f"{len(flags)} flag di safety registrati nella settimana (elenco sotto).", \
            "i flag sono segnali da valutare, non una diagnosi"
        action = ("segui le azioni del flag (vedi docs/agent/safety.md); nessun allenamento intenso fino a "
                  "chiarimento; per sintomi cardiovascolari acuti 112, altrimenti medico prima di riprendere")
    else:
        data, interp = "nessun flag di safety nella settimana.", "nessun segnale di safety registrato"
        action = "continua a registrare dolore e sintomi appena compaiono"
    return Area("Safety", status, "flag presenti" if flags else "nessun flag",
                "rosso se c'è almeno un flag di safety nella settimana, altrimenti verde", data,
                "nessun confronto: i flag non sono una metrica", interp, action,
                extra=[f"- ⚠ {f}" for f in flags])


def _data_quality(s: _Series) -> Area:
    parts, grades, trends = [], [], []
    for subject, what in (("domain:body", "pesate"), ("domain:nutrition", "giorni di cibo completi")):
        m, prev = s.get("dq_score", subject), s.get("dq_score", subject, 1)
        if m is not None:
            grades.append(m.detail.get("grade", "D"))
            parts.append(f"{what} al {_fmt(m.value * 100, 0)}% della settimana (grado {m.detail.get('grade')})")
        if m is not None and prev is not None:
            trends.append(f"{what}: {_word(m.value, prev.value, 2)} (prima: {_fmt(prev.value * 100, 0)}%)")
    status, key = classify_data(grades)
    any_m = s.get("dq_score", "domain:body") or s.get("dq_score", "domain:nutrition")
    cite = f" — {_cite(any_m)}" if any_m else ""
    data = ("; ".join(parts) + cite) if parts else "nessun dato di qualità per la settimana"
    if trends:
        trend = "; ".join(trends) + f" rispetto alla settimana precedente — {_cite(any_m)}"
    else:
        trend = "nessun confronto con la settimana precedente"
    interp = {"missing": "senza registrazioni le altre valutazioni non sono possibili",
              "A": "copertura ampia: le altre valutazioni poggiano su dati solidi",
              "B": "copertura ampia: le altre valutazioni poggiano su dati solidi",
              "C": "copertura parziale: le altre valutazioni sono meno affidabili",
              "D": "copertura scarsa: le altre valutazioni restano incerte"}[key]
    action = ("registra il peso ogni mattina e il cibo completo del giorno prima"
              if status in (GIALLO, ROSSO, NV) else "continua a registrare peso e cibo come ora")
    reason = {"missing": "nessun dato", "A": "copertura ampia", "B": "copertura ampia", "C": "copertura parziale",
              "D": "copertura scarsa"}[key]
    return Area("Dati", status, reason, "peggior grado tra pesate e cibo: A o B verde, C giallo, D rosso "
                "[param:dq_grade_thresholds]", data, trend, interp, action)


def _vs_target(s: _Series, metric: str, name: str, what: str, unit: str) -> Area:
    m = s.get(metric)
    status, key = (classify_energy if metric == "intake_vs_target_week" else classify_protein)(m)
    if m is None:
        data = "nessun target nutrizionale attivo o nessun dato"
    elif m.value is None:
        data = f"{m.detail.get('complete_days', 0)} giorni completi, media non calcolata — {_cite(m)}"
    else:
        data = (f"media {_fmt(m.detail['actual_mean'], 0)} {unit} vs target {_fmt(m.detail['target_mean'], 0)} "
                f"{unit}: {_sign(m.value)} {unit}{_interval(m, 0)} ({m.detail['complete_days']} giorni completi) "
                f"— {_cite(m)}")
    trend = s.trend(metric, 0, unit)
    if metric == "intake_vs_target_week":
        criterion = ("verde se l'intervallo di confidenza della differenza media dal target include lo zero, "
                     "giallo se lo esclude (sopra o sotto il target)")
        interp = {"missing": "giorni completi insufficienti per stimare la differenza dal target",
                  "within": "compatibile con il target: la differenza resta dentro il rumore del giorno per giorno",
                  "above": "sopra il target oltre il rumore del giorno per giorno",
                  "below": "sotto il target oltre il rumore del giorno per giorno"}[key]
        action = {"missing": "registra il cibo completo di ogni giorno: servono più giorni completi",
                  "within": "mantieni l'apporto attuale rispetto al target",
                  "above": "avvicinati al target giornaliero a partire dal prossimo pasto; nessun cambio al piano",
                  "below": (f"avvicinati al target giornaliero; se lo scarto è voluto, parlane: un cambio di "
                            f"target sarebbe {PLAN_NOTE}")}[key]
        reason = {"missing": "giorni completi insufficienti", "within": "in linea con il target",
                  "above": "sopra il target oltre il rumore", "below": "sotto il target oltre il rumore"}[key]
    else:
        criterion = ("giallo se l'intero intervallo di confidenza è sotto il target (sotto oltre il rumore), "
                     "altrimenti verde; sopra il target non è segnalato")
        interp = {"missing": "giorni completi insufficienti per stimare la differenza dal target",
                  "ok": "compatibile con il target, oppure sopra",
                  "below": "sotto il target oltre il rumore del giorno per giorno"}[key]
        action = {"missing": "registra il cibo completo di ogni giorno: servono più giorni completi",
                  "ok": "mantieni le fonti proteiche attuali",
                  "below": "aggiungi una fonte proteica ai pasti principali; nessun cambio al piano"}[key]
        reason = {"missing": "giorni completi insufficienti", "ok": "in linea con il target",
                  "below": "sotto il target oltre il rumore"}[key]
    return Area(name, status, reason, criterion, data, trend, interp, action)


def _sessions(s: _Series) -> Area:
    m = s.get("sessions_vs_plan_week")
    status, key = classify_sessions(m)
    data = (f"{_fmt(m.value, 0)} su {m.detail.get('planned', 0)} pianificate — {_cite(m)}" if m is not None
            else "nessun programma attivo")
    prev = s.get("sessions_vs_plan_week", back=1)
    if m is None:
        trend = "nessun confronto"
    elif prev is None:
        trend = "prima settimana con questo dato" if not s.earlier("sessions_vs_plan_week") \
            else "nessun confronto (dato della settimana precedente non disponibile)"
    else:
        more = {"più alto": "più sedute", "più basso": "meno sedute", "invariato": "stesse sedute"}
        trend = (f"{more[_word(m.value, prev.value, 0)]} rispetto alla settimana precedente (prima: "
                 f"{_fmt(prev.value, 0)} su {prev.detail.get('planned', 0)} pianificate) — {_cite(prev)}")
    interp = {"missing": "nessun programma attivo con cui confrontare le sedute",
              "all": "sedute svolte come da piano",
              "few": "una seduta in meno del piano, compatibile con un imprevisto",
              "many": "più sedute del piano mancate"}[key]
    action = {"missing": "registra le sedute svolte per poterle confrontare con il piano",
              "all": "mantieni la frequenza attuale",
              "few": "recupera la seduta saltata solo se il calendario lo permette, senza accumulare carico",
              "many": (f"recupera la regolarità con la settimana minima; se il calendario è cambiato, la "
                       f"modifica del programma sarebbe {PLAN_NOTE}")}[key]
    reason = {"missing": "nessun programma", "all": "sedute come da piano", "few": "una seduta mancata",
              "many": "più sedute mancate"}[key]
    return Area("Sedute", status, reason,
                "verde se le sedute svolte raggiungono quelle pianificate; giallo se ne manca al massimo una "
                "[param:review_sessions_missed_yellow]; rosso se ne mancano di più", data, trend, interp, action)


def _velocity(s: _Series, ctx: dict) -> Area:
    m = s.get("weight_rate_pct_14d")
    window_start = s.end - timedelta(days=13)
    status, key = classify_velocity(m, ctx.get("expected"), window_start, ctx["phase_start"])
    crit = ("verde se la velocità di variazione del peso a 14 giorni è dentro l'intervallo atteso dalla "
            f"preregistrazione dell'intervento n. {ctx['intervention_number']}; giallo se è fuori ma l'intervallo "
            "di confidenza si sovrappone all'atteso; rosso se è interamente fuori. Non valutabile se la finestra "
            "di 14 giorni include giorni prima della fase")
    direction = (ctx.get("expected") or {}).get("direction")
    side = key.split("_")[-1]
    faster = (direction == "decrease" and side == "below") or (direction == "increase" and side == "above")
    if key in ("no_range", "pre_phase", "missing"):
        data = {"no_range": "la preregistrazione non fissa un intervallo atteso per questa velocità",
                "pre_phase": "la finestra di 14 giorni include giorni prima della fase: velocità non confrontabile",
                "missing": "dati insufficienti per stimare la velocità a 14 giorni"}[key]
        trend = "nessun confronto"
        interp = {"no_range": "senza intervallo atteso non c'è un criterio con cui valutare la velocità",
                  "pre_phase": "i primi giorni della fase non sono ancora separabili da quelli precedenti",
                  "missing": "rumore non ancora stimabile"}[key]
        action = {"no_range": "nessuna azione sulla velocità finché non c'è un intervallo atteso",
                  "pre_phase": "continua a registrare il peso ogni mattina; la velocità sarà valutabile tra poco",
                  "missing": "registra il peso ogni mattina per stimare la velocità"}[key]
        reason = {"no_range": "nessun intervallo atteso", "pre_phase": "finestra con giorni prima della fase",
                  "missing": "dati insufficienti"}[key]
    else:
        where = {"inside": "dentro l'intervallo atteso", "above": "sopra l'intervallo atteso",
                 "below": "sotto l'intervallo atteso"}["inside" if key == "inside" else side]
        data = f"**{_fmt(m.value, 2)} %/sett**{_interval(m, 2)}, {where} (n={m.n_obs}) — {_cite(m)}"
        trend = s.trend("weight_rate_pct_14d", 2, "%/sett")
        if key == "inside":
            interp, action, reason = ("compatibile con l'esito atteso dall'intervento",
                                      "mantieni il piano com'è e continua a registrare", "dentro l'atteso")
        else:
            pace = "più rapido" if faster else "più lento"
            conf = "l'intervallo di confidenza si sovrappone all'atteso: compatibile con l'esito atteso entro il " \
                   "rumore" if key.startswith("overlap") else "anche l'intervallo di confidenza è fuori dall'atteso"
            interp = f"ritmo {pace} dell'atteso; {conf}"
            if key.startswith("overlap"):
                action = "nessuna modifica: ricontrolla la prossima settimana con altri giorni di dati"
                reason = "fuori dall'atteso, dentro il rumore"
            elif faster:
                action = ("nessun aumento del deficit; valutare una correzione verso l'alto "
                          f"({PLAN_NOTE})")
                reason = "oltre l'atteso, più rapido"
            else:
                action = (f"verifica prima l'aderenza (pesate e cibo); un eventuale aggiustamento sarebbe "
                          f"{PLAN_NOTE}")
                reason = "oltre l'atteso, più lento"
    return Area("Velocità del peso", status, reason, crit, data, trend, interp, action)


def _strength(s: _Series) -> Area:
    subjects = sorted({m.subject for m in s.values if m.metric_id == "e1rm_change_week" and m.period_end == s.end})
    k = p("review_strength_red_weeks")
    changes = [[s.get("e1rm_change_week", sub, b) for b in range(k)] for sub in subjects]
    status, key = classify_strength(changes)
    parts = []
    for sub, c in zip(subjects, changes, strict=True):
        m = c[0]
        verdict = review._beyond_noise(m)
        parts.append(f"{sub.split(':', 1)[1]} {_sign(m.value, 1)} kg ({verdict})")
    data = ("variazione dell'e1RM migliore rispetto alla settimana precedente: " + "; ".join(parts) +
            " — `e1rm_change_week@1` · stima") if parts else "nessun confronto di forza disponibile nella settimana"
    down = [sub.split(":", 1)[1] for sub, c in zip(subjects, changes, strict=True)
            if c[0] is not None and c[0].hi is not None and c[0].hi < 0]
    trend = (f"in calo oltre il rumore: {', '.join(down)}" if down else
             "nessun esercizio in calo oltre il rumore") if parts else "nessun confronto"
    interp = {"missing": "rumore non ancora stimabile: servono più settimane sugli stessi esercizi",
              "ok": "forza mantenuta o in aumento, entro il rumore delle settimane precedenti",
              "down": "calo oltre il rumore questa settimana: da rileggere con sonno, carico ed energia",
              "down_long": "calo oltre il rumore per più settimane consecutive: criterio di uscita della fase"}[key]
    action = {"missing": "registra carichi, ripetizioni e RIR di ogni serie di lavoro",
              "ok": "segui la doppia progressione del programma",
              "down": "nessun cambio: controlla recupero e aderenza e ricontrolla la prossima settimana",
              "down_long": f"rivalutare il deficit con l'aderenza della fase; un cambio sarebbe {PLAN_NOTE}"}[key]
    reason = {"missing": "rumore non stimabile", "ok": "nessun calo oltre il rumore", "down": "calo oltre il rumore",
              "down_long": "calo oltre il rumore per più settimane"}[key]
    return Area("Forza", status, reason,
                "verde se nessun esercizio cala oltre il rumore; giallo se almeno uno cala oltre il rumore questa "
                "settimana; rosso se lo stesso esercizio cala oltre il rumore per più settimane consecutive "
                "[param:review_strength_red_weeks]", data, trend, interp, action)


def _sleep(s: _Series) -> Area:
    m = s.get("sleep_mean_week")
    status, key = classify_sleep(m)
    if m is None:
        data = "nessun dato di sonno nella settimana"
    elif m.value is None:
        data = f"{m.n_obs} notti registrate, meno del minimo per la media — {_cite(m)}"
    else:
        data = f"{_fmt(m.value, 1)} h/notte{_interval(m, 1)} ({m.n_obs} notti) — {_cite(m)}"
    interp = {"missing": "dati di sonno insufficienti per una media",
              "ok": "in linea con l'indicazione per gli adulti", "short": "sotto l'indicazione per gli adulti"}[key]
    action = {"missing": "indossa l'orologio di notte o registra il sonno per ottenere la media",
              "ok": "mantieni gli orari di sonno attuali",
              "short": "anticipa l'orario di andare a letto di poco; nessun cambio al piano"}[key]
    reason = {"missing": "dati insufficienti", "ok": "in linea con l'indicazione", "short": "sotto l'indicazione"}[key]
    return Area("Sonno", status, reason, "verde se la media è almeno 7 h/notte [param:sleep_recommended_min_h], "
                "altrimenti giallo", data, s.trend("sleep_mean_week", 1, "h/notte"), interp, action)


def _load(s: _Series) -> Area:
    load, mono, strain = (s.get(k) for k in ("training_load_week", "training_monotony_week", "training_strain_week"))
    if load is None:
        data = "nessuna seduta registrata nella settimana"
        reason = "nessuna seduta"
    else:
        data = f"carico {_fmt(load.value, 0)} AU"
        if mono is not None and mono.value is not None and strain is not None and strain.value is not None:
            data += f", monotonia {_fmt(mono.value, 2)}, strain {_fmt(strain.value, 0)} AU"
        else:
            why = LOAD_REASON.get((mono.detail if mono else {}).get("reason"), "dati insufficienti")
            data += f"; monotonia e strain non definiti ({why})"
        data += f" — {_cite(load)}" + "".join(f", `{x.ref}`" for x in (mono, strain) if x is not None)
        reason = "solo informativo"
    return Area("Carico (Foster)", INFO, reason,
                "nessun colore: le soglie di monotonia e strain sono individuali e non generalizzabili "
                "[claim:load.monotony_strain]", data, s.trend("training_load_week", 0, "AU"),
                "il carico descrive quanto e come ti sei allenato; da solo non indica se è troppo o troppo poco",
                "nessuna azione dal solo carico; leggilo insieme a sonno e sedute")


def _efficiency_line(values: list[MetricValue], week_end: date) -> str:
    """Latest aerobic-efficiency reading up to the week end (cites the pace and the change metrics)."""
    pace = [m for m in values if m.metric_id == "run_pace_at_ref_hr" and m.period_end <= week_end]
    if not pace:
        return "- Efficienza aerobica: nessuna corsa facile confrontabile"
    last = max(pace, key=lambda m: (m.period_end, m.subject))
    ch = next((m for m in values if m.metric_id == "run_efficiency_change" and m.subject == last.subject
               and m.period_end == last.period_end), None)
    word = {"better": "migliorata oltre il rumore", "worse": "peggiorata oltre il rumore",
            "within": "dentro il rumore", None: NOT_ESTIMABLE}[running.beyond_noise(ch)]
    vs = "" if ch is None or ch.lo is None else f" rispetto alle {last.detail['window']} precedenti"
    return (f"- Efficienza aerobica (corse facili confrontabili, a FC {_fmt(last.detail['ref_hr'], 0)} bpm): passo "
            f"{running.mmss(last.value)}/km, {word}{vs} — "
            f"{_cite(last)}, {_cite(ch) if ch else ''}".rstrip(", "))


# ---------------------------------------------------------------- render
def _e1rm_table(values: list[MetricValue], week_end: date) -> list[str]:
    def get(metric: str, subject: str) -> MetricValue | None:
        return next((m for m in values if m.metric_id == metric and m.subject == subject
                     and m.period_end == week_end), None)

    rows = ["| Esercizio | e1RM migliore | Variazione | Esito | Rif. |", "|---|---|---|---|---|"]
    for v in sorted((m for m in values if m.metric_id == "e1rm_best_week" and m.period_end == week_end),
                    key=lambda m: m.subject):
        ch = get("e1rm_change_week", v.subject)
        change = f"{_sign(ch.value, 1)} kg" if ch else "prima settimana"
        lb = " (limite inferiore)" if v.detail.get("lower_bound") else ""
        refs = _cite(v) + (f" · {_cite(ch)}" if ch else "")
        rows.append(f"| {v.subject.split(':', 1)[1]} | {_fmt(v.value, 1)} kg{lb} | {change} | "
                    f"{review._beyond_noise(ch) if ch else review.NOT_ESTIMABLE} | {refs} |")
    return rows


def areas(values: list[MetricValue], week_start: date, issues, ctx: dict) -> list[Area]:
    s = _Series(values, week_start + timedelta(days=6))
    return [_safety(issues, s), _data_quality(s),
            _vs_target(s, "intake_vs_target_week", "Energia", "Energia", "kcal/die"),
            _vs_target(s, "protein_vs_target_week", "Proteine", "Proteine", "g/die"),
            _sessions(s), _velocity(s, ctx), _strength(s), _sleep(s), _load(s)]


def top_actions(ar: list[Area], n: int = 3) -> list[str]:
    """Rosso before giallo, ties by area order; filled with 'Mantieni' from green areas (safety excluded)."""
    alert = sorted((a for a in ar if a.status in RANK), key=lambda a: RANK[a.status])  # stable: area order
    out = [f"**{a.name}** ({a.status}): {a.action}" for a in alert]
    out += [f"Mantieni: {a.action}" for a in ar if a.status == VERDE and a.name != "Safety"]
    return out[:n]


def render(values: list[MetricValue], week_start: date, issues: list[tuple[str, str]] | None, ctx: dict) -> str:
    week_end = week_start + timedelta(days=6)
    iso_y, iso_w, _ = week_start.isocalendar()
    ar = areas(values, week_start, issues, ctx)
    wk = (week_start - ctx["phase_start"]).days // 7 + 1
    L = [f"# Review settimanale — {iso_y}-W{iso_w:02d} ({week_start} → {week_end})", ""]
    L += ["> Generata dal codice: ogni numero rimanda a `metrica@versione`. Il criterio di ogni area è dichiarato",
          "> qui sotto; lo stato è sempre scritto a parole oltre al colore. Le azioni non cambiano mai il piano:",
          "> un cambio è solo una proposta da approvare con «approvo».", "",
          f"Fase: {ctx['phase_name']}, settimana {wk} (intervento n. {ctx['intervention_number']}).", ""]
    L += ["## Semaforo", "", "| Area | Stato | Motivo in breve |", "|---|---|---|"]
    L += [f"| {a.name} | {EMOJI[a.status]} {a.status} | {a.reason} |" for a in ar]
    L += ["", "## Tre azioni prioritarie", ""]
    acts = top_actions(ar)
    L += [f"{i}. {t}" for i, t in enumerate(acts, 1)] if acts else ["Nessuna azione: nessuna area valutabile."]
    L += ["", "## Aree", ""]
    for a in ar:
        L += [f"### {EMOJI[a.status]} {a.name} — {a.status}", "", f"- **Criterio:** {a.criterion}",
              f"- **Dati:** {a.data}", f"- **Tendenza:** {a.trend}", f"- **Interpretazione:** {a.interp}",
              f"- **Azione:** {a.action}"]
        L += (a.extra or []) + [""]

    # details: the lagging indicators and the data-quality section of the previous format, verbatim
    old = review.render(values, week_start, issues)
    lag = old[old.index("## 2. Indicatori ritardati"):old.index("## 3. Punti di attenzione")]
    lag = lag.replace("## 2. Indicatori ritardati", "## Dettagli — indicatori ritardati", 1).rstrip().splitlines()
    table = [i for i, line in enumerate(lag) if line.startswith("|")]
    if table:  # the e1RM table, rebuilt so that each row cites both the best e1RM and its change
        lag[table[0]:table[-1] + 1] = _e1rm_table(values, week_end)
    L += [*lag, _efficiency_line(values, week_end), ""]
    L += [old[old.index("## Qualità dei dati"):].rstrip()]
    L.append("")
    return "\n".join(L)

