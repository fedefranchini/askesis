"""Askesis command-line interface (`askesis`, short alias `ak`)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer

from askesis import config as config_mod
from askesis.core.ids import new_id
from askesis.core.timeutil import at_local, now_utc
from askesis.core.units import format_duration
from askesis.ingestion import manual, readback, staging
from askesis.ingestion.pipeline import Receipt, ingest
from askesis.model.entities import Envelope
from askesis.parsers.text import Intent, ParseError, parse_day, parse_gym, parse_run
from askesis.store import backup as backup_mod
from askesis.store import repository as repo
from askesis.store.db import connect

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Askesis — data core e registrazione rapida.")
log_app = typer.Typer(no_args_is_help=True, help="Registra un singolo dato.")
show_app = typer.Typer(no_args_is_help=True, help="Mostra i dati registrati.")
app.add_typer(log_app, name="log")
metrics_app = typer.Typer(no_args_is_help=True, help="Metriche derivate (cache ricalcolabile).")
from askesis.cli import f3  # noqa: E402

app.add_typer(f3.plan_app, name="plan")
app.add_typer(f3.iv_app, name="intervention")
app.add_typer(f3.safety_app, name="safety")
app.add_typer(f3.gym_app, name="gym-note")
app.add_typer(show_app, name="show")
app.add_typer(metrics_app, name="metrics")

DateOpt = Annotated[str | None, typer.Option("--date", "-d", help="Data YYYY-MM-DD (default: oggi)")]


def _ctx():
    cfg = config_mod.load()
    return cfg, connect(cfg.db_path)


def _day(value: str | None, cfg) -> date:
    if value:
        return date.fromisoformat(value)
    return now_utc().astimezone(ZoneInfo(cfg.timezone)).date()


def _describe(env: Envelope) -> str:
    p = env.payload
    e = env.entity_type
    if e == "body_weight":
        return f"peso {p['value_kg']} kg ({env.local_date})"
    if e == "nutrition_day":
        prot = f" · {p['protein_g']:g} g proteine" if "protein_g" in p else ""
        return f"cibo {env.local_date}: {p.get('energy_kcal', 0):g} kcal{prot} [{p['completeness']}]"
    if e == "body_measurement":
        return f"{p['site']} {', '.join(f'{r:g}' for r in p['readings_cm'])} cm ({env.local_date})"
    if e == "training_session":
        return f"sessione pesi {env.local_date}"
    if e == "set_record":
        tag = f" r{p['rir']:g}" if "rir" in p else (f" @{p['rpe']:g}" if "rpe" in p else "")
        kind = "" if p["set_type"] == "working" else f" [{p['set_type']}]"
        return f"  {p['exercise_raw']} {p['load_kg']:g}x{p['reps']}{tag}{kind}"
    if e == "running_session":
        return (f"corsa {p['distance_m'] / 1000:.2f} km in {format_duration(p['elapsed_s'])}"
                + (f" · FC {p['avg_hr']}" if "avg_hr" in p else "") + f" ({env.local_date})")
    if e == "daily_activity":
        return f"passi {p['steps']} ({env.local_date})"
    if e == "daily_context":
        return f"{p['key']} = {p['value']} ({env.local_date})"
    if e == "sleep_session":
        return f"sonno {format_duration(p['asleep_s'])} (risveglio {env.local_date})"
    return f"{e} ({env.local_date})"


def _print_receipt(r: Receipt) -> None:
    saved = [e.model_dump(mode="json") for e in r.inserted]
    for line in readback.lines(saved):
        typer.echo(f"✓ {line}" if not line.startswith("  ") else f"  {line.strip()}")
    for label in r.duplicates:
        typer.echo(f"= già presente: {label}")
    for label, reason in r.rejected:
        typer.secho(f"✗ rifiutato {label}: {reason}", fg="red")
    for rid, sev, msg in r.issues:
        if sev != "info":
            typer.secho(f"! {msg} [{rid[-8:]}]", fg="yellow")
    typer.echo(r.summary())


def _commit(records: list[dict], adapter: str, dry_run: bool = False, yes: bool = False) -> Receipt | None:
    """Save records. Workouts, runs and imports are previewed and saved only with --yes (decision 2026-10-04, b)."""
    cfg, conn = _ctx()
    from askesis import services

    pv = services.preview(records)
    if dry_run or (pv.needs_confirmation and not yes):
        typer.echo("ANTEPRIMA — nulla salvato:")
        for line in pv.lines:
            typer.echo(f"· {line}" if not line.startswith("  ") else f"  {line.strip()}")
        if not dry_run:
            typer.echo("Se è corretto, ripetere con --yes per salvare.")
        return None
    r = ingest(conn, records, adapter)
    _print_receipt(r)
    if r.inserted:
        f3.print_flags(conn, max(e.local_date for e in r.inserted))
    return r


def _build(intents: list[Intent], day: date) -> list[dict]:
    cfg = config_mod.load()
    now = now_utc()
    return [rec for it in intents for rec in manual.build(it, cfg, day, now)]  # same path as services.build_day


@app.command()
def day(
    line: Annotated[str, typer.Argument(help="Riga rapida, es. 'p 68.4 · cibo 1850 115'")],
    date_: DateOpt = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Mostra senza salvare")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Conferma: salva anche pesi e corsa")] = False,
) -> None:
    """Registrazione rapida giornaliera (cibo e passi = giorno precedente). Pesi e corsa: anteprima, poi --yes."""
    cfg = config_mod.load()
    try:
        intents = parse_day(line, cfg.context_aliases)
    except ParseError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _commit(_build(intents, _day(date_, cfg)), "manual_day", dry_run, yes)


@log_app.command("weight")
def log_weight(kg: float, date_: DateOpt = None) -> None:
    """Peso a digiuno (kg)."""
    _commit(_build([Intent("weight", {"value_kg": kg})], _day(date_, config_mod.load())), "manual_cli")


@log_app.command("food")
def log_food(
    kcal: float,
    protein_g: Annotated[float | None, typer.Argument()] = None,
    date_: Annotated[str | None, typer.Option("--date", "-d", help="Giorno nutrizionale (default: ieri)")] = None,
    partial: Annotated[bool, typer.Option("--partial", "-p")] = False,
) -> None:
    """Kcal e proteine di un giorno (default: ieri)."""
    cfg = config_mod.load()
    target = date.fromisoformat(date_) if date_ else _day(None, cfg) - timedelta(days=1)
    intent = Intent("food", {"energy_kcal": kcal, "protein_g": protein_g, "partial": partial, "when": "oggi"})
    _commit(_build([intent], target), "manual_cli")


@log_app.command("waist")
def log_waist(readings: list[float], date_: DateOpt = None) -> None:
    """Circonferenza vita: 2–3 letture in cm."""
    _commit(_build([Intent("waist", {"readings_cm": readings})], _day(date_, config_mod.load())), "manual_cli")


@log_app.command("gym")
def log_gym(text: str, date_: DateOpt = None, yes: Annotated[bool, typer.Option("--yes", "-y")] = False) -> None:
    """Sessione pesi, es. 'panca 80x8 r2, 80x7 r1 · rematore 60x10 r2'."""
    try:
        exercises = parse_gym(text)
    except ParseError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _commit(_build([Intent("gym", {"exercises": exercises})], _day(date_, config_mod.load())), "manual_cli", yes=yes)


@log_app.command("run")
def log_run(text: str, date_: DateOpt = None, yes: Annotated[bool, typer.Option("--yes", "-y")] = False) -> None:
    """Corsa, es. '5.2km 31:40 fc145 stop:fiato'."""
    try:
        data = parse_run(text)
    except ParseError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _commit(_build([Intent("run", data)], _day(date_, config_mod.load())), "manual_cli", yes=yes)


@log_app.command("steps")
def log_steps(
    steps: int, date_: Annotated[str | None, typer.Option("--date", "-d", help="Default: ieri")] = None
) -> None:
    """Passi totali di un giorno (default: ieri)."""
    cfg = config_mod.load()
    day_ = date.fromisoformat(date_) + timedelta(days=1) if date_ else _day(None, cfg)
    _commit(_build([Intent("steps", {"steps": steps})], day_), "manual_cli")


@log_app.command("context")
def log_context(key: str, value: str, date_: DateOpt = None) -> None:
    """Variabile di contesto generica (chiave libera)."""
    try:
        val: float | str = float(value.replace(",", "."))
    except ValueError:
        val = value
    _commit(_build([Intent("context", {"key": key, "value": val})], _day(date_, config_mod.load())), "manual_cli")


@log_app.command("checkin")
def log_checkin(
    fatigue: Annotated[int | None, typer.Option(min=1, max=5)] = None,
    soreness: Annotated[int | None, typer.Option(min=1, max=5)] = None,
    stress: Annotated[int | None, typer.Option(min=1, max=5)] = None,
    readiness: Annotated[int | None, typer.Option(min=1, max=10)] = None,
    illness: Annotated[bool, typer.Option("--illness")] = False,
    date_: DateOpt = None,
) -> None:
    """Check-in soggettivo (scale 1–5, readiness 1–10)."""
    cfg = config_mod.load()
    payload = {k: v for k, v in dict(fatigue_1_5=fatigue, soreness_1_5=soreness, stress_1_5=stress,
                                     readiness_1_10=readiness, illness=illness or None).items() if v is not None}
    if not payload:
        raise typer.BadParameter("indica almeno un valore")
    d = _day(date_, cfg)
    rec = manual._env("subjective_checkin", cfg, now_utc(), d, payload,
                      occurred_at=at_local(d, manual.NOON, cfg.timezone))
    _commit([rec], "manual_cli")


@app.command("why")
def why(number: int) -> None:
    """Perché abbiamo cambiato X (intervento n.), cosa sapevamo e cosa è successo."""
    f3.why_cmd(number)


@log_app.command("event")
def log_event(
    kind: Annotated[str, typer.Option("--kind", help="injury | illness | symptom")],
    description: Annotated[str, typer.Option("--desc")],
    region: Annotated[str | None, typer.Option("--region")] = None,
    flag: Annotated[list[str] | None, typer.Option("--flag", help="segnale strutturato (ripetibile)")] = None,
    status: Annotated[str | None, typer.Option("--status")] = None,
    date_: DateOpt = None,
) -> None:
    """Evento di salute confermato dall'atleta (sintomo, malattia, infortunio)."""
    cfg = config_mod.load()
    d = _day(date_, cfg)
    payload = {"kind": kind, "description": description, "body_region": region, "status": status,
               "red_flags": flag or []}
    rec = manual._env("health_event", cfg, now_utc(), d, {k: v for k, v in payload.items() if v},
                      occurred_at=at_local(d, manual.NOON, cfg.timezone))
    _commit([rec], "manual_cli")


@app.command()
def fix(record_id: str, changes: Annotated[str, typer.Argument(help='JSON, es. \'{"value_kg": 68.2}\'')]) -> None:
    """Corregge un record creando una nuova versione (il vecchio resta nello storico)."""
    cfg, conn = _ctx()
    row = repo.get(conn, record_id) or _by_prefix(conn, record_id)
    if row is None:
        raise typer.BadParameter("record non trovato")
    rec = {k: row[k] for k in ("entity_type", "occurred_at", "start_at", "end_at", "tz", "local_date",
                               "source_id", "entry_method")}
    rec = {k: v for k, v in rec.items() if v is not None}
    rec.update(id=new_id(), recorded_at=now_utc(),
               supersedes_id=row["id"], payload=json.loads(row["payload"]) | json.loads(changes))
    _print_receipt(ingest(conn, [rec], "manual_fix"))


@app.command()
def retract(record_id: str, reason: Annotated[str, typer.Option("--reason", "-r")]) -> None:
    """Ritira un record inserito per errore (resta nello storico)."""
    cfg, conn = _ctx()
    row = repo.get(conn, record_id) or _by_prefix(conn, record_id)
    if row is None:
        raise typer.BadParameter("record non trovato")
    with conn:
        repo.retract(conn, row["id"], reason)
    typer.echo(f"✓ ritirato {row['entity_type']} {_short(row['id'])}")


def _short(record_id: str) -> str:
    """UUIDv7 starts with a timestamp: the random tail is the distinguishing part."""
    return record_id[-8:]


def _by_prefix(conn, short: str):
    rows = conn.execute("SELECT * FROM raw_record WHERE id LIKE ? OR id LIKE ?", (short + "%", "%" + short)).fetchall()
    return rows[0] if len(rows) == 1 else None


def _print_rows(rows) -> None:
    for r in rows:
        typer.echo(f"{_short(r['id'])}  {_describe(Envelope.model_validate(_row_env(r)))}")


def _row_env(r) -> dict:
    keys = ("id", "entity_type", "schema_version", "occurred_at", "start_at", "end_at", "tz", "local_date",
            "recorded_at", "source_id", "source_record_id", "entry_method", "supersedes_id", "notes")
    d = {k: r[k] for k in keys if r[k] is not None}
    d["payload"] = json.loads(r["payload"])
    return d


@show_app.command("day")
def show_day(date_: DateOpt = None) -> None:
    """Record correnti di un giorno."""
    cfg, conn = _ctx()
    d = _day(date_, cfg).isoformat()
    _print_rows(repo.current(conn, date_from=d, date_to=d))


@show_app.command("week")
def show_week(date_: DateOpt = None) -> None:
    """Riepilogo della settimana lun–dom che contiene la data."""
    cfg, conn = _ctx()
    d = _day(date_, cfg)
    start = d - timedelta(days=d.weekday())
    end = start + timedelta(days=6)
    typer.echo(f"Settimana {start} → {end}")
    for i in range(7):
        day_ = (start + timedelta(days=i)).isoformat()
        rows = [r for r in repo.current(conn, date_from=day_, date_to=day_)
                if r["entity_type"] in ("body_weight", "nutrition_day", "running_session", "daily_activity",
                                        "body_measurement", "training_session")]
        typer.echo(f"{day_}  " + " | ".join(_describe(Envelope.model_validate(_row_env(r))) for r in rows))


@app.command("import-staging")
def import_staging(
    paths: Annotated[list[Path] | None, typer.Argument(help="File NDJSON (default: tutti in staging_dir)")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Conferma l'import dopo l'anteprima")] = False,
) -> None:
    """Importa lo staging NDJSON v0 nel database (idempotente). Anteprima, poi --yes."""
    cfg, conn = _ctx()
    files = paths or sorted(cfg.staging_dir.glob("*.ndjson"))
    records, digest = staging.read(files)
    typer.echo(f"{len(files)} file · {len(records)} record · sha256 {digest[:12]}")
    if dry_run or not yes:
        for line in readback.lines(records)[:60]:
            typer.echo(f"· {line}")
        typer.echo("ANTEPRIMA — nulla salvato." + ("" if dry_run else " Se è corretto, ripetere con --yes."))
        return
    r = ingest(conn, records, "staging_v0", input_ref=digest)
    for label, reason in r.rejected:
        typer.secho(f"✗ {label}: {reason}", fg="red")
    typer.echo(r.summary())


@app.command("import-health")
def import_health(
    path: Path,
    since: Annotated[str | None, typer.Option("--since", help="Importa solo dal giorno YYYY-MM-DD")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Salva (senza: solo anteprima)")] = False,
    exclude_source: Annotated[list[str] | None, typer.Option("--exclude-source", help="Sorgente da ignorare")] = None,
) -> None:
    """Import dell'esportazione di Salute (export.zip): anteprima per tipo, poi salvataggio con --yes."""
    from askesis.ingestion import apple_health as ah

    cfg, conn = _ctx()
    typer.echo(f"Lettura di {path.name} (in streaming)…")
    data = ah.parse(path)
    plan = ah.build(data, cfg, conn, date.fromisoformat(since) if since else None,
                    exclude_sources=set(exclude_source or []))
    typer.echo(f"Esportazione del {data.export_at:%Y-%m-%d %H:%M}" if data.export_at
               else "Data di esportazione assente")
    for (entity, outcome), n in sorted(plan.counts.items()):
        typer.echo(f"  {entity:18} {outcome}: {n}")
    if data.not_imported:
        top = ", ".join(f"{k.removeprefix('HKQuantityTypeIdentifier')} {v}"
                        for k, v in data.not_imported.most_common(8))
        typer.echo(f"  non importati (nessuna regola ancora): {top}")
    if not plan.records:
        typer.echo("Nulla da salvare.")
        return
    if not yes:
        typer.echo(f"ANTEPRIMA — nulla salvato ({len(plan.records)} record). Per salvare: ripetere con --yes")
        return
    r = ingest(conn, plan.records, "apple_health_export", input_ref=path.name, source_kind="imported")
    typer.echo(r.summary())
    for label, reason in r.rejected[:10]:
        typer.secho(f"✗ rifiutato {label}: {reason}", fg="red")
    if r.inserted:
        f3.print_flags(conn, max(e.local_date for e in r.inserted))


@app.command("import-hevy")
def import_hevy(
    path: Path,
    mapping: Annotated[Path | None, typer.Option("--mapping", help="Abbinamenti esercizi (default: privato)")] = None,
    write_mapping: Annotated[bool, typer.Option("--write-mapping", help="Crea la bozza degli abbinamenti")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Salva (senza: solo anteprima)")] = False,
) -> None:
    """Import dell'esportazione degli allenamenti di Hevy: abbinamento esplicito degli esercizi, anteprima, --yes."""
    from askesis.ingestion import hevy

    cfg, conn = _ctx()
    map_path = mapping or config_mod.ROOT / "private" / "hevy_map.yaml"
    sessions = hevy.parse(path, cfg.timezone)
    confirmed = hevy.load_mapping(map_path)
    report = hevy.mapping_report(sessions, confirmed)
    typer.echo(f"{len(sessions)} sedute, {sum(len(s.sets) for s in sessions)} serie, dal "
               f"{sessions[0].start:%Y-%m-%d} al {sessions[-1].start:%Y-%m-%d}" if sessions else "nessuna seduta")
    for title, n, done, prop in report:
        typer.echo(f"  {title} ({n} serie) → " + (done if done else f"DA CONFERMARE (proposta: {prop or 'nessuna'})"))
    if write_mapping:
        if map_path.exists():
            raise typer.BadParameter(f"{map_path} esiste già: modificalo a mano")
        lines = ["# Abbinamenti esercizi Hevy → catalogo (PRIVATO). Valori: id del catalogo, ignore, raw.",
                 "# Le proposte NON sono confermate: controlla ogni riga, poi togli il commento.", "exercises:"]
        lines += [f"  # {json.dumps(t, ensure_ascii=False)}: {p or 'raw'}" for t, _, d, p in report if not d]
        map_path.parent.mkdir(parents=True, exist_ok=True)
        map_path.write_text("\n".join(lines) + "\n")
        typer.echo(f"✓ bozza scritta in {map_path} (righe commentate: da confermare)")
        return
    plan = hevy.build(sessions, confirmed, cfg, conn)
    if plan.unmapped:
        typer.echo(f"✗ {len(plan.unmapped)} esercizi da abbinare: nulla importato (vedi --write-mapping)")
        raise typer.Exit(1)
    for (what, outcome), n in sorted(plan.counts.items()):
        typer.echo(f"  {what}: {outcome}: {n}")
    if not plan.records:
        typer.echo("Nulla da salvare.")
        return
    if not yes:
        typer.echo(f"ANTEPRIMA — nulla salvato ({len(plan.records)} record). Per salvare: ripetere con --yes")
        return
    r = ingest(conn, plan.records, "hevy_export", input_ref=path.name, source_kind="imported")
    typer.echo(r.summary())
    for label, reason in r.rejected[:10]:
        typer.secho(f"✗ rifiutato {label}: {reason}", fg="red")


backup_app = typer.Typer(invoke_without_command=True, help="Backup del database, test di ripristino, agenti launchd.")
app.add_typer(backup_app, name="backup")


@backup_app.callback()
def backup_cmd(ctx: typer.Context) -> None:
    """Backup del database con rotazione (7 giornalieri + 4 settimanali)."""
    if ctx.invoked_subcommand is not None:
        return
    cfg, conn = _ctx()
    path = backup_mod.backup(conn, cfg.backup_dir, datetime.now())
    removed = backup_mod.rotate(cfg.backup_dir)
    typer.echo(f"✓ backup {path.name} · rimossi {len(removed)} vecchi backup")


@backup_app.command("verify")
def backup_verify(
    snapshot: Annotated[Path | None, typer.Argument(help="Backup da verificare (default: il più recente)")] = None,
    notify: Annotated[bool, typer.Option("--notify", help="Notifica macOS se il test fallisce")] = False,
) -> None:
    """Test di ripristino: integrità, nessun record perso o alterato, stesso digest delle metriche."""
    cfg, conn = _ctx()
    snap = snapshot or backup_mod.latest(cfg.backup_dir)
    if snap is None:
        typer.secho("✗ nessun backup da verificare", fg="red")
        raise typer.Exit(1)
    res = backup_mod.verify(conn, snap, cfg.backup_dir / "restore-tests")
    res["verified_at"] = datetime.now().isoformat(timespec="seconds")
    (cfg.backup_dir / "restore-test-last.json").write_text(json.dumps(res, indent=2))
    typer.echo(("✓" if res["ok"] else "✗") + f" test di ripristino di {snap.name}: " + json.dumps(
        {k: v for k, v in res.items() if k not in ("snapshot", "verified_at")}, ensure_ascii=False))
    if not res["ok"]:
        if notify:
            import subprocess

            subprocess.run(["osascript", "-e", 'display notification "Test di ripristino del backup fallito" '
                            'with title "Askesis"'], check=False)
        raise typer.Exit(1)


@backup_app.command("agent")
def backup_agent(
    install: Annotated[bool, typer.Option("--install", help="Scrive e carica gli agenti (serve --yes)")] = False,
    uninstall: Annotated[bool, typer.Option("--uninstall", help="Scarica e rimuove gli agenti")] = False,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Agenti launchd: backup giornaliero (02:30) e test di ripristino settimanale (domenica 03:00).
    Senza opzioni mostra soltanto cosa verrebbe installato."""
    import shutil

    cfg, _ = _ctx()
    uv = shutil.which("uv") or "uv"
    agents = backup_mod.launch_agents(config_mod.ROOT, uv, cfg.backup_dir)
    from askesis import launchd

    if uninstall:
        launchd.uninstall(list(agents))
        typer.echo("✓ agenti rimossi")
        return
    for label, xml in agents.items():
        typer.echo(f"— {launchd.AGENTS_DIR / (label + '.plist')}\n{xml}")
    if not (install and yes):
        typer.echo("Nulla installato. Per installare: bin/ak backup agent --install --yes")
        return
    for label in launchd.install(agents):
        typer.echo(f"✓ installato e caricato {label}")


@metrics_app.command("compute")
def metrics_compute(
    from_: Annotated[str | None, typer.Option("--from")] = None,
    to: Annotated[str | None, typer.Option("--to")] = None,
) -> None:
    """Calcola le metriche (default: tutto il periodo con dati)."""
    from askesis.analytics import engine

    cfg, conn = _ctx()
    run_id, values, dg = engine.run(conn, date.fromisoformat(from_) if from_ else None,
                                    date.fromisoformat(to) if to else None)
    typer.echo(f"✓ run {run_id[-8:] if run_id else '—'} · {len(values)} valori · digest {dg[:12]}")


@metrics_app.command("rebuild")
def metrics_rebuild() -> None:
    """Cancella la cache delle metriche, ricalcola tutto e verifica la riproducibilità."""
    from askesis.analytics import engine

    cfg, conn = _ctx()
    run_id, n, dg, ok = engine.rebuild(conn)
    typer.echo(f"{'✓' if ok else '✗'} rebuild · {n} valori · digest {dg[:12]} · "
               f"{'riproducibile' if ok else 'NON riproducibile'}")
    if not ok:
        raise typer.Exit(1)


def _save_validated(md: str, out: Path, conn, echo: bool = True) -> None:
    """Generated reports pass the validator too: on failure the final file is not written (DA VERIFICARE)."""
    from askesis.validation import textcheck

    out.parent.mkdir(parents=True, exist_ok=True)
    draft = out.with_name(f".{out.name}.draft")
    draft.write_text(md + "\n")
    res, target = textcheck.finalize(draft, out, conn)
    draft.unlink()
    if echo:
        typer.echo(md)
    if res.ok:
        typer.echo(f"\n✓ validata e salvata in {target.relative_to(config_mod.ROOT)}")
        return
    typer.echo(f"\n✗ DA VERIFICARE — {len(res.issues)} punti non supportati → {target.relative_to(config_mod.ROOT)}")
    for i in res.issues:
        typer.echo(i.render())
    raise typer.Exit(1)


@app.command("review")
def review_cmd(
    date_: Annotated[
        str | None, typer.Option("--date", "-d", help="Un giorno della settimana (default: settimana scorsa)")
    ] = None,
    stdout_only: Annotated[bool, typer.Option("--stdout", help="Non salvare su file")] = False,
    month: Annotated[bool, typer.Option("--month", help="Retrospettiva mensile (default: mese scorso)")] = False,
) -> None:
    """Review settimanale (lun–dom) o retrospettiva mensile, in Markdown, salvata in reports/."""
    from askesis.analytics import engine, review

    if month:
        md = f3.month_cmd(date_)
        typer.echo(md)
        if not stdout_only:
            first = md.split("— ")[1][:7]
            _save_validated(md, config_mod.ROOT / "reports" / f"retro-{first}.md", connect(config_mod.load().db_path),
                            echo=False)
        return
    cfg, conn = _ctx()
    d = date.fromisoformat(date_) if date_ else _day(None, cfg) - timedelta(days=7)
    ws = d - timedelta(days=d.weekday())
    _, values, _ = engine.run(conn, ws - timedelta(days=28), ws + timedelta(days=6))
    issues = [(r["severity"], r["message"]) for r in conn.execute(
        """SELECT i.severity, i.message FROM dq_issue i JOIN raw_record r ON r.id = i.record_id
           WHERE i.status = 'open' AND i.severity != 'info' AND r.local_date BETWEEN ? AND ?""",
        (ws.isoformat(), (ws + timedelta(days=6)).isoformat()))]
    issues += [(f"safety {r['tier']}", f"{r['message']} [flag:{r['id'][-8:]}]") for r in conn.execute(
        "SELECT id, tier, message FROM safety_flag WHERE local_date BETWEEN ? AND ?",  # only this week's flags
        (ws.isoformat(), (ws + timedelta(days=6)).isoformat()))]
    md = review.render(values, ws, issues)
    if stdout_only:
        typer.echo(md)
        return
    y, w, _ = ws.isocalendar()
    out = config_mod.ROOT / "reports" / f"review-{y}-W{w:02d}.md"
    _save_validated(md, out, conn)


@app.command("validate")
def validate_cmd(
    draft: Path,
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="File definitivo (scritto solo se il controllo passa)")
    ] = None,
) -> None:
    """Controlla un testo generato: numeri con riferimento verificato, id e fonti presenti nella KB.

    Con --out: se passa scrive il file definitivo; altrimenti scrive <out>.DA-VERIFICARE.md con i punti non
    supportati ed esce con codice 1."""
    from askesis.validation import textcheck

    cfg, conn = _ctx()
    if out is None:
        res, target = textcheck.validate(draft.read_text(), conn), None
    else:
        res, target = textcheck.finalize(draft, out, conn)
    if res.ok:
        typer.echo("✓ validazione superata" + (f": salvato {target}" if target else ""))
        return
    typer.echo(f"✗ DA VERIFICARE — {len(res.issues)} punti non supportati" + (f" → {target}" if target else ""))
    for i in res.issues:
        typer.echo(i.render())
    raise typer.Exit(1)


web_app = typer.Typer(no_args_is_help=True, help="Dashboard locale (solo questo Mac).")
app.add_typer(web_app, name="web")


@web_app.command("set-password")
def web_set_password() -> None:
    """Imposta la password della dashboard (salvata solo come hash, in data/)."""
    from askesis.web import auth
    from askesis.web.app import paths

    cfg = config_mod.load()
    pw = typer.prompt("Nuova password", hide_input=True, confirmation_prompt=True)
    try:
        auth.set_password(paths(cfg)[0], pw)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    n = auth.revoke(paths(cfg)[2])
    typer.echo(f"✓ password impostata · sessioni aperte chiuse · dispositivi ricordati revocati: {n}")


@web_app.command("devices")
def web_devices(
    revoke: Annotated[str | None, typer.Option("--revoke", help="Revoca il dispositivo con questo ID")] = None,
    revoke_all: Annotated[bool, typer.Option("--revoke-all", help="Revoca tutti i dispositivi ricordati")] = False,
) -> None:
    """Dispositivi ricordati della dashboard: elenco, revoca di uno o di tutti."""
    from askesis.web import auth
    from askesis.web.app import fmt_date, paths

    path = paths(config_mod.load())[2]
    if revoke_all or revoke:
        n = auth.revoke(path, None if revoke_all else revoke)
        typer.echo(f"✓ revocati: {n}" if n else "nessun dispositivo con quell'ID")
        if not n:
            raise typer.Exit(1)
        return
    items = auth.devices(path)
    if not items:
        typer.echo("Nessun dispositivo ricordato.")
    for d in items:
        typer.echo(f"{d['id']}  {d['label']}  ricordato {fmt_date(d['created'])} · ultimo accesso "
                   f"{fmt_date(d['last_used'])} · scade {fmt_date(d['expires'])}")


@web_app.command("serve")
def web_serve(port: Annotated[int, typer.Option("--port")] = 8765) -> None:
    """Avvia la dashboard sugli indirizzi di web_bind (default solo 127.0.0.1, cioè solo da questo Mac)."""
    try:
        import uvicorn

        from askesis.web.app import create_app
    except ImportError as exc:
        raise typer.BadParameter("dipendenze mancanti: uv sync --extra dashboard") from exc
    import threading

    from askesis.web import netwatch

    cfg = config_mod.load()
    addresses = netwatch.resolve(cfg.web_bind)
    sockets, missing = netwatch.bind_sockets(addresses, port)
    if not sockets:
        raise typer.BadParameter(f"nessun indirizzo disponibile tra {cfg.web_bind}")
    served = {s.getsockname()[0] for s in sockets}
    for addr in sorted(served):
        typer.echo(f"Dashboard su http://{addr}:{port}")
    if missing or len(served) < len(cfg.web_bind):
        typer.echo(f"In attesa di {', '.join(missing) or 'indirizzi della VPN'} (interfaccia non attiva): "
                   "riavvio quando compare")
    # Always watching: Meshnet may start after login, reconnect, or change address; a restart rebinds cleanly.
    threading.Thread(target=netwatch.watch, args=(cfg.web_bind, served), daemon=True).start()
    hosts = cfg.web_allowed_hosts + [a for a in served if a not in cfg.web_allowed_hosts]
    server = uvicorn.Server(uvicorn.Config(create_app(cfg, allowed_hosts=hosts), log_level="warning"))
    server.run(sockets=sockets)


@web_app.command("check")
def web_check(
    port: Annotated[int, typer.Option("--port")] = 8765,
    notify: Annotated[bool, typer.Option("--notify", help="Notifica macOS quando lo stato cambia")] = False,
) -> None:
    """Controlla dal Mac che la dashboard sia raggiungibile via VPN: agente, ascolto, indirizzi, firewall,
    client rifiutati nelle ultime 24 ore. Non può vedere il lato iPhone."""
    from askesis.web import health

    cfg = config_mod.load()
    since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    rep = health.check(cfg, port, since)
    if notify and not rep.ok:  # a planned restart (address change) takes ~30 s: confirm before alerting
        import time

        time.sleep(45)
        rep = health.check(cfg, port, since)
    for line in rep.info:
        typer.echo(f"✓ {line}")
    for line in rep.problems:
        typer.secho(f"✗ {line}", fg="red")
    if rep.ok:
        typer.echo("✓ dashboard raggiungibile dal lato Mac")
    if notify:
        health.notify_on_change(rep, cfg.db_path.parent / "web-check.json")
    if not rep.ok:
        raise typer.Exit(1)


CHECK_LABEL = "local.askesis.web-check"


@web_app.command("agent")
def web_agent(
    port: Annotated[int, typer.Option("--port")] = 8765,
    install: Annotated[bool, typer.Option("--install", help="Scrive e carica l'agente (serve --yes)")] = False,
    uninstall: Annotated[bool, typer.Option("--uninstall", help="Scarica e rimuove l'agente")] = False,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Agenti launchd: la dashboard (avvio al login, riavvio se si ferma per errore o cambiano gli indirizzi) e il
    controllo di raggiungibilità ogni 10 minuti. Senza opzioni mostra soltanto cosa verrebbe installato."""
    import shutil

    from askesis import launchd

    cfg = config_mod.load()
    label = "local.askesis.dashboard"
    uv = shutil.which("uv") or "uv"
    ak = str(config_mod.ROOT / "bin" / "ak")
    agents = {
        label: launchd.plist(label, [ak, "web", "serve", "--port", str(port)], str(Path(uv).parent),
                             cfg.db_path.parent / "web.log", run_at_load=True, keep_alive=True),
        CHECK_LABEL: launchd.plist(CHECK_LABEL, [ak, "web", "check", "--port", str(port), "--notify"],
                                   str(Path(uv).parent), cfg.db_path.parent / "web-check.log", interval=600),
    }
    if uninstall:
        launchd.uninstall(list(agents))
        typer.echo("✓ agenti rimossi")
        return
    for name, xml in agents.items():
        typer.echo(f"— {launchd.AGENTS_DIR / (name + '.plist')}\n{xml}")
    if not (install and yes):
        typer.echo("Nulla installato. Per installare: bin/ak web agent --install --yes")
        return
    for name in launchd.install(agents):
        typer.echo(f"✓ installato e caricato {name}")


@app.command("init")
def init() -> None:
    """Crea o aggiorna il database."""
    cfg, conn = _ctx()
    typer.echo(f"✓ database pronto: {cfg.db_path}")


if __name__ == "__main__":
    app()
