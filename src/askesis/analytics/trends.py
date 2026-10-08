"""The trends page as four questions, computed here (not in the web layer), each with a verdict.

1. Sto dimagrendo al ritmo giusto?   weight trend, noise band, target rate band of the active plan
2. Sto rispettando il piano?        energy, protein and sessions vs the plan in force
3. Sto mantenendo o guadagnando forza?  e1RM of the programme's fundamental lifts only
4. Come stanno corsa e recupero?    running volume, sleep, steps

Verdicts: "ok" (in linea), "warn" (attenzione), "noise" (dentro il rumore), "neutral" (no target to judge against),
"empty" (not enough data: the message says what is missing and how much). Thresholds come from versioned rules and
parameters; nothing here invents a number. Only data up to `day` is used.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import numpy as np

from askesis.plan import calorie_rule
from askesis.plan import store as plan_store
from askesis.reference import catalog
from askesis.reference import exercise_label as exercise_name

from . import adherence, body, engine, running, training
from .params import p

PERIODS = (28, 91, 365)
VERDICT_LABELS = {"ok": "in linea", "warn": "attenzione", "noise": "dentro il rumore", "neutral": "",
                  "empty": "dati insufficienti"}


@dataclass
class Question:
    key: str
    title: str
    status: str = "empty"
    verdict: str = ""
    value: str = ""  # headline, already formatted
    unit: str = ""
    caption: str = ""
    facts: list[str] = field(default_factory=list)
    empty: str = ""  # what is missing, when status == "empty"
    spark: list[tuple[str, float]] = field(default_factory=list)  # (ISO date, value)
    spark_band: list[tuple[float, float]] = field(default_factory=list)  # optional (lo, hi) per spark point
    spark_target: float | None = None
    refs: list[str] = field(default_factory=list)

    def set(self, status: str, verdict: str | None = None) -> None:
        self.status, self.verdict = status, VERDICT_LABELS[status] if verdict is None else verdict


def _f(x: float, nd: int = 0, sign: bool = False) -> str:
    s = f"{x:+,.{nd}f}" if sign else f"{x:,.{nd}f}"
    s = s.replace(",", " ").replace(".", ",")
    return s.replace("-", "−")


# ------------------------------------------------------------------ 1. rate of loss
def target_band(conn: sqlite3.Connection, day: date) -> tuple[float, float] | None:
    """Target weekly rate (% of body weight) while a fat-loss phase is in force: the band of calorie_adjustment."""
    row = plan_store.active(conn, "phase", day)
    rule = calorie_rule.load_rule()
    if row is None or plan_store.content(row)["phase"] not in rule["applies_when"]["phase"]:
        return None
    lo, hi = rule["decision"]["target_band_pct_per_week"]
    return float(lo), float(hi)


def q_rate(conn, inp: engine.Inputs, day: date, days: int) -> Question:
    q = Question("rate", "Sto dimagrendo al ritmo giusto?", refs=["weight_ema@1", "weight_rate_pct_14d@1"])
    daily = {d: v for d, v in body.daily_weights(inp.weighins).items() if d <= day}
    ema = body.weight_ema_series(daily)
    start = day - timedelta(days=days - 1)
    if ema:
        last = max(daily)
        noise = body.weight_noise_sd(daily, last)
        half = 1.96 * noise[0] if noise else None
        q.spark = [(d.isoformat(), ema[d]) for d in sorted(ema) if d >= start]
        if half:
            q.spark_band = [(ema[d] - half, ema[d] + half) for d in sorted(ema) if d >= start]
        q.facts.append(f"peso di tendenza {_f(ema[last], 1)} kg")
    band = target_band(conn, day)
    if band:
        q.caption = f"ritmo obiettivo {_f(-band[1], 1)}–{_f(-band[0], 1)} % del peso a settimana (calorie_adjustment@2)"
    need = p("weight_rate_min_points")["14"]
    last = max(daily) if daily else day
    have = sum(1 for d in daily if last - timedelta(days=13) <= d <= last)
    rate = next((m for m in body.weight_rate(daily, last, 14) if m.metric_id == "weight_rate_pct_14d"), None) \
        if daily else None
    if daily and (day - last).days > 13:
        q.empty = (f"Ultima pesata il {last.strftime('%d/%m/%Y')}: per il ritmo servono almeno {need} pesate "
                   "negli ultimi 14 giorni.")
        q.set("empty")
        return q
    if rate is None:
        q.empty = f"Servono almeno {need} pesate in 14 giorni per stimare il ritmo: ne hai {have}."
        q.set("empty")
        return q
    q.value, q.unit = _f(rate.value, 2, sign=True), "% a settimana"
    q.facts.append(f"intervallo {_f(rate.lo, 2, True)} / {_f(rate.hi, 2, True)} %")
    if rate.lo <= 0 <= rate.hi:
        q.set("noise")
    elif band and band[0] <= rate.value <= band[1]:
        q.set("ok")
    elif band:
        q.set("warn", "più veloce del ritmo obiettivo" if rate.value < band[0] else "più lento del ritmo obiettivo")
    else:
        q.set("neutral", "in calo oltre il rumore" if rate.value < 0 else "in aumento oltre il rumore")
    return q


# ------------------------------------------------------------------ 2. adherence
def q_plan(conn, inp: engine.Inputs, day: date, days: int) -> Question:
    q = Question("plan", "Sto rispettando il piano?", refs=["nutrition_day", "sessions_vs_plan_week@1"])
    y = day - timedelta(days=1)
    target = adherence.plan_on(inp.plans, "nutrition_target", y)
    prog = adherence.plan_on(inp.plans, "programme", day)
    if target is None and prog is None and adherence.plan_on(inp.plans, "nutrition_target", day) is None:
        upcoming = conn.execute(
            "SELECT json_extract(i.prereg, '$.start_date') s FROM intervention i JOIN v_intervention_status st "
            "ON st.id = i.id WHERE st.status = 'approved' AND s > ? ORDER BY s LIMIT 1", (day.isoformat(),)).fetchone()
        when = (f" Il piano entra in vigore il {date.fromisoformat(upcoming['s']).strftime('%d/%m')}."
                if upcoming else "")
        q.empty = "Nessun piano attivo: non c'è ancora un target con cui confrontare." + when
        q.set("empty")
        return q
    statuses = []
    start = day - timedelta(days=days - 1)
    if target:
        week = [n for n in inp.nutrition if y - timedelta(days=6) <= n.day <= y and n.completeness == "complete"]
        q.spark = [(n.day.isoformat(), float(n.kcal)) for n in sorted(inp.nutrition, key=lambda n: n.day)
                   if start <= n.day <= y and n.completeness == "complete" and n.kcal is not None]
        q.spark_target = float(target["energy_kcal"])
        if len(week) >= p("adherence_min_days_of_7"):
            kcal = float(np.mean([n.kcal for n in week]))
            prot = float(np.mean([n.protein_g for n in week if n.protein_g is not None] or [0]))
            tol_e = calorie_rule.load_rule()["preconditions"]["intake_vs_target_within"]
            tol_p = p("protein_status_tolerance")
            diff = kcal - target["energy_kcal"]
            q.value, q.unit = _f(diff, 0, sign=True), "kcal/die rispetto al target"
            q.facts.append(f"energia {_f(kcal)} kcal/die, target {_f(target['energy_kcal'])}")
            q.facts.append(f"proteine {_f(prot)} g/die, target {_f(target['protein_g'])}")
            statuses.append("ok" if abs(diff) / target["energy_kcal"] <= tol_e else "warn")
            statuses.append("ok" if prot >= target["protein_g"] * (1 - tol_p) else "warn")
        else:
            q.facts.append(f"giorni di cibo completi negli ultimi 7: {len(week)} (ne servono "
                           f"{p('adherence_min_days_of_7')})")
    if prog:
        planned = sum(adherence.planned_on(inp.plans, start + timedelta(days=i)) or 0
                      for i in range((y - start).days + 1))
        done = len({s.session_id for s in inp.sets if start <= s.day <= day and s.set_type != "warmup"})
        if planned:
            q.facts.append(f"sedute {done} su {planned} previste nel periodo")
            statuses.append("ok" if done >= planned else "warn")
            if not q.value:
                q.value, q.unit = f"{done}/{planned}", "sedute"
    if not statuses:
        since = [x[0] for x in inp.plans if x[2] in ("nutrition_target", "programme") and x[0] <= day]
        first = max(since).strftime("%d/%m") if since else None
        detail = ("; ".join(q.facts) + ".") if q.facts else \
            ("il primo confronto arriva con il primo giorno completo di dati sotto il piano.")
        q.empty = (f"Piano in vigore dal {first}: " if first else "Dati ancora insufficienti: ") + detail
        q.set("empty")
        return q
    q.set("warn" if "warn" in statuses else "ok")
    return q


# ------------------------------------------------------------------ 3. strength
def fundamentals(inp: engine.Inputs, day: date) -> list[str]:
    """First N multi-joint lifts of each planned session of the programme in force (or the latest one)."""
    prog = adherence.plan_on(inp.plans, "programme", day)
    if prog is None:
        return []
    compound = {e["id"]: e.get("compound", False) for e in catalog()["exercises"]}
    n = p("strength_fundamentals_per_session")
    out: list[str] = []
    for s in prog.get("microcycle", []):
        picked = [lf["exercise"] for lf in s.get("lifts", []) if compound.get(lf["exercise"])][:n]
        out += [x for x in picked if x not in out]
    return out


def q_strength(conn, inp: engine.Inputs, day: date, days: int, values) -> Question:
    q = Question("strength", "Sto mantenendo o guadagnando forza?", refs=["e1rm_best_week@1", "e1rm_change_week@1"])
    lifts = fundamentals(inp, day)
    if not lifts:
        q.empty = "Nessun programma attivo: i fondamentali da seguire arrivano con il programma."
        q.set("empty")
        return q
    start = day - timedelta(days=days - 1)
    series = {}
    for key in lifts:
        pts = sorted((m.period_end, m.value) for m in values
                     if m.metric_id == "e1rm_best_week" and m.subject == f"exercise:{key}" and m.value is not None
                     and m.period_end <= day)
        if pts:
            series[key] = pts
    if not series:
        q.empty = (f"Nessuna serie con RIR registrata per i fondamentali ({', '.join(exercise_name(k) for k in lifts)})"
                   ": il primo valore arriva dopo la prima seduta.")
        q.set("empty")
        return q
    first = lifts[0] if lifts[0] in series else next(iter(series))
    q.spark = [(d.isoformat(), v) for d, v in series[first] if d >= start]
    q.value, q.unit = _f(series[first][-1][1], 1), f"kg e1RM · {exercise_name(first)}"
    statuses = []
    for key, pts in series.items():
        ch = [m for m in values if m.metric_id == "e1rm_change_week" and m.subject == f"exercise:{key}"
              and m.period_end <= day]
        ch = max(ch, key=lambda m: m.period_end) if ch else None
        if ch is None or ch.lo is None:
            weeks = len(pts)
            q.facts.append(f"{exercise_name(key)}: {_f(pts[-1][1], 1)} kg, rumore non ancora stimabile "
                           f"({weeks} settimane registrate, ne servono {p('noise_min_points') + 2})")
            statuses.append("neutral")
            continue
        if ch.lo > 0:
            word, st = "in aumento oltre il rumore", "ok"
        elif ch.hi < 0:
            word, st = "in calo oltre il rumore", "warn"
        else:
            word, st = "stabile, dentro il rumore", "ok"
        q.facts.append(f"{exercise_name(key)}: {_f(pts[-1][1], 1)} kg, {word}")
        statuses.append(st)
    if "warn" in statuses:
        q.set("warn")
    elif all(s == "neutral" for s in statuses):
        q.set("neutral", "rumore non ancora stimabile")
    else:
        q.set("ok")
    return q


# ------------------------------------------------------------------ 4. running and recovery
def _efficiency_fact(q: Question, inp: engine.Inputs, day: date) -> None:
    """Aerobic efficiency of the environment with most comparable easy runs up to `day` (nothing without any)."""
    vals = running.efficiency(inp.runs, inp.plans, date.min, day)
    counts: dict[str, int] = {}
    for m in vals:
        if m.metric_id == "run_efficiency":
            counts[m.subject] = counts.get(m.subject, 0) + 1
    if not counts:
        return
    env = max(sorted(counts), key=lambda e: counts[e])
    k, need = int(p("run_eff_window")), max(2 * int(p("run_eff_window")), int(p("run_eff_window")) + int(
        p("run_eff_min_noise_runs")))
    q.refs.append("run_efficiency_change@1")
    ch = [m for m in vals if m.metric_id == "run_efficiency_change" and m.subject == env]
    pace = [m for m in vals if m.metric_id == "run_pace_at_ref_hr" and m.subject == env]
    verdict = running.beyond_noise(ch[-1]) if ch else None
    if verdict is None:
        q.facts.append(f"efficienza aerobica: rumore non ancora stimabile ({counts[env]} corse facili "
                       f"confrontabili, ne servono {need})")
        return
    word = {"better": "migliorata oltre il rumore", "worse": "peggiorata oltre il rumore",
            "within": "dentro il rumore"}[verdict]
    q.facts.append(f"efficienza aerobica (corse facili confrontabili, a FC {_f(pace[-1].detail['ref_hr'])}): passo "
                   f"{running.mmss(pace[-1].value)}/km, {word} rispetto alle {k} precedenti")


def q_recovery(conn, inp: engine.Inputs, day: date, days: int) -> Question:
    q = Question("recovery", "Come stanno corsa e recupero?", refs=["sleep_mean_week@1", "run_volume_km@1",
                                                                     "steps_mean_week@1"])
    start = day - timedelta(days=days - 1)
    statuses = []
    sl = adherence.sleep_week(inp.sleep, day - timedelta(days=6), day)
    thr = p("sleep_recommended_min_h")
    q.spark = [(d.isoformat(), s / 3600) for d, s in sorted(inp.sleep) if start <= d <= day]
    q.spark_target = float(thr)
    if sl is not None and sl.value is not None:
        q.value, q.unit = _f(sl.value, 1), "ore di sonno, media 7 notti"
        statuses.append("ok" if sl.value >= thr else "warn")
        q.facts.append(f"soglia {thr} ore (raccomandazione per adulti)")
    y = day - timedelta(days=1)
    steps = [v for d, v in inp.steps.items() if y - timedelta(days=6) <= d <= y]
    target = (adherence.plan_on(inp.plans, "nutrition_target", y) or {}).get("steps_target")
    if len(steps) >= p("steps_min_days_for_mean"):
        mean = float(np.mean(steps))
        q.facts.append(f"passi {_f(mean)} al giorno" + (f", target {_f(target)}" if target else ""))
        if target:
            statuses.append("ok" if mean >= target else "warn")
    runs = training.running_week(inp.runs, start, day)
    km = next((m.value for m in runs if m.metric_id == "run_volume_km"), 0.0)
    n = next((int(m.value) for m in runs if m.metric_id == "run_count"), 0)
    q.facts.append(f"corsa {_f(km, 1)} km in {n} uscite nel periodo" if n else "nessuna corsa nel periodo")
    _efficiency_fact(q, inp, day)
    if not statuses and not n:
        q.empty = "Servono almeno qualche notte di sonno o qualche giorno di passi registrati per un quadro."
        q.set("empty")
        return q
    q.set("warn" if "warn" in statuses else "ok" if statuses else "neutral")
    return q


def questions(conn: sqlite3.Connection, day: date, days: int = 28, inp: engine.Inputs | None = None) -> list[dict]:
    if days not in PERIODS:
        days = PERIODS[0]
    inp = inp or engine.load_inputs(conn)
    first = min(inp.dates) if inp.dates else day
    values = engine.compute(inp, max(first, day - timedelta(days=days + 70)), day) if inp.dates else []
    qs = [q_rate(conn, inp, day, days), q_plan(conn, inp, day, days), q_strength(conn, inp, day, days, values),
          q_recovery(conn, inp, day, days)]
    return [asdict(q) for q in qs]
