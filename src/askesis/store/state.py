"""Private app files (password hash, devices, passkeys, tokens, logs, coach texts) on disk or in the database.

On the Mac they stay files with owner-only permissions, as always. When the database is hosted (no persistent disk
on the server) the same calls keep them in the `app_state` / `app_log` tables, keyed by "<folder>/<file name>": the
callers keep passing the same paths and never know which backend is in use.
"""

from __future__ import annotations

import fnmatch
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_conn: Callable[[], Any] | None = None  # database backend when set (hosted deployment)


def use_database(conn_factory: Callable[[], Any] | None) -> None:
    """Keep the private files in the database (hosted) or, with None, on disk (default)."""
    global _conn
    _conn = conn_factory


def in_database() -> bool:
    return _conn is not None


def _key(path: Path) -> str:
    return f"{path.parent.name}/{path.name}"


def exists(path: Path) -> bool:
    if _conn is None:
        return path.exists()
    return _conn().execute("SELECT 1 FROM app_state WHERE key = ?", (_key(path),)).fetchone() is not None


def read_text(path: Path) -> str:
    if _conn is None:
        return path.read_text()
    row = _conn().execute("SELECT content FROM app_state WHERE key = ?", (_key(path),)).fetchone()
    if row is None:
        raise FileNotFoundError(str(path))
    return row[0]


def read_bytes(path: Path) -> bytes:
    return path.read_bytes() if _conn is None else read_text(path).encode()


def mtime(path: Path) -> float:
    if _conn is None:
        return path.stat().st_mtime
    row = _conn().execute("SELECT updated_at FROM app_state WHERE key = ?", (_key(path),)).fetchone()
    if row is None:
        raise FileNotFoundError(str(path))
    return float(row[0])


def write_private(path: Path, text: str) -> None:
    """Whole-file write readable only by the owner (atomic replace on disk)."""
    if _conn is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
        return
    c = _conn()
    with c:
        c.execute("INSERT INTO app_state(key, content, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                  "SET content = excluded.content, updated_at = excluded.updated_at", (_key(path), text, time.time()))


def delete(path: Path) -> None:
    if _conn is None:
        path.unlink(missing_ok=True)
        return
    c = _conn()
    with c:
        c.execute("DELETE FROM app_state WHERE key = ?", (_key(path),))


def append_line(path: Path, line: str) -> None:
    if _conn is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(line + "\n")
        return
    c = _conn()
    with c:
        c.execute("INSERT INTO app_log(key, line) VALUES (?, ?)", (_key(path), line))


def tail_lines(path: Path, limit: int) -> list[str]:
    if _conn is None:
        return path.read_text(errors="replace").splitlines()[-limit:] if path.exists() else []
    rows = _conn().execute("SELECT line FROM app_log WHERE key = ? ORDER BY id DESC LIMIT ?",
                           (_key(path), limit)).fetchall()
    return [r[0] for r in reversed(rows)]


def listing(directory: Path, pattern: str = "*") -> list[Path]:
    """Files of a folder matching a glob pattern (names only, like Path.glob on one level)."""
    if _conn is None:
        return sorted(directory.glob(pattern)) if directory.exists() else []
    prefix = f"{directory.name}/"
    rows = _conn().execute("SELECT key FROM app_state WHERE key LIKE ? ESCAPE '\\'",
                           (prefix.replace("_", "\\_").replace("%", "\\%") + "%",)).fetchall()
    names = [r[0][len(prefix):] for r in rows]
    return sorted(directory / n for n in names if "/" not in n and fnmatch.fnmatch(n, pattern))
