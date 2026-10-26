"""SQLite connection (local file, or the hosted Turso database) and forward-only migrations."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

from askesis.core.timeutil import iso, now_utc

MIGRATIONS_DIR = Path(os.environ.get("ASKESIS_ROOT", Path(__file__).resolve().parents[3])) / "migrations"


REMOTE = ("libsql://", "https://", "wss://")


def resolve_token(source: str) -> str:
    """The Turso token from the environment ("env:NAME") or the macOS Keychain ("keychain:service"); never a file."""
    kind, _, name = source.partition(":")
    if kind == "env" and os.environ.get(name):
        return os.environ[name]
    if kind == "keychain":
        res = subprocess.run(["security", "find-generic-password", "-s", name, "-w"], capture_output=True,
                             text=True, check=False)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    raise RuntimeError(f"token del database non trovato ({kind}:{name})")


def connect(path: Path | str, token_source: str = "env:ASKESIS_DB_TOKEN") -> sqlite3.Connection:
    if str(path).startswith(REMOTE):
        from askesis.store import remote, state

        conn = remote.connect(str(path), resolve_token(token_source))
        conn.execute("PRAGMA foreign_keys = ON")
        migrate(conn)
        state.use_database(lambda: conn)  # private files (auth, devices, texts…) live in the hosted database
        return conn  # type: ignore[return-value]  # same interface as sqlite3.Connection
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    applied = []
    have = current_version(conn)
    for f in sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        version = int(f.name[:4])
        if version <= have:
            continue
        with conn:
            conn.executescript(f.read_text())
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (version, f.name, iso(now_utc())),
            )
        applied.append(f.name)
    return applied
