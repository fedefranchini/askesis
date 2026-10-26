"""Turso (libSQL) connection with the sqlite3 interface the rest of the code relies on.

The libsql driver speaks SQLite's dialect (same schema, triggers, JSON functions) but returns plain tuples and has no
`row_factory` or `backup()`. This wrapper adds exactly those: rows readable by name and by index (like
`sqlite3.Row`), `with conn:` transactions, and an online copy into a local SQLite file for the nightly backup.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from typing import Any


class Row:
    """Like sqlite3.Row: row[0], row["name"], row.keys(), dict(row), iteration over the values."""

    __slots__ = ("_index", "_values")

    def __init__(self, index: dict[str, int], values: Sequence[Any]) -> None:
        self._index, self._values = index, tuple(values)

    def __getitem__(self, key: int | str | slice) -> Any:
        if isinstance(key, str):
            try:
                return self._values[self._index[key]]
            except KeyError:
                raise IndexError(f"No item with that key: {key}") from None
        return self._values[key]

    def keys(self) -> list[str]:
        return list(self._index)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Row):
            return self._values == other._values and self.keys() == other.keys()
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._values)

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._index, self._values, strict=True))!r})"


class Cursor:
    def __init__(self, cur: Any) -> None:
        self._cur = cur
        desc = cur.description or ()
        self.description = desc
        self._index = {d[0]: i for i, d in enumerate(desc)}

    def _row(self, values: Sequence[Any] | None) -> Row | None:
        return None if values is None else Row(self._index, values)

    def fetchone(self) -> Row | None:
        return self._row(self._cur.fetchone())

    def fetchall(self) -> list[Row]:
        return [Row(self._index, v) for v in self._cur.fetchall()]

    def fetchmany(self, size: int = 1) -> list[Row]:
        return [Row(self._index, v) for v in self._cur.fetchmany(size)]

    def __iter__(self) -> Iterator[Row]:
        return iter(self.fetchall())

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self._cur.lastrowid


class Connection:
    """The subset of sqlite3.Connection used by Askesis, over a libsql connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> Cursor:
        return Cursor(self._conn.execute(sql, tuple(params) if isinstance(params, list) else params))

    def executemany(self, sql: str, seq: Any) -> Cursor:
        return Cursor(self._conn.executemany(sql, [tuple(p) for p in seq]))

    def executescript(self, script: str) -> None:
        self._conn.executescript(script)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def backup(self, dest: sqlite3.Connection) -> None:
        """Online copy into a local SQLite database: tables and rows first, then indexes, triggers and views (so
        the append-only triggers never fire while the copy is filled)."""
        objects = self.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL "
                               "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'libsql_%'").fetchall()
        order = {"table": 0, "index": 1, "trigger": 2, "view": 3}
        with dest:
            for o in sorted(objects, key=lambda o: order.get(o["type"], 9)):
                if o["type"] != "table":
                    continue
                dest.execute(o["sql"])
                rows = self._conn.execute(f'SELECT * FROM "{o["name"]}"').fetchall()
                if rows:
                    marks = ", ".join("?" * len(rows[0]))
                    dest.executemany(f'INSERT INTO "{o["name"]}" VALUES ({marks})', rows)
            for o in sorted(objects, key=lambda o: order.get(o["type"], 9)):
                if o["type"] != "table":
                    dest.execute(o["sql"])


def connect(url: str, auth_token: str) -> Connection:
    import libsql  # optional dependency (extra "cloud"): only the hosted deployment needs it

    return Connection(libsql.connect(url, auth_token=auth_token))


def connect_local(path: str) -> Connection:
    """A local libSQL file through the same wrapper: used by the tests to exercise this module without a network."""
    import libsql

    return Connection(libsql.connect(path))
