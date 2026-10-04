"""Weekly review in Markdown. Every number carries its metric reference and epistemic label."""

from __future__ import annotations

from datetime import date, timedelta

from .base import MetricValue

LABEL = {"MEASUREMENT": "misura", "ESTIMATE": "stima", "INFERENCE": "inferenza", "FACT": "fatto"}


def _fmt(v: float | None, nd: int = 1) -> str:
    return "—" if v is None else f"{v:,.{nd}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _cite(m: MetricValue) -> str:
    return f"`{m.ref}` · {LABEL[m.epistemic]}"


def _interval(m: MetricValue, nd: int = 1) -> str:
    return "" if m.lo is None or m.hi is None else f" (IC {_fmt(m.lo, nd)}–{_fmt(m.hi, nd)})"


def render(values: list[MetricValue], week_start: date, issues: list[tuple[str, str]] | None = None) -> str:
    week_end = week_start + timedelta(days=6)
    iso_y, iso_w, _ = week_start.isocalendar()

    def get(metric: str, subject: str = "global") -> MetricValue | None:
        c = [m for m in values if m.metric_id == metric and m.subject == subject and m.period_end == week_end]
        return c[0] if c else None

    def many(metric: str) -> list[MetricValue]:
        return sorted((m for m in values if m.metric_id == metric and m.period_end == week_end),
                      key=lambda m: m.subject)

    L = [f"# Review settimanale — {iso_y}-W{iso_w:02d} ({week_start} → {week_end})", ""]
    L += ["> Generata dal codice: ogni numero rimanda a `metrica@versione`. Nessuna raccomandazione: questa",
          "> review descrive i dati; le decisioni si prendono separatamente e si registrano come interventi.", ""]

    L += ["## Qualità dei dati", "", "| Dominio | Copertura | Grado | Rif. |", "|---|---|---|---|"]
    for m in many("dq_score"):
        L.append(f"| {m.subject.split(':')[1]} | {_fmt(m.value * 100, 0)}% | {m.detail.get('grade')} | {_cite(m)} |")
    L.append("")

    L += ["## Corpo", ""]
    daily = [m for m in values if m.metric_id == "weight_daily" and week_start <= m.period_start <= week_end]
    L.append(f"- Pesate nella settimana: **{len(daily)}/7**")
    for metric, label in (("weight_ema", "Peso di tendenza (EMA) a fine settimana"),
                          ("weight_ma7", "Media mobile 7 giorni")):
        m = get(metric)
        L.append(f"- {label}: **{_fmt(m.value, 2)} kg** — {_cite(m)}" if m else f"- {label}: dati insufficienti")
    for days in (14, 28):
        kg, pct = get(f"weight_rate_{days}d"), get(f"weight_rate_pct_{days}d")
        if kg and pct:
            L.append(f"- Velocità {days} gg: **{_fmt(kg.value, 2)} kg/sett**{_interval(kg, 2)} = "
                     f"**{_fmt(pct.value, 2)} %/sett**{_interval(pct, 2)} — {_cite(kg)}, n={kg.n_obs}")
        else:
            L.append(f"- Velocità {days} gg: dati insufficienti")
    waist = [m for m in values if m.metric_id == "waist_session" and week_start <= m.period_start <= week_end]
    for m in waist:
        L.append(f"- Vita {m.period_start}: **{_fmt(m.value, 1)} cm** (letture {_fmt(m.lo, 1)}–{_fmt(m.hi, 1)}) "
                 f"— {_cite(m)}")
    L.append("")

    L += ["## Nutrizione ed energia", ""]
    m = get("intake_mean_7d")
    if m and m.value is not None:
        L.append(f"- Intake medio (giorni completi {m.detail['complete_days']}/7): **{_fmt(m.value, 0)} kcal/die**"
                 f"{_interval(m, 0)} — {_cite(m)}")
    else:
        L.append(f"- Intake medio: dati insufficienti ({m.detail.get('complete_days', 0) if m else 0}/7 giorni "
                 "completi)")
    pm = get("protein_mean_7d")
    if pm:
        L.append(f"- Proteine medie: **{_fmt(pm.value, 0)} g/die** — {_cite(pm)}")
    t = get("adaptive_tdee")
    if t:
        method = {"prior_only": "solo prior (formula), nessun dato osservato sufficiente",
                  "adaptive": "adattivo, peso dei dati osservati "
                              f"{_fmt(t.detail.get('weight_of_observed', 0) * 100, 0)}%",
                  "observed_only": "solo dati osservati"}[t.detail["method"]]
        L.append(f"- Mantenimento stimato (TDEE): **{_fmt(t.value, 0)} kcal/die**{_interval(t, 0)} — {method} — "
                 f"{_cite(t)}")
    L.append("")

    L += ["## Allenamento", ""]
    s = get("sessions_strength")
    h = get("hard_sets")
    if s:
        L.append(f"- Sessioni pesi: **{_fmt(s.value, 0)}** · serie allenanti: **{_fmt(h.value, 0)}** "
                 f"(serie senza RIR: {h.detail.get('proximity_unknown_sets', 0)}) — {_cite(h)}")
        vol = many("volume_per_muscle_week")
        if vol:
            L += ["", "| Muscolo | Serie allenanti (frazionarie) |", "|---|---|"]
            L += [f"| {v.subject.split(':')[1]} | {_fmt(v.value, 1)} |" for v in vol]
            L.append(f"\n_Conteggio frazionario: `{vol[0].ref}` · stima_")
        e1 = many("e1rm_best_week")
        if e1:
            L += ["", "| Esercizio | e1RM migliore | Nota |", "|---|---|---|"]
            L += [f"| {v.subject.split(':', 1)[1]} | {_fmt(v.value, 1)} kg | "
                  f"{'limite inferiore (RIR mancante)' if v.detail.get('lower_bound') else ''} |" for v in e1]
            L.append(f"\n_`{e1[0].ref}` · stima (formula Epley, opinione esperta)_")
    else:
        L.append("- Nessuna sessione di pesi registrata")
    km = get("run_volume_km")
    if km:
        n, tm, hr = get("run_count"), get("run_time_min"), get("run_avg_hr")
        L.append(f"- Corsa: **{_fmt(km.value, 1)} km** in **{_fmt(n.value, 0)}** uscite, **{_fmt(tm.value, 0)} min**"
                 + (f", FC media {_fmt(hr.value, 0)}" if hr else "") + f" — {_cite(km)}")
    else:
        L.append("- Nessuna corsa registrata")
    st = get("steps_mean_week")
    if st:
        L.append(f"- Passi medi: **{_fmt(st.value, 0)}/die** ({st.n_obs} giorni) — {_cite(st)}"
                 if st.value is not None else f"- Passi: solo {st.n_obs} giorni registrati, media non calcolata")
    L.append("")

    ctx = many("context_mean_week")
    if ctx:
        L += ["## Variabili di contesto", ""]
        L += [f"- {c.subject.split(':', 1)[1]}: media **{_fmt(c.value, 1)}/die** ({c.n_obs} giorni) — {_cite(c)}"
              for c in ctx]
        L.append("")

    if issues:
        L += ["## Segnalazioni di qualità aperte", ""] + [f"- [{sev}] {msg}" for sev, msg in issues] + [""]
    return "\n".join(L)
