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


NOT_ESTIMABLE = "rumore non ancora stimabile"


def _beyond_noise(m: MetricValue | None) -> str:
    """Change vs noise from the metric's interval: excludes zero → real; includes zero → within noise."""
    if m is None or m.value is None or m.lo is None or m.hi is None:
        return NOT_ESTIMABLE
    return "cambiamento reale" if m.lo > 0 or m.hi < 0 else "dentro il rumore"


def _vs_target(m: MetricValue | None) -> str:
    if m is None or m.value is None or m.lo is None or m.hi is None:
        return NOT_ESTIMABLE
    if m.lo > 0:
        return "sopra il target, oltre il rumore"
    if m.hi < 0:
        return "sotto il target, oltre il rumore"
    return "compatibile con il target (dentro il rumore)"


def _sign(v: float, nd: int = 0) -> str:
    return ("+" if v > 0 else "") + _fmt(v, nd)


def attention_points(values: list[MetricValue], week_start: date, issues: list[tuple[str, str]] | None) -> list[str]:
    """Deterministic, ranked: safety → missing data → adherence beyond noise → sessions. At most 3."""
    week_end = week_start + timedelta(days=6)

    def get(metric: str) -> MetricValue | None:
        return next((m for m in values if m.metric_id == metric and m.subject == "global"
                     and m.period_end == week_end), None)

    out = [f"Safety: {msg}" for sev, msg in (issues or []) if sev.startswith("safety")]
    body = next((m for m in values if m.metric_id == "dq_score" and m.subject == "domain:body"
                 and m.period_end == week_end), None)
    nutr = next((m for m in values if m.metric_id == "dq_score" and m.subject == "domain:nutrition"
                 and m.period_end == week_end), None)
    for m, what in ((body, "pesate"), (nutr, "giorni di cibo completi")):
        if m is not None and m.detail.get("grade") in ("C", "D"):
            out.append(f"Dati: {what} al {_fmt(m.value * 100, 0)}% della settimana (grado {m.detail['grade']}) — "
                       f"`{m.ref}`: senza dati le altre valutazioni restano incerte.")
    for metric, what, unit in (("intake_vs_target_week", "Energia", "kcal/die"),
                               ("protein_vs_target_week", "Proteine", "g/die")):
        m = get(metric)
        verdict = _vs_target(m)
        if "oltre il rumore" in verdict:
            out.append(f"{what}: media {_sign(m.value)} {unit} rispetto al target ({verdict}) — `{m.ref}`.")
    sv = get("sessions_vs_plan_week")
    if sv and sv.value < sv.detail.get("planned", 0):
        out.append(f"Sedute: {_fmt(sv.value, 0)} su {sv.detail['planned']} pianificate — `{sv.ref}`.")
    return out[:3]


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
    L += ["> Generata dal codice: ogni numero rimanda a `metrica@versione`. Ordine: fondamentali → indicatori",
          "> ritardati → punti di attenzione. Esiti: *cambiamento reale* se l'intervallo della metrica esclude lo",
          "> zero, *dentro il rumore* se lo include, *rumore non ancora stimabile* se mancano i dati per stimarlo.", ""]

    flags = [msg for sev, msg in (issues or []) if sev.startswith("safety")]
    if flags:
        L += ["## Safety", ""] + [f"- ⚠ {f}" for f in flags] + [""]

    # ---------------------------------------------------------------- 1. fundamentals
    L += ["## 1. Fondamentali", "", "| Voce | Settimana | Esito | Rif. |", "|---|---|---|---|"]
    for metric, label, unit, fallback in (("intake_vs_target_week", "Energia", "kcal/die", "intake_mean_7d"),
                                          ("protein_vs_target_week", "Proteine", "g/die", "protein_mean_7d")):
        m = get(metric)
        if m is not None and m.value is not None:
            ci = _interval(m, 0)
            L.append(f"| {label} | media {_fmt(m.detail['actual_mean'], 0)} {unit} vs target "
                     f"{_fmt(m.detail['target_mean'], 0)}: {_sign(m.value)}{ci} ({m.detail['complete_days']} giorni "
                     f"completi) | {_vs_target(m)} | {_cite(m)} |")
        elif m is not None:
            L.append(f"| {label} | {m.detail.get('complete_days', 0)} giorni completi, media non calcolata | "
                     f"{NOT_ESTIMABLE} | {_cite(m)} |")
        else:
            f = get(fallback)
            if f is not None and f.value is not None:
                L.append(f"| {label} | media {_fmt(f.value, 0)} {unit} (nessun target attivo) | — | {_cite(f)} |")
            else:
                L.append(f"| {label} | dati insufficienti | {NOT_ESTIMABLE} | — |")
    sv = get("sessions_vs_plan_week")
    if sv:
        L.append(f"| Sedute di pesi | {_fmt(sv.value, 0)} su {sv.detail['planned']} pianificate | "
                 f"{'come da piano' if sv.value >= sv.detail['planned'] else 'sotto il piano'} | {_cite(sv)} |")
    else:
        ss = get("sessions_strength")
        L.append(f"| Sedute di pesi | {_fmt(ss.value, 0) if ss else 0} (nessun programma attivo) | — | "
                 f"{_cite(ss) if ss else '—'} |")
    sl = get("sleep_mean_week")
    if sl and sl.value is not None:
        L.append(f"| Sonno | {_fmt(sl.value, 1)} h/notte{_interval(sl, 1)} ({sl.n_obs} notti) | — | {_cite(sl)} |")
    else:
        L.append(f"| Sonno | {'dati insufficienti' if sl else 'nessun dato'} | — | {_cite(sl) if sl else '—'} |")
    st = get("steps_mean_week")
    if st and st.value is not None:
        L.append(f"| Passi | {_fmt(st.value, 0)}/die ({st.n_obs} giorni) | — | {_cite(st)} |")
    else:
        L.append("| Passi | dati insufficienti | — | — |")
    L.append("")

    # ---------------------------------------------------------------- 2. lagging indicators
    L += ["## 2. Indicatori ritardati", ""]
    daily = [m for m in values if m.metric_id == "weight_daily" and week_start <= m.period_start <= week_end]
    L.append(f"- Pesate nella settimana: **{len(daily)}/7**")
    ema = get("weight_ema")
    L.append(f"- Peso di tendenza (EMA): **{_fmt(ema.value, 2)} kg** — {_cite(ema)}" if ema
             else "- Peso di tendenza: dati insufficienti")
    for days in (14, 28):
        kg, pct = get(f"weight_rate_{days}d"), get(f"weight_rate_pct_{days}d")
        if kg and pct:
            L.append(f"- Velocità {days} gg: **{_fmt(pct.value, 2)} %/sett**{_interval(pct, 2)} "
                     f"({_fmt(kg.value, 2)} kg/sett) → **{_beyond_noise(pct)}** (intervallo Theil–Sen, livello "
                     f"`rate_ci_level`) — {_cite(pct)} · {_cite(kg)}, n={pct.n_obs}")
        else:
            L.append(f"- Velocità {days} gg: dati insufficienti → **{NOT_ESTIMABLE}**")
    for m in [m for m in values if m.metric_id == "waist_session" and week_start <= m.period_start <= week_end]:
        ch = next((c for c in values if c.metric_id == "waist_change" and c.period_start == m.period_start), None)
        if ch is None:
            L.append(f"- Vita {m.period_start}: **{_fmt(m.value, 1)} cm** (prima misura) — {_cite(m)}")
            continue
        md = ch.detail.get("minimal_difference_cm")
        base = (f"differenza minima {_fmt(md, 1)} cm stimata da {ch.detail['noise_sessions']} sessioni con letture "
                "ripetute" if md is not None else "letture ripetute insufficienti")
        L.append(f"- Vita {m.period_start}: **{_fmt(m.value, 1)} cm**, {_sign(ch.value, 1)} cm dalla misura del "
                 f"{ch.detail['previous_session']} → **{_beyond_noise(ch)}** ({base}) — {_cite(ch)}")
    e1 = many("e1rm_best_week")
    if e1:
        L += ["", "| Esercizio | e1RM migliore | Variazione | Esito | Rif. |", "|---|---|---|---|---|"]
        for v in e1:
            ch = get("e1rm_change_week", v.subject)
            change = f"{_sign(ch.value, 1)} kg" if ch else "prima settimana"
            verdict = _beyond_noise(ch) if ch else NOT_ESTIMABLE
            lb = " (limite inferiore)" if v.detail.get("lower_bound") else ""
            L.append(f"| {v.subject.split(':', 1)[1]} | {_fmt(v.value, 1)} kg{lb} | {change} | {verdict} | "
                     f"{_cite(ch or v)} |")
        L.append("")
    else:
        L.append("- Forza: nessuna sessione di pesi registrata")
    km = get("run_volume_km")
    if km:
        n, tm = get("run_count"), get("run_time_min")
        L.append(f"- Corsa: **{_fmt(km.value, 1)} km** in **{_fmt(n.value, 0)}** uscite, **{_fmt(tm.value, 0)} min** "
                 f"— {_cite(km)}, `{tm.ref}`")
    else:
        L.append("- Corsa: nessuna corsa registrata")
    L.append("")

    # ---------------------------------------------------------------- 3. attention points
    points = attention_points(values, week_start, issues)
    L += ["## 3. Punti di attenzione", ""]
    L += [f"{i}. {pt}" for i, pt in enumerate(points, 1)] if points else ["Nessun punto di attenzione dai dati."]
    L.append("")

    # ---------------------------------------------------------------- data quality and details
    L += ["## Qualità dei dati", "", "| Dominio | Copertura | Grado | Rif. |", "|---|---|---|---|"]
    for m in many("dq_score"):
        L.append(f"| {m.subject.split(':')[1]} | {_fmt(m.value * 100, 0)}% | {m.detail.get('grade')} | {_cite(m)} |")
    t = get("adaptive_tdee")
    if t:
        method = {"prior_only": "solo prior (formula)", "observed_only": "solo dati osservati",
                  "adaptive": "adattivo"}[t.detail["method"]]
        L += ["", f"- Mantenimento stimato (TDEE): **{_fmt(t.value, 0)} kcal/die**{_interval(t, 0)} — {method} — "
                  f"{_cite(t)}"]
    vol = many("volume_per_muscle_week")
    if vol:
        L += ["", "| Muscolo | Serie allenanti (frazionarie) | Rif. |", "|---|---|---|"]
        L += [f"| {v.subject.split(':')[1]} | {_fmt(v.value, 1)} | {_cite(v)} |" for v in vol]
    ctx = many("context_mean_week")
    if ctx:
        L += ["", "Variabili di contesto:"] + [
            f"- {c.subject.split(':', 1)[1]}: media **{_fmt(c.value, 1)}/die** ({c.n_obs} giorni) — {_cite(c)}"
            for c in ctx]
    other = [(sev, msg) for sev, msg in (issues or []) if not sev.startswith("safety")]
    if other:
        L += ["", "Segnalazioni di qualità aperte:"] + [f"- [{sev}] {msg}" for sev, msg in other]
    L.append("")
    return "\n".join(L)


def render_month(values: list[MetricValue], first: date, last: date, interventions: list[dict],
                 profile: list[dict], flags: list[dict]) -> str:
    """Monthly retrospective: weekly series + interventions + athlete response profile (N-of-1)."""
    weeks = sorted({m.period_end for m in values if m.period_end >= first and m.period_end <= last + timedelta(days=6)
                    and m.metric_id in ("weight_ema", "dq_score")})

    def at(metric: str, end: date, subject: str = "global") -> MetricValue | None:
        return next((m for m in values if m.metric_id == metric and m.subject == subject and m.period_end == end), None)

    cols = [("weight_ema", "Peso EMA", 2), ("weight_rate_pct_14d", "Vel. 14 gg %/sett", 2),
            ("intake_mean_7d", "Intake medio", 0), ("adaptive_tdee", "TDEE", 0), ("hard_sets", "Serie allenanti", 0),
            ("run_volume_km", "Corsa km", 1)]
    present = {m.metric_id for m in values if m.value is not None}

    def head(metric: str, label: str) -> str:
        return f"{label} `{metric}@1`" if metric in present else label

    L = [f"# Retrospettiva mensile — {first:%Y-%m} ({first} → {last})", "",
         "> Serie settimanali calcolate dal codice (riferimento `metrica@versione` nell'intestazione di ogni colonna).",
         "> Le conclusioni sugli interventi sono N-of-1: compatibili/non compatibili con l'esito atteso, mai prova",
         "> causale.", "",
         "| Settimana (fine) | " + " | ".join(head(m, lab) for m, lab, _ in cols) + " | DQ `dq_score@1` |",
         "|---|" + "---|" * (len(cols) + 1)]
    for w in weeks:
        cells = [_fmt(getattr(at(m, w), "value", None), nd) for m, _, nd in cols]
        dq = at("dq_score", w, "domain:overall")
        cells.append(dq.detail.get("grade") if dq else "—")
        L.append(f"| {w} | " + " | ".join(cells) + " |")
    L += ["", "## Interventi"]
    L += [f"- n. {i['number']} — {i['title']}: {i['status']}" for i in interventions] or ["- nessuno"]
    L += ["", "## Profilo di risposta dell'atleta (N-of-1, specifico, non evidenza generale)"]
    L += [f"- {p['created_at'][:10]}: {p['finding']} — confidenza {p['confidence']}" for p in profile] or \
         ["- nessuna nuova conclusione nel mese"]
    L += ["", "## Safety"]
    L += [f"- {f['local_date']} [{f['tier']}] {f['message']}" + (f" [flag:{f['id'][-8:]}]" if f.get("id") else "")
          for f in flags] or ["- nessun flag nel mese"]
    return "\n".join(L)
