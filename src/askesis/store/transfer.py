"""Move the whole database to the hosted one (Turso), row for row, and prove the copy is identical.

The destination has the same schema (same migrations). Its data tables must be empty, or hold only the synthetic
rehearsal data, which is cleared first; its private files (app_state/app_log: password hash, passkeys…) are kept.
Rows keep their ids and are inserted parents first; the result is checked table by table with a digest of every
row, so the copy is accepted only if source and destination are identical.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

KEEP = {"schema_migrations", "app_state", "app_log"}  # never copied (hosted private files stay as they are)
CHUNK = 200


class TransferError(RuntimeError):
    pass


def data_tables(conn: Any) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                        "AND name NOT LIKE 'libsql_%' AND name NOT LIKE '\\_%' ESCAPE '\\'").fetchall()
    return sorted(r[0] for r in rows if r[0] not in KEEP)


def _parents(conn: Any, table: str) -> set[str]:
    return {r[2] for r in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()} - {table}


def ordered(conn: Any, tables: list[str]) -> list[str]:
    """Parents before children (foreign keys); a table referring to itself keeps its rowid order."""
    out: list[str] = []
    pending = list(tables)
    while pending:
        ready = [t for t in pending if _parents(conn, t) & set(tables) <= set(out)]
        if not ready:
            raise TransferError(f"dipendenze circolari tra le tabelle: {pending}")
        for t in ready:
            out.append(t)
            pending.remove(t)
    return out


def _columns(conn: Any, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def _norm(v: Any) -> Any:
    """Decimal numbers compared to 12 significant digits: the network transport of the hosted database may change
    the last bit of a double (e.g. …7213 → …7217); texts, integers and ids are compared exactly."""
    return float(f"{v:.12g}") if isinstance(v, float) else v


def digest(conn: Any, table: str) -> tuple[int, str]:
    """Row count and SHA-256 of every row (all columns, in a fixed order), independent of storage order."""
    cols = _columns(conn, table)
    order = ", ".join(f'"{c}"' for c in cols)
    h = hashlib.sha256()
    n = 0
    for row in conn.execute(f'SELECT {order} FROM "{table}" ORDER BY {order}').fetchall():
        h.update(json.dumps([_norm(v) for v in row], ensure_ascii=False, default=str).encode())
        h.update(b"\n")
        n += 1
    return n, h.hexdigest()


def _schema_version(conn: Any) -> int:
    return conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0


def plan(src: sqlite3.Connection, dst: Any) -> dict:
    """What a transfer would do; nothing is written."""
    if _schema_version(src) != _schema_version(dst):
        raise TransferError(f"schema diverso: sorgente v{_schema_version(src)}, destinazione v{_schema_version(dst)}")
    tables = data_tables(src)
    if set(tables) != set(data_tables(dst)):
        raise TransferError("le tabelle di sorgente e destinazione non coincidono")
    counts = {t: src.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    dst_records = dst.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0]
    real_in_dst = dst.execute("SELECT COUNT(*) FROM raw_record WHERE source_id != 'synthetic'").fetchone()[0]
    if real_in_dst:
        raise TransferError(f"la destinazione contiene già {real_in_dst} record non sintetici: trasferimento rifiutato")
    return {"tables": ordered(src, tables), "counts": counts, "synthetic_to_clear": dst_records,
            "schema": _schema_version(src)}


def clear_synthetic(dst: Any) -> None:
    """Empty the data tables of a destination that holds only the synthetic rehearsal (checked by `plan`). The
    append-only triggers are lifted for the operation and restored exactly as they were."""
    triggers = dst.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'").fetchall()
    tables = ordered(dst, data_tables(dst))
    with dst:
        for t in triggers:
            dst.execute(f'DROP TRIGGER "{t[0]}"')
        for table in reversed(tables):
            dst.execute(f'DELETE FROM "{table}"')
        for t in triggers:
            dst.execute(t[1])


def copy(src: sqlite3.Connection, dst: Any, tables: list[str]) -> None:
    with dst:
        for table in tables:
            cols = _columns(src, table)
            names = ", ".join(f'"{c}"' for c in cols)
            marks = ", ".join("?" * len(cols))
            rows = src.execute(f'SELECT {names} FROM "{table}" ORDER BY rowid').fetchall()
            per = max(1, min(CHUNK, 30000 // len(cols)))  # one statement per chunk: one round trip, not one per row
            for i in range(0, len(rows), per):
                part = rows[i:i + per]
                values = ", ".join(f"({marks})" for _ in part)
                dst.execute(f'INSERT INTO "{table}" ({names}) VALUES {values}', [v for r in part for v in r])


def verify(src: sqlite3.Connection, dst: Any, tables: list[str]) -> dict[str, dict]:
    out = {}
    for t in tables:
        a, b = digest(src, t), digest(dst, t)
        out[t] = {"rows": a[0], "identical": a == b}
    return out


def first_difference(src: sqlite3.Connection, dst: Any, table: str) -> dict | None:
    """The first row (in a fixed order) that differs, column by column with the value types: for diagnosis."""
    cols = _columns(src, table)
    order = ", ".join(f'"{c}"' for c in cols)
    a = src.execute(f'SELECT {order} FROM "{table}" ORDER BY {order}').fetchall()
    b = dst.execute(f'SELECT {order} FROM "{table}" ORDER BY {order}').fetchall()
    for ra, rb in zip(a, b, strict=False):
        if [_norm(v) for v in ra] != [_norm(v) for v in rb]:
            return {c: (repr(x), type(x).__name__, repr(y), type(y).__name__)
                    for c, x, y in zip(cols, ra, rb, strict=True) if _norm(x) != _norm(y) or type(x) is not type(y)}
    return None

