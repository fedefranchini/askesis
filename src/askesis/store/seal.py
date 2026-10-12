"""Sealed backups: compressed and encrypted to the athlete's public age keys before they leave the server.

The server only ever holds public keys (recipients): it can write sealed backups but never read them back. The
private key stays with the athlete (password manager or keychain, plus a paper copy). Plain snapshots stay where they
are created (`backup_dir`, same disk as the database); only `*.db.gz.age` files are copied elsewhere (pull to the
development machine, cloud folder). Every sealed file is listed in `manifest.json` with its SHA-256, so a copy can
be checked without the key. Only files created by this module are ever removed."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

PATTERN = re.compile(r"^askesis-(\d{8})-(\d{6})\.db\.gz\.age$")
MANIFEST = "manifest.json"
RECIPIENT = re.compile(r"^age1[02-9ac-hj-np-z]{58}$")  # X25519 public key (bech32)


class SealError(RuntimeError):
    pass


def age_binary() -> str:
    found = shutil.which("age") or next((p for p in ("/opt/homebrew/bin/age", "/usr/local/bin/age", "/usr/bin/age")
                                         if Path(p).exists()), None)
    if not found:
        raise SealError("comando age non trovato (macOS: brew install age · Debian/Ubuntu: apt install age)")
    return found


def check_recipients(recipients: list[str]) -> list[str]:
    """At least one valid X25519 public key: without it nothing is sealed (never an unencrypted copy)."""
    if not recipients:
        raise SealError("nessuna chiave pubblica age configurata (backup_age_recipients): backup non cifrato rifiutato")
    bad = [r for r in recipients if not RECIPIENT.match(r)]
    if bad:
        raise SealError(f"chiave pubblica age non valida: {bad[0][:12]}… (attesa una chiave age1…)")
    return recipients


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sealed_name(snapshot: Path) -> str:
    return f"{snapshot.stem}.db.gz.age"


def seal(snapshot: Path, out_dir: Path, recipients: list[str]) -> Path:
    """gzip + age to every recipient; written to a temporary name and renamed only when complete."""
    check_recipients(recipients)
    age = age_binary()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / sealed_name(snapshot)
    partial = target.with_name(f".{target.name}.partial")
    args = [age, "--encrypt", "--output", str(partial)]
    for r in recipients:
        args += ["--recipient", r]
    data = gzip.compress(snapshot.read_bytes(), mtime=0)
    try:
        res = subprocess.run(args, input=data, capture_output=True, timeout=300)
        if res.returncode != 0:
            raise SealError(f"age: {res.stderr.decode(errors='replace').strip() or res.returncode}")
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    write_manifest(out_dir)
    return target


def _sealed(directory: Path) -> list[tuple[datetime, Path]]:
    files = []
    for p in directory.iterdir() if directory.exists() else []:
        if m := PATTERN.match(p.name):
            files.append((datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S"), p))
    return sorted(files, reverse=True)


def rotate(directory: Path, keep_daily: int = 14, keep_weekly: int = 8, keep_monthly: int = 12) -> list[Path]:
    """Newest sealed backup of each of the last N days, ISO weeks and months."""
    files = _sealed(directory)
    keep: set[Path] = set()
    seen: dict[str, list] = {"d": [], "w": [], "m": []}
    limits = {"d": keep_daily, "w": keep_weekly, "m": keep_monthly}
    for ts, p in files:
        for kind, key in (("d", ts.date()), ("w", ts.isocalendar()[:2]), ("m", (ts.year, ts.month))):
            if key not in seen[kind] and len(seen[kind]) < limits[kind]:
                seen[kind].append(key)
                keep.add(p)
    removed = [p for _, p in files if p not in keep]
    for p in removed:
        p.unlink()
    if removed:
        write_manifest(directory)
    return removed


def write_manifest(directory: Path) -> dict:
    entries = [{"file": p.name, "bytes": p.stat().st_size, "sha256": _sha256(p)} for _, p in _sealed(directory)]
    manifest = {"format": 1, "files": entries}
    tmp = directory / f".{MANIFEST}.partial"
    tmp.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(tmp, directory / MANIFEST)
    return manifest


def check_copies(directory: Path) -> dict:
    """Without the key: every file listed in the manifest is present with the same hash (a pulled or cloud copy)."""
    path = directory / MANIFEST
    if not path.exists():
        return {"ok": False, "problem": "manifest mancante"}
    listed = json.loads(path.read_text())["files"]
    bad = [e["file"] for e in listed if not (directory / e["file"]).exists()
           or _sha256(directory / e["file"]) != e["sha256"]]
    return {"ok": not bad and bool(listed), "files": len(listed), "missing_or_changed": bad}


def latest(directory: Path) -> Path | None:
    files = _sealed(directory)
    return files[0][1] if files else None


def open_sealed(path: Path, identity: Path, dest: Path) -> Path:
    """Decrypt and decompress to `dest` (a file path). The identity file is read by age only."""
    age = age_binary()
    res = subprocess.run([age, "--decrypt", "--identity", str(identity), str(path)], capture_output=True, timeout=300)
    if res.returncode != 0:
        raise SealError(f"decifratura non riuscita: {res.stderr.decode(errors='replace').strip() or res.returncode}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(gzip.decompress(res.stdout))
    return dest


def check_sealed(path: Path, identity: Path) -> dict:
    """Restore test of a sealed backup with the private key: decrypt into a private temporary folder, integrity
    check, record counts and last recording time. The plaintext copy is always deleted."""
    out: dict = {"sealed": path.name}
    with tempfile.TemporaryDirectory(prefix="askesis-restore-") as tmp:
        os.chmod(tmp, 0o700)
        db = open_sealed(path, identity, Path(tmp) / "restore.db")
        conn = sqlite3.connect(str(db))
        try:
            out["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
            for table in ("raw_record", "plan_version", "intervention_event"):
                out[f"{table}_count"] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            out["last_recorded_at"] = conn.execute("SELECT MAX(recorded_at) FROM raw_record").fetchone()[0]
        finally:
            conn.close()
    out["ok"] = out.get("integrity") == "ok" and out.get("raw_record_count", 0) > 0
    return out


class identity_file:  # noqa: N801 - used as a context manager
    """The private key as a file readable only by this user for the duration of the block: a path as given, or
    copied from the macOS keychain (`keychain:<service>`) into a private temporary folder deleted afterwards."""

    def __init__(self, spec: str):
        if not spec:
            raise SealError("chiave privata non configurata (backup_identity): file o keychain:<servizio>")
        self.spec, self._tmp = spec, None

    def __enter__(self) -> Path:
        if not self.spec.startswith("keychain:"):
            path = Path(self.spec).expanduser()
            if not path.exists():
                raise SealError(f"file della chiave privata non trovato: {path}")
            return path
        res = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", self.spec.removeprefix("keychain:"),
                              "-w"], capture_output=True, text=True, timeout=60)
        if res.returncode != 0 or not res.stdout.strip():
            raise SealError("chiave privata non trovata nel portachiavi")
        self._tmp = tempfile.TemporaryDirectory(prefix="askesis-key-")
        os.chmod(self._tmp.name, 0o700)
        key = Path(self._tmp.name) / "identity.txt"
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(res.stdout.strip() + "\n")
        return key

    def __exit__(self, *exc) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
