"""Full export (JSON + CSV) of the database, written only to private folders.

Destination rules are enforced here, not by convention: allowed roots only (DB folder, backup folder, private/),
never a cloud-synced path, git-ignored when inside the work tree, never an existing directory."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from askesis import config as config_mod

NOTE = "Dati personali: non condividere; trasferimento solo via AirDrop, mai iCloud Drive, mail o altri cloud."
SECRET = re.compile(r"password|passwd|token|secret|session_id|credential", re.I)
CLOUD_PARTS = ("/library/mobile documents/", "/library/cloudstorage/")
CLOUD_NAMES = {"dropbox", "google drive", "onedrive", "icloud drive", "iclouddrive"}
JSON_COLUMNS = {"payload", "content", "prereg", "detail", "details", "evidence", "params", "value_json"}
FORMATS = ("both", "json", "csv")


class ExportError(Exception):
    """Refusal with an Italian user-facing message; nothing has been written."""


@dataclass
class Result:
    directory: Path
    files: list[dict]
    manifest_sha256: str
    skipped: list[str]


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def is_cloud_path(path: Path) -> bool:
    low = path.as_posix().lower() + "/"
    return any(p in low for p in CLOUD_PARTS) or any(part.lower() in CLOUD_NAMES for part in path.parts)


def _git_ignored(path: Path, root: Path) -> bool:
    try:
        r = subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=root, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def resolve_destination(out: Path | None, db_path: Path, backup_dir: Path, now: datetime,
                        root: Path | None = None) -> Path:
    """Validate and return the (not yet existing) export directory. Raises ExportError."""
    root = (root or config_mod.ROOT).resolve()
    allowed = [db_path.resolve().parent, backup_dir.resolve(), (root / "private").resolve()]
    dest = (out.expanduser() if out else db_path.parent / "exports" / f"{now:%Y-%m-%d_%H%M%S}").resolve()
    if not any(_inside(dest, a) for a in allowed):
        raise ExportError("esportazione rifiutata: cartella non privata (ammesse solo cartella del database, "
                          "dei backup e private/)")
    if is_cloud_path(dest):
        raise ExportError("esportazione rifiutata: cartella sincronizzata con un cloud")
    if _inside(dest, root) and not _git_ignored(dest, root):
        raise ExportError("esportazione rifiutata: cartella dentro la repository ma non ignorata da git")
    if dest.exists():
        raise ExportError(f"esportazione rifiutata: {dest.name} esiste già (nessuna sovrascrittura)")
    return dest


def _maybe_json(col: str, value):
    if isinstance(value, str) and (col in JSON_COLUMNS or value[:1] in "{["):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _plain(value):
    return value.hex() if isinstance(value, bytes) else value


def read_all(conn: sqlite3.Connection) -> tuple[dict, dict, list[str], int]:
    """(tables, columns, skipped, schema_version) read in one transaction; tables include the v_current view."""
    conn.execute("BEGIN")
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        tables: dict[str, list[dict]] = {}
        columns: dict[str, list[str]] = {}
        skipped: list[str] = []
        for name in [*names, "v_current"]:
            cur = conn.execute(f'SELECT * FROM "{name}"')  # noqa: S608
            cols = [d[0] for d in cur.description]
            if SECRET.search(name) or any(SECRET.search(c) for c in cols):
                skipped.append(name)
                continue
            columns[name] = cols
            tables[name] = [{c: _plain(v) for c, v in zip(cols, row, strict=True)} for row in cur.fetchall()]
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
    finally:
        conn.execute("COMMIT")
    return tables, columns, skipped, version


def _cell(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return "" if v is None else v


def _csv(cols: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in cols])
    return buf.getvalue()


def _current_csvs(rows: list[dict], cols: list[str]) -> dict[str, tuple[list[str], list[dict]]]:
    out: dict[str, tuple[list[str], list[dict]]] = {}
    for r in rows:
        flat = {c: r[c] for c in cols if c != "payload"}
        payload = json.loads(r["payload"]) if isinstance(r.get("payload"), str) else (r.get("payload") or {})
        if not isinstance(payload, dict):
            payload = {"payload": payload}
        for k, v in payload.items():
            flat[f"payload_{k}" if k in flat else k] = v
        ecols, erows = out.setdefault(r["entity_type"], ([], []))
        ecols.extend(c for c in flat if c not in ecols)
        erows.append(flat)
    return out


def _write(directory: Path, name: str, text: str) -> None:
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.chmod(directory / name, 0o600)


def export(conn: sqlite3.Connection, dest: Path, fmt: str = "both", now: datetime | None = None) -> Result:
    """Write the export into `dest` (already validated by resolve_destination)."""
    from askesis.analytics.engine import ENGINE_VERSION

    if fmt not in FORMATS:
        raise ExportError(f"formato non valido: {fmt} (ammessi: {', '.join(FORMATS)})")
    now = now or datetime.now(UTC)
    exported_at = now.astimezone(UTC).isoformat(timespec="seconds")
    tables, columns, skipped, version = read_all(conn)
    texts: dict[str, tuple[str, int]] = {}
    if fmt in ("both", "json"):
        parsed = {t: [{c: _maybe_json(c, v) for c, v in r.items()} for r in rows] for t, rows in tables.items()}
        doc = {"format": "askesis-export", "version": 1, "exported_at": exported_at, "schema_version": version,
               "tables": parsed}
        texts["askesis-export.json"] = (json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                        sum(len(r) for r in tables.values()))
    if fmt in ("both", "csv"):
        for t, rows in tables.items():
            if t != "v_current":
                texts[f"{t}.csv"] = (_csv(columns[t], rows), len(rows))
        for et, (cols, rows) in sorted(_current_csvs(tables["v_current"], columns["v_current"]).items()):
            texts[f"current_{et}.csv"] = (_csv(cols, rows), len(rows))
    dest.mkdir(parents=True, exist_ok=False)
    os.chmod(dest, 0o700)
    files = []
    for name, (text, n) in sorted(texts.items()):
        _write(dest, name, text)
        files.append({"file": name, "rows": n, "sha256": hashlib.sha256(text.encode()).hexdigest()})
    manifest = {"exported_at": exported_at, "schema_version": version, "engine_version": ENGINE_VERSION,
                "format": fmt, "files": files, "skipped_tables": skipped, "note": NOTE}
    text = json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    _write(dest, "manifest.json", text)
    return Result(dest, files, hashlib.sha256(text.encode()).hexdigest(), skipped)
