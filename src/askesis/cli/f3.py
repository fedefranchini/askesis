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
        typer.echo({"no_programme": "nessun programma attivo", "rest_day": "giorno senza sessione"}[out["status"]])
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
@iv_app.command("propose")
def iv_propose(
    file: Path, title: Annotated[str, typer.Option("--title")], category: Annotated[str, typer.Option("--category")]
) -> None:
    """Registra una proposta (pre-registrazione da file YAML)."""
    cfg, conn = _ctx()
    iid = reg.propose(conn, title, category, yaml.safe_load(file.read_text()))
    typer.echo(f"✓ proposto intervento n. {reg.get(conn, iid)['number']} ({iid[-8:]})")


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
    typer.echo(f"✓ intervento n. {number} attivato · {len(versions)} versioni di piano create")


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
def month_cmd(date_: str | None) -> str:
    from askesis.analytics import engine, review

    cfg, conn = _ctx()
    d = _d(date_, cfg)
    first = (d.replace(day=1) - timedelta(days=1)).replace(day=1) if not date_ else d.replace(day=1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    _, values, _ = engine.run(conn, first - timedelta(days=28), last)
    ivs = [dict(r) for r in conn.execute("SELECT number, title, status FROM v_intervention_status ORDER BY number")]
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
            "SELECT tier, local_date, message FROM safety_flag WHERE local_date BETWEEN ? AND ?",
            (first.isoformat(), last.isoformat()),
        )
    ]
    return review.render_month(values, first, last, ivs, prof, flags)
