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
from askesis.ingestion import manual, staging
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
    for env in r.inserted:
        typer.echo(f"✓ {_describe(env)}")
    for label in r.duplicates:
        typer.echo(f"= già presente: {label}")
    for label, reason in r.rejected:
        typer.secho(f"✗ rifiutato {label}: {reason}", fg="red")
    for rid, sev, msg in r.issues:
        if sev != "info":
            typer.secho(f"! {msg} [{rid[-8:]}]", fg="yellow")
    typer.echo(r.summary())


def _commit(records: list[dict], adapter: str, dry_run: bool = False) -> Receipt | None:
    cfg, conn = _ctx()
    if dry_run:
        for rec in records:
            typer.echo(f"· {_describe(Envelope.model_validate(rec))}")
        typer.echo(f"(dry-run: {len(records)} record, nulla salvato)")
        return None
    r = ingest(conn, records, adapter)
    _print_receipt(r)
    return r


def _build(intents: list[Intent], day: date) -> list[dict]:
    cfg = config_mod.load()
    now = now_utc()
    return [rec for it in intents for rec in manual.build(it, cfg, day, now)]


@app.command()
def day(
    line: Annotated[str, typer.Argument(help="Riga rapida, es. 'p 68.4 · cibo 1850 115'")],
    date_: DateOpt = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Mostra senza salvare")] = False,
) -> None:
    """Registrazione rapida giornaliera (cibo e passi = giorno precedente)."""
    cfg = config_mod.load()
    try:
        intents = parse_day(line, cfg.context_aliases)
    except ParseError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _commit(_build(intents, _day(date_, cfg)), "manual_day", dry_run)


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
def log_gym(text: str, date_: DateOpt = None) -> None:
    """Sessione pesi, es. 'panca 80x8 r2, 80x7 r1 · rematore 60x10 r2'."""
    try:
        exercises = parse_gym(text)
    except ParseError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _commit(_build([Intent("gym", {"exercises": exercises})], _day(date_, config_mod.load())), "manual_cli")


@log_app.command("run")
def log_run(text: str, date_: DateOpt = None) -> None:
    """Corsa, es. '5.2km 31:40 fc145 stop:fiato'."""
    try:
        data = parse_run(text)
    except ParseError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _commit(_build([Intent("run", data)], _day(date_, config_mod.load())), "manual_cli")


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
) -> None:
    """Importa lo staging NDJSON v0 nel database (idempotente)."""
    cfg, conn = _ctx()
    files = paths or sorted(cfg.staging_dir.glob("*.ndjson"))
    records, digest = staging.read(files)
    typer.echo(f"{len(files)} file · {len(records)} record · sha256 {digest[:12]}")
    if dry_run:
        return
    r = ingest(conn, records, "staging_v0", input_ref=digest)
    for label, reason in r.rejected:
        typer.secho(f"✗ {label}: {reason}", fg="red")
    typer.echo(r.summary())


@app.command("backup")
def backup_cmd() -> None:
    """Backup del database con rotazione (7 giornalieri + 4 settimanali)."""
    cfg, conn = _ctx()
    path = backup_mod.backup(conn, cfg.backup_dir, datetime.now())
    removed = backup_mod.rotate(cfg.backup_dir)
    typer.echo(f"✓ backup {path.name} · rimossi {len(removed)} vecchi backup")


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


@app.command("review")
def review_cmd(
    date_: Annotated[
        str | None, typer.Option("--date", "-d", help="Un giorno della settimana (default: settimana scorsa)")
    ] = None,
    stdout_only: Annotated[bool, typer.Option("--stdout", help="Non salvare su file")] = False,
) -> None:
    """Review settimanale (lun–dom) in Markdown, salvata in reports/."""
    from askesis.analytics import engine, review

    cfg, conn = _ctx()
    d = date.fromisoformat(date_) if date_ else _day(None, cfg) - timedelta(days=7)
    ws = d - timedelta(days=d.weekday())
    _, values, _ = engine.run(conn, ws - timedelta(days=28), ws + timedelta(days=6))
    issues = [(r["severity"], r["message"]) for r in conn.execute(
        """SELECT i.severity, i.message FROM dq_issue i JOIN raw_record r ON r.id = i.record_id
           WHERE i.status = 'open' AND i.severity != 'info' AND r.local_date BETWEEN ? AND ?""",
        (ws.isoformat(), (ws + timedelta(days=6)).isoformat()))]
    md = review.render(values, ws, issues)
    if stdout_only:
        typer.echo(md)
        return
    y, w, _ = ws.isocalendar()
    out = config_mod.ROOT / "reports" / f"review-{y}-W{w:02d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md + "\n")
    typer.echo(md)
    typer.echo(f"\n✓ salvata in {out.relative_to(config_mod.ROOT)}")


@app.command("init")
def init() -> None:
    """Crea o aggiorna il database."""
    cfg, conn = _ctx()
    typer.echo(f"✓ database pronto: {cfg.db_path}")


if __name__ == "__main__":
    app()
