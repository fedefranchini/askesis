"""SQLite connection and forward-only migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from askesis.core.timeutil import iso, now_utc

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def connect(path: Path | str) -> sqlite3.Connection:
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
