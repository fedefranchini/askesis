"""CLI for plans, interventions, safety and monthly retrospective (phase F3)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

import typer
import yaml

from askesis import config as config_mod
from askesis.interventions import registry as reg
from askesis.plan import rules as plan_rules
from askesis.plan import store as plan_store
from askesis.safety import rules as safety
from askesis.store.db import connect

plan_app = typer.Typer(no_args_is_help=True, help="Piano: fase, programma, target, periodi programmati.")
iv_app = typer.Typer(no_args_is_help=True, help="Registro degli interventi.")
safety_app = typer.Typer(no_args_is_help=True, help="Safety layer.")

DateOpt = Annotated[str | None, typer.Option("--date", "-d", help="Data YYYY-MM-DD (default: oggi)")]


def _ctx():
    cfg = config_mod.load()
    return cfg, connect(cfg.db_path)


def _today(cfg) -> date:
    from zoneinfo import ZoneInfo

    from askesis.core.timeutil import now_utc

    return now_utc().astimezone(ZoneInfo(cfg.timezone)).date()


def _d(value: str | None, cfg) -> date:
    return date.fromisoformat(value) if value else _today(cfg)


# ------------------------------------------------------------------ safety
TIER_LABEL = {"T0": "info", "T1": "attenzione", "T2": "pausa + valutazione professionale", "T3": "URGENTE"}


def print_flags(conn, on: date) -> None:
    safety.evaluate(conn, on)
    for f in safety.open_flags(conn):
        color = "red" if f["tier"] in ("T2", "T3") else "yellow"
        typer.secho(f"⚠ [{f['tier']} · {TIER_LABEL[f['tier']]}] {f['message']}", fg=color)
        for a in json.loads(f["actions"]):
            typer.secho(f"   → {a}", fg=color)


@safety_app.command("check")
def safety_check(date_: DateOpt = None) -> None:
    """Valuta le regole di safety e mostra i flag aperti."""
    cfg, conn = _ctx()
    print_flags(conn, _d(date_, cfg))
    if not safety.open_flags(conn):
        typer.echo("✓ nessun flag di safety aperto")


@safety_app.command("resolve")
def safety_resolve(flag_id: str, resolution: Annotated[str, typer.Option("--resolution", "-r")]) -> None:
    """Chiude un flag con la sua risoluzione (resta nello storico)."""
    cfg, conn = _ctx()
    rows = [f for f in safety.open_flags(conn) if f["id"].endswith(flag_id) or f["id"] == flag_id]
    if len(rows) != 1:
        raise typer.BadParameter("flag non trovato o ambiguo")
    safety.resolve(conn, rows[0]["id"], resolution)
    typer.echo("✓ flag risolto")


def _status_message(out: dict) -> str:
    if out["status"] == "blocked_by_safety":
        return (
            "✗ nessuna prescrizione: flag di safety aperti ("
            + "; ".join(f"{f['tier']}: {f['message']}" for f in out["flags"])
            + ")"
        )
    return {"no_programme": "nessun programma attivo", "rest_day": "giorno senza sessione"}[out["status"]]


# ------------------------------------------------------------------ plan
@plan_app.command("show")
def plan_show(date_: DateOpt = None) -> None:
    """Piano in vigore in una data."""
    cfg, conn = _ctx()
    d = _d(date_, cfg)
    for kind in ("phase", "programme", "nutrition_target"):
        row = plan_store.active(conn, kind, d)
        if row is None:
            typer.echo(f"{kind}: —")
            continue
        typer.echo(
            f"{kind}: «{row['name']}» v{row['version_no']} dal {row['valid_from']} "
            f"(intervento {row['intervention_id'][-8:] if row['intervention_id'] else '—'})"
        )
        typer.echo("  " + json.dumps(plan_store.content(row), ensure_ascii=False)[:400])
    for p in plan_store.periods_on(conn, d):
        typer.echo(f"periodo programmato: {p['label']} {p['start']}→{p['end']} {p['modifiers']}")


@plan_app.command("history")
def plan_history(kind: Annotated[str | None, typer.Argument()] = None) -> None:
    """Tutte le versioni del piano (immutabili)."""
    cfg, conn = _ctx()
    for r in plan_store.history(conn, kind):
        typer.echo(
            f"{r['kind']:<17} «{r['name']}» v{r['version_no']} dal {r['valid_from']} · registrata "
            f"{r['recorded_at'][:10]}"
        )


@plan_app.command("next")
def plan_next(date_: DateOpt = None, no_record: Annotated[bool, typer.Option("--no-record")] = False) -> None:
    """Prescrizione della sessione del giorno (regole L1: doppia progressione, deload pianificati)."""
    cfg, conn = _ctx()
    out = plan_rules.next_session(conn, _d(date_, cfg), record=not no_record)
    if out["status"] != "session":
        typer.echo(_status_message(out))
        return
    flags = [k for k in ("deload", "minimal_week") if out[k]] + out["periods"]
    typer.echo(f"{out['date']}" + (f" · {', '.join(flags)}" if flags else ""))
    for s in out["sessions"]:
        typer.echo(f"## {s['name']}")
        for lift in s["lifts"]:
            load = f"{lift['load_kg']:g} kg" if lift["load_kg"] is not None else "carico da calibrare"
            typer.echo(
                f"- {lift['exercise']}: {lift['sets']}×{lift['rep_target']} "
                f"(range {lift['rep_range'][0]}–{lift['rep_range'][1]}) @ RIR {lift['target_rir']:g} · {load}"
                f" — {lift['reason']}"
            )
        if s["run"]:
            r = s["run"]
            typer.echo(
                f"- corsa {r['kind']}: "
                + (f"{r['duration_min']:g} min " if r.get("duration_min") else "")
                + (f"{r['distance_km']:g} km " if r.get("distance_km") else "")
                + f"· intensità {r['intensity']}"
            )


# ------------------------------------------------------------------ interventions
def _print_derived(conn, data: dict, today: date) -> None:
    """Provisional preview of the values that will be computed and frozen at activation."""
    from askesis.interventions import derive

    if not data.get("derived"):
        return
    spec = derive.DerivedSpec.model_validate(data["derived"])
    until = min(today, spec.data_until)
    typer.echo(f"\nValori derivati — PROVVISORI (dati fino al {until}); all'attivazione verranno ricalcolati con i "
               f"dati fino al {spec.data_until} e congelati:")
    try:
        res = derive.compute(conn, spec, data_until=until, preview=True)
    except derive.DerivationError as exc:
        typer.echo(f"  non calcolabili ora: {exc}")
        return
    for d in spec.values:
        v = res["values"][d.name]
        when = f", {v['period_end']}" if v.get("period_end") else ""
        src = f" [{v['source']}{when}]" if v.get("source") else ""
        typer.echo(f"  - {d.name} = {v['value']:.1f} {d.unit}{src} — {d.description}")


@iv_app.command("propose")
def iv_propose(
    file: Path,
    title: Annotated[str, typer.Option("--title")],
    category: Annotated[str, typer.Option("--category")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Controlla e mostra senza registrare")] = False,
) -> None:
    """Registra una proposta (pre-registrazione da file YAML), dopo il controllo di numeri e fonti."""
    from askesis.validation import textcheck

    cfg, conn = _ctx()
    data = yaml.safe_load(file.read_text())
    check = textcheck.check_prereg({**data, "title": title}, conn)
    if not check.ok:  # an unsupported proposal is never registered
        typer.echo(f"✗ proposta DA VERIFICARE — {len(check.issues)} punti non supportati, non registrata:")
        for i in check.issues:
            typer.echo(i.render())
        raise typer.Exit(1)
    if dry_run:
        reg.Prereg.model_validate(data)
        typer.echo("✓ controllo superato (nessuna registrazione: --dry-run)")
        _print_derived(conn, data, _today(cfg))
        return
    iid = reg.propose(conn, title, category, data)
    typer.echo(f"✓ proposto intervento n. {reg.get(conn, iid)['number']} ({iid[-8:]})")
    _print_derived(conn, data, _today(cfg))


@iv_app.command("render")
def iv_render(
    file: Path,
    title: Annotated[str, typer.Option("--title")],
    out: Annotated[Path | None, typer.Option("--out", help="Salva la proposta in Markdown")] = None,
    note: Annotated[bool, typer.Option("--note", help="Crea/aggiorna la nota del piano in Apple Notes")] = False,
) -> None:
    """Versione leggibile di una pre-registrazione (valori derivati provvisori) e nota del piano per il telefono."""
    from askesis import notes_bridge
    from askesis.ingestion.gymnote import to_html
    from askesis.interventions import derive, present

    cfg, conn = _ctx()
    data = yaml.safe_load(file.read_text())
    preview, error = None, None
    if data.get("derived"):
        spec = derive.DerivedSpec.model_validate(data["derived"])
        try:
            preview = derive.compute(conn, spec, data_until=min(_today(cfg), spec.data_until), preview=True)
        except derive.DerivationError as exc:
            error = str(exc)
    md = present.render_proposal(title, data, preview, error)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md)
        typer.echo(f"✓ proposta salvata in {out}")
    else:
        typer.echo(md)
    if note:
        from askesis.validation import textcheck

        check = textcheck.check_prereg({**data, "title": title}, conn)
        if not check.ok:  # the phone plan only shows validated proposals
            typer.echo(f"✗ nota non aggiornata: proposta DA VERIFICARE ({len(check.issues)} punti non supportati)")
            raise typer.Exit(1)
        lines = present.plan_note_lines(title, data, preview, provisional=bool(data.get("derived")))
        notes_bridge.upsert(lines[0], to_html(lines))
        typer.echo(f"✓ nota «{lines[0]}» aggiornata in Apple Notes (cartella {notes_bridge.FOLDER})")


@iv_app.command("approve")
def iv_approve(
    number: int,
    verbatim: Annotated[str, typer.Option("--verbatim")],
    reasoning: Annotated[str, typer.Option("--reasoning")],
    confidence: Annotated[str, typer.Option("--confidence")] = "moderate",
    override: Annotated[str | None, typer.Option("--override-safety")] = None,
) -> None:
    """Registra l'approvazione testuale dell'atleta e attiva l'intervento."""
    cfg, conn = _ctx()
    row = reg.get(conn, number)
    try:
        versions = reg.approve(conn, row["id"], verbatim, reasoning, confidence, override_reason=override)
    except safety.SafetyBlock as exc:
        typer.secho(f"✗ bloccato dalla safety: {exc}", fg="red")
        raise typer.Exit(1) from exc
    if reg.status(conn, row["id"]) == "approved":
        start = json.loads(row["prereg"])["start_date"]
        typer.echo(f"✓ intervento n. {number} approvato · attivazione dal {start} con `bin/ak intervention activate "
                   f"{number}` (valori derivati calcolati e congelati in quel momento)")
        return
    typer.echo(f"✓ intervento n. {number} attivato · {len(versions)} versioni di piano create")


@iv_app.command("activate")
def iv_activate(
    number: int,
    date_: DateOpt = None,
    override: Annotated[str | None, typer.Option("--override-safety")] = None,
    note: Annotated[bool, typer.Option("--note", help="Aggiorna la nota del piano in Apple Notes")] = False,
) -> None:
    """Attiva un intervento approvato: calcola e congela i valori derivati, crea le versioni di piano."""
    from askesis.interventions import derive

    cfg, conn = _ctx()
    row = reg.get(conn, number)
    try:
        res = reg.activate(conn, row["id"], _d(date_, cfg), override_reason=override)
    except safety.SafetyBlock as exc:
        typer.secho(f"✗ bloccato dalla safety: {exc}", fg="red")
        raise typer.Exit(1) from exc
    except (ValueError, derive.DerivationError) as exc:
        typer.secho(f"✗ non attivato: {exc}", fg="red")
        raise typer.Exit(1) from exc
    typer.echo(f"✓ intervento n. {number} attivato · valori congelati (dati fino al {res['data_until']}):")
    for name, v in res["values"].items():
        typer.echo(f"  - {name} = {v['value']:.1f}")
    if note:  # the phone plan now shows the frozen values
        from askesis import notes_bridge
        from askesis.ingestion.gymnote import to_html
        from askesis.interventions import present

        lines = present.plan_note_lines(row["title"], json.loads(row["prereg"]), res | {"frozen": True},
                                        provisional=False)
        try:
            notes_bridge.upsert(lines[0], to_html(lines))
            typer.echo(f"✓ nota «{lines[0]}» aggiornata con i valori congelati")
        except notes_bridge.NotesError as exc:
            typer.echo(f"⚠ nota non aggiornata: {exc}")


@iv_app.command("reject")
def iv_reject(
    number: int,
    verbatim: Annotated[str, typer.Option("--verbatim")],
    reasoning: Annotated[str, typer.Option("--reasoning")],
) -> None:
    cfg, conn = _ctx()
    reg.reject(conn, reg.get(conn, number)["id"], verbatim, reasoning)
    typer.echo(f"✓ intervento n. {number} rifiutato (registrato)")


@iv_app.command("amend")
def iv_amend(number: int, text: str) -> None:
    """Emendamento datato (la pre-registrazione non cambia)."""
    cfg, conn = _ctx()
    reg.amend(conn, reg.get(conn, number)["id"], text)
    typer.echo("✓ emendamento registrato")


@iv_app.command("evaluate")
def iv_evaluate(number: int, date_: DateOpt = None) -> None:
    """Valutazione calcolata (intermedia o finale)."""
    cfg, conn = _ctx()
    res = reg.evaluate(conn, reg.get(conn, number)["id"], _d(date_, cfg))
    typer.echo(
        f"{res['wording']} · osservato {reg._r(res['actual'])} · atteso {res['expected_range']} · aderenza "
        + ", ".join(f"{k} {v:.0%}" for k, v in res["adherence"].items())
    )
    for c in res["confounders"]:
        typer.echo(f"  confondente: {c}")


@iv_app.command("list")
def iv_list() -> None:
    cfg, conn = _ctx()
    for r in conn.execute("SELECT number, title, category, status FROM v_intervention_status ORDER BY number"):
        typer.echo(f"n. {r['number']:<3} {r['status']:<10} {r['category']:<28} {r['title']}")


def why_cmd(number: int) -> None:
    """Perché abbiamo cambiato X, cosa sapevamo, cosa è successo."""
    cfg, conn = _ctx()
    typer.echo(reg.render_why(reg.why(conn, number)))


# ------------------------------------------------------------------ monthly retrospective
def intervention_status_as_of(conn, on: date) -> list[dict]:
    """Status of each intervention as it was at the end of `on` (events recorded later are ignored)."""
    limit = (on + timedelta(days=1)).isoformat()
    out = []
    for r in conn.execute("SELECT id, number, title FROM intervention WHERE created_at < ? ORDER BY number", (limit,)):
        ev = conn.execute(
            "SELECT event FROM intervention_event WHERE intervention_id = ? AND at < ? "
            "ORDER BY at DESC, rowid DESC LIMIT 1",
            (r["id"], limit),
        ).fetchone()
        out.append({"number": r["number"], "title": r["title"], "status": ev["event"] if ev else "proposed"})
    return out


def month_cmd(date_: str | None) -> str:
    from askesis.analytics import engine, review

    cfg, conn = _ctx()
    d = _d(date_, cfg)
    first = (d.replace(day=1) - timedelta(days=1)).replace(day=1) if not date_ else d.replace(day=1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    _, values, _ = engine.run(conn, first - timedelta(days=28), last)
    ivs = intervention_status_as_of(conn, last)
    prof = [
        dict(r)
        for r in conn.execute(
            "SELECT finding, conclusion, confidence, created_at FROM response_profile WHERE created_at BETWEEN ? AND ?",
            (first.isoformat(), (last + timedelta(days=1)).isoformat()),
        )
    ]
    flags = [
        dict(r)
        for r in conn.execute(
            "SELECT id, tier, local_date, message FROM safety_flag WHERE local_date BETWEEN ? AND ?",
            (first.isoformat(), last.isoformat()),
        )
    ]
    return review.render_month(values, first, last, ivs, prof, flags)


# ------------------------------------------------------------------ gym day sheet (Apple Notes)
gym_app = typer.Typer(no_args_is_help=True, help="Scheda del giorno per la palestra (Apple Note).")


def _settings() -> dict[str, str]:
    p = config_mod.ROOT / "private" / "machine_settings.yaml"
    return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}


def _last_performance(conn, exercise: str, before: date) -> str | None:
    from askesis.ingestion.readback import _set

    hist = plan_rules.exposures(conn, exercise, before, limit=1)
    if not hist:
        return None
    h = hist[-1]
    sets = [
        _set({"load_kg": ld, "reps": r, **({"rir": q} if q is not None else {})})
        for ld, r, q in zip(h.loads, h.reps, h.rirs, strict=True)
    ]
    return f"({h.day.day}/{h.day.month}): " + " · ".join(sets)


@gym_app.command("create")
def gym_create(
    date_: Annotated[str, typer.Option("--for", help="Data della sessione YYYY-MM-DD")],
    print_only: Annotated[bool, typer.Option("--print", help="Mostra senza creare la nota")] = False,
) -> None:
    """Crea/aggiorna la nota con la sessione pianificata (solo esercizi e carichi)."""
    from askesis import notes_bridge
    from askesis.ingestion import gymnote
    from askesis.reference import find_exercise

    cfg, conn = _ctx()
    d = date.fromisoformat(date_)
    out = plan_rules.next_session(conn, d, record=not print_only)
    if out["status"] != "session":
        typer.echo(_status_message(out))
        raise typer.Exit(1)
    settings = _settings()
    for s in out["sessions"]:
        last = {}
        for lift in s["lifts"]:
            ref = find_exercise(lift["exercise"])
            key = ref["id"] if ref else lift["exercise"]
            perf = _last_performance(conn, lift["exercise"], d)
            if perf:
                last[key] = perf
        lines = gymnote.render(d, s, last, settings)
        typer.echo("\n".join(lines))
        if not print_only:
            notes_bridge.upsert(lines[0], gymnote.to_html(lines))
            typer.echo(f"\n✓ nota «{lines[0]}» creata/aggiornata in Note → cartella {notes_bridge.FOLDER}")


@gym_app.command("import")
def gym_import(
    date_: Annotated[str, typer.Option("--for", help="Data della sessione YYYY-MM-DD")],
    session: Annotated[str | None, typer.Option("--session", help="Nome sessione (default: dal piano)")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Conferma dopo l'anteprima")] = False,
) -> None:
    """Legge i risultati dalla nota, mostra l'anteprima e (con --yes) li importa."""
    from askesis import notes_bridge
    from askesis.cli.main import _print_receipt
    from askesis.core.timeutil import at_local, now_utc
    from askesis.ingestion import gymnote, manual, readback
    from askesis.ingestion.pipeline import ingest
    from askesis.parsers.text import Intent, ParseError, parse_run

    cfg, conn = _ctx()
    d = date.fromisoformat(date_)
    if session is None:
        out = plan_rules.next_session(conn, d, record=False)
        names = [s["name"] for s in out.get("sessions", [])]
        session = names[0] if names else "sessione"
    body = notes_bridge.read(gymnote.title_for(d, session))
    if body is None:
        typer.echo(f"nota «{gymnote.title_for(d, session)}» non trovata")
        raise typer.Exit(1)
    res = gymnote.parse_note(gymnote.html_to_lines(body))
    intents = [Intent("gym", {"exercises": res.exercises})] if res.exercises else []
    if res.run_text:
        try:
            intents.append(Intent("run", parse_run(res.run_text)))
        except ParseError as exc:
            res.errors.append(("corsa", f"{res.run_text!r}: {exc}"))
    now = now_utc()
    start = at_local(d, manual.NOON, cfg.timezone)
    records = [r for it in intents for r in manual.build(it, cfg, d, now)]
    for r in records:  # deterministic time: the exact session time is not in the note
        if "start_at" in r:
            dur = (r["end_at"] - r["start_at"]) if r.get("end_at") else None
            r["start_at"] = start
            if dur is not None:
                r["end_at"] = start + dur
    records = gymnote.keyed_records(records, d)
    to_ingest, unchanged, missing = gymnote.reconcile(conn, records)
    for ex, problem in res.errors:
        typer.secho(f"✗ non letto — {ex}: {problem} (correggi la nota o dettamelo)", fg="red")
    for ex in res.skipped:
        typer.echo(f"– non svolto: {ex}")
    if res.notes:
        typer.echo(f"Note: {res.notes}")
    if unchanged:
        typer.echo(f"= già importati e invariati: {len(unchanged)}")
    if missing:
        typer.secho(
            f"! righe importate in precedenza ma ora assenti dalla nota: {', '.join(missing)} "
            "(non ritirate automaticamente: chiedere all'atleta)",
            fg="yellow",
        )
    if not to_ingest:
        typer.echo("nulla di nuovo da importare")
        return
    corrections = [r for r in to_ingest if r.get("supersedes_id")]
    if not yes:
        typer.echo("ANTEPRIMA — nulla salvato:")
        for line in readback.lines(to_ingest):
            typer.echo(f"· {line}" if not line.startswith("  ") else f"  {line.strip()}")
        if corrections:
            typer.echo(f"({len(corrections)} correzioni di righe già importate)")
        typer.echo("Se è corretto, ripetere con --yes per salvare.")
        return
    r = ingest(conn, to_ingest, "gym_note")
    _print_receipt(r)
    print_flags(conn, d)
