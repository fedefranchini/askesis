"""CLI for the daily Health sync (iOS Shortcut → dashboard): device tokens, last results, partial-day confirmation."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

import typer

from askesis import config as config_mod
from askesis.ingestion import health_sync
from askesis.store.db import connect

sync_app = typer.Typer(no_args_is_help=True, help="Sincronizzazione quotidiana da Salute (Comandi Rapidi).")


def _ctx():
    cfg = config_mod.load()
    return cfg, connect(cfg.db_path)


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "mai"


@sync_app.command("token-create")
def token_create(label: Annotated[str, typer.Option("--label", help="Nome del dispositivo, es. iPhone")] = "iPhone"
                 ) -> None:
    """Crea un token per un dispositivo: viene mostrato una sola volta (va incollato nel Comando Rapido)."""
    cfg, _ = _ctx()
    secret, rec = health_sync.create_token(health_sync.tokens_path(cfg), label)
    typer.echo(f"Token per «{rec['label']}» (id {rec['id']}), mostrato solo ora:\n\n{secret}\n")
    typer.echo("Copialo nel Comando Rapido (campo Authorization: «Bearer » seguito dal token). "
               f"Per revocarlo: bin/ak sync token-revoke {rec['id']}")


@sync_app.command("token-list")
def token_list() -> None:
    """Token attivi: dispositivo, creazione, ultimo uso (mai il segreto)."""
    cfg, _ = _ctx()
    rows = health_sync.tokens(health_sync.tokens_path(cfg))
    if not rows:
        typer.echo("Nessun token attivo.")
    for t in rows:
        typer.echo(f"{t['id']}  {t['label']}  creato {_when(t['created'])}  ultimo uso {_when(t['last_used'])}")


@sync_app.command("token-revoke")
def token_revoke(token_id: str) -> None:
    """Revoca un token: il Comando Rapido che lo usa smette subito di funzionare."""
    cfg, _ = _ctx()
    ok = health_sync.revoke_token(health_sync.tokens_path(cfg), token_id)
    typer.echo("✓ revocato" if ok else "token non trovato")
    if not ok:
        raise typer.Exit(1)


@sync_app.command("status")
def status() -> None:
    """Esito dell'ultimo invio per dispositivo: conteggi, tipi letti, unità ed etichette (nessun valore)."""
    cfg, _ = _ctx()
    for t in health_sync.tokens(health_sync.tokens_path(cfg)):
        typer.echo(f"{t['label']} ({t['id']}) — ultimo invio {_when(t['last_used'])}")
        r = t.get("last_result")
        if not r:
            continue
        if "error" in r:
            typer.echo(f"  ✗ {r['error']}")
            continue
        typer.echo(f"  {'PROVA (nulla salvato)' if r['probe'] else 'salvato'}: {health_sync.summary_line(r)}")
        for kind, v in r["report"]["types"].items():
            extra = f" · problemi {v['problems']}" if v["problems"] else ""
            typer.echo(f"  {kind:9} {v['understood']}/{v['received']} · unità {v['units']} · etichette "
                       f"{v['labels']} · sorgenti {len(v['sources'])}{extra}")
        for line in r["report"].get("not_synced", []):
            typer.echo(f"  – {line}")
        for k, n in r.get("plan", {}).items():
            typer.echo(f"  {k}: {n}")


@sync_app.command("food-confirm")
def food_confirm(
    date_: Annotated[str, typer.Option("--for", help="Giornata YYYY-MM-DD")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Conferma dopo l'anteprima")] = False,
) -> None:
    """Conferma come completa una giornata di cibo importata da Salute (resta parziale finché non la confermi)."""
    from askesis.ingestion import readback
    from askesis.ingestion.pipeline import ingest

    cfg, conn = _ctx()
    try:
        rec = health_sync.confirmation_record(conn, cfg, date.fromisoformat(date_))
    except health_sync.SyncError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    for line in readback.lines([rec]):
        typer.echo(f"· {line}")
    if not yes:
        typer.echo("ANTEPRIMA — nulla salvato. Se la giornata è completa: ripetere con --yes")
        return
    r = ingest(conn, [rec], "manual")
    typer.echo(r.summary())
