"""`bin/ak cloud`: move the data of this Mac to the hosted database (Turso), with preview and full verification."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from askesis import config as config_mod

cloud_app = typer.Typer(no_args_is_help=True,
                        help="Database online (Turso): trasferimento verificato dai dati del Mac.")


def _dest(url: str | None, cfg: config_mod.Config):
    from askesis.store.db import connect

    target = url or cfg.db_url
    if not target:
        raise typer.BadParameter("indirizzo del database online mancante (--to o db_url)")
    return connect(target, cfg.db_token)


@cloud_app.command("push")
def push(
    to: Annotated[str | None, typer.Option("--to", help="libsql://… (default: db_url della configurazione)")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Esegue il trasferimento (senza: solo anteprima)")] = False,
) -> None:
    """Copia tutti i dati del database del Mac in quello online e verifica ogni tabella riga per riga.

    La destinazione deve essere vuota o contenere solo la prova sintetica (che viene cancellata); password, passkey e
    file privati già online restano. Il database del Mac non viene modificato."""
    from askesis.store import transfer
    from askesis.store.db import connect

    cfg = config_mod.load()
    src = connect(cfg.db_path)  # always the local file, whatever db_url says
    dst = _dest(to, cfg)
    try:
        pl = transfer.plan(src, dst)
    except transfer.TransferError as exc:
        typer.secho(f"✗ {exc}", fg="red")
        raise typer.Exit(1) from exc
    total = sum(pl["counts"].values())
    typer.echo(f"Schema v{pl['schema']} · {len(pl['tables'])} tabelle · {total} righe da copiare")
    for t in pl["tables"]:
        typer.echo(f"  {t}: {pl['counts'][t]}")
    if pl["synthetic_to_clear"]:
        typer.echo(f"Online ci sono {pl['synthetic_to_clear']} record sintetici: verranno cancellati prima.")
    if not yes:
        typer.echo("ANTEPRIMA: nulla è stato scritto. Ripeti con --yes per trasferire.")
        return
    if pl["synthetic_to_clear"]:
        transfer.clear_synthetic(dst)
    transfer.copy(src, dst, pl["tables"])
    res = transfer.verify(src, dst, pl["tables"])
    bad = [t for t, r in res.items() if not r["identical"]]
    for t, r in res.items():
        typer.echo(f"  {'✓' if r['identical'] else '✗'} {t}: {r['rows']} righe")
    if bad:
        for t in bad:
            typer.echo(f"  prima differenza in {t}: {transfer.first_difference(src, dst, t)}")
        typer.secho(f"✗ copia NON identica in: {', '.join(bad)}", fg="red")
        raise typer.Exit(1)
    typer.echo(f"✓ trasferimento completo e identico: {total} righe in {len(res)} tabelle "
               "(numeri decimali a 12 cifre significative)")


@cloud_app.command("check")
def check(to: Annotated[str | None, typer.Option("--to")] = None) -> None:
    """Confronta ogni tabella del Mac con quella online (sola lettura)."""
    from askesis.store import transfer
    from askesis.store.db import connect

    cfg = config_mod.load()
    src, dst = connect(cfg.db_path), _dest(to, cfg)
    tables = transfer.ordered(src, transfer.data_tables(src))
    res = transfer.verify(src, dst, tables)
    for t, r in res.items():
        typer.echo(f"  {'✓' if r['identical'] else '✗'} {t}: {r['rows']} righe")
        if not r["identical"]:
            typer.echo(f"    prima differenza: {transfer.first_difference(src, dst, t)}")
    if not all(r["identical"] for r in res.values()):
        raise typer.Exit(1)
    typer.echo("✓ identici (numeri decimali confrontati a 12 cifre significative)")


@cloud_app.command("push-files")
def push_files(
    to: Annotated[str | None, typer.Option("--to", help="libsql://… (default: db_url della configurazione)")] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Copia online i testi del coach (reports/*.md) e le evidenze private (private/evidence/*.yaml).
    Password, passkey e dispositivi non si copiano: online si impostano a parte."""
    from askesis.store import state

    cfg = config_mod.load()
    evidence = config_mod.ROOT / "private" / "evidence"
    files = sorted(config_mod.reports_dir().glob("*.md")) + sorted(evidence.glob("*.yaml"))
    texts = {f: f.read_text() for f in files}  # read from disk before the store is bound to the database
    typer.echo(f"{len(files)} file: {sum(1 for f in files if f.suffix == '.md')} testi del coach, "
               f"{sum(1 for f in files if f.suffix == '.yaml')} evidenze private")
    if not yes:
        typer.echo("ANTEPRIMA: nulla è stato scritto. Ripeti con --yes.")
        return
    _dest(to, cfg)  # binds the private store to the hosted database
    for f, text in texts.items():
        state.write_private(Path(f), text)
    missing = [f.name for f, text in texts.items() if state.read_text(Path(f)) != text]
    state.use_database(None)
    if missing:
        typer.secho(f"✗ non identici online: {', '.join(missing)}", fg="red")
        raise typer.Exit(1)
    typer.echo(f"✓ {len(texts)} file copiati e verificati")
