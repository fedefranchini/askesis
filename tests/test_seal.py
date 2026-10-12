"""Sealed backups (age to public keys only) and the server deploy kit (forced command, units, installer)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from conftest import weight

from askesis.ingestion.pipeline import ingest
from askesis.store import backup as backup_mod
from askesis.store import seal
from askesis.store.db import connect

ROOT = Path(__file__).resolve().parents[1]
needs_age = pytest.mark.skipif(shutil.which("age") is None and not Path("/opt/homebrew/bin/age").exists(),
                               reason="age non installato")


def keypair(tmp: Path) -> tuple[str, Path]:
    keygen = shutil.which("age-keygen") or "/opt/homebrew/bin/age-keygen"
    ident = tmp / "id.txt"
    out = subprocess.run([keygen, "-o", str(ident)], capture_output=True, text=True, check=True)
    pub = re.search(r"age1\w+", out.stderr + ident.read_text()).group(0)
    return pub, ident


def snapshot(tmp: Path, when: str = "20990101-023000") -> tuple[sqlite3.Connection, Path]:
    conn = connect(tmp / "live.db")
    ingest(conn, [weight(240.0, "2099-01-01")], "test")
    snap = backup_mod.backup(conn, tmp / "backups", datetime.strptime(when, "%Y%m%d-%H%M%S"))
    return conn, snap


@needs_age
def test_seal_refuses_without_valid_public_key(tmp_path):
    _, snap = snapshot(tmp_path)
    for bad in ([], ["not-a-key"], ["AGE-SECRET-KEY-1QQQQ"]):
        with pytest.raises(seal.SealError):
            seal.seal(snap, tmp_path / "sealed", bad)
    assert not (tmp_path / "sealed").exists() or not list((tmp_path / "sealed").glob("*.age"))


@needs_age
def test_sealed_backup_is_unreadable_without_the_key_and_restores_with_it(tmp_path):
    pub, ident = keypair(tmp_path)
    _, snap = snapshot(tmp_path)
    out = seal.seal(snap, tmp_path / "sealed", [pub])
    blob = out.read_bytes()
    assert blob.startswith(b"age-encryption.org/v1") and b"SQLite format" not in blob and b"body_weight" not in blob
    manifest = json.loads((tmp_path / "sealed" / "manifest.json").read_text())
    assert [e["file"] for e in manifest["files"]] == [out.name]
    res = seal.check_sealed(out, ident)
    assert res["ok"] and res["integrity"] == "ok" and res["raw_record_count"] == 1
    assert res["last_recorded_at"].startswith("2099-01-01")
    (tmp_path / "other").mkdir()
    _, other = keypair(tmp_path / "other")
    with pytest.raises(seal.SealError):
        seal.check_sealed(out, other)


@needs_age
def test_copies_are_checked_against_the_manifest_without_the_key(tmp_path):
    pub, _ = keypair(tmp_path)
    _, snap = snapshot(tmp_path)
    out = seal.seal(snap, tmp_path / "sealed", [pub])
    copy = tmp_path / "pulled"
    shutil.copytree(tmp_path / "sealed", copy)
    assert seal.check_copies(copy)["ok"]
    (copy / out.name).write_bytes(b"age-encryption.org/v1 corrupted")
    assert seal.check_copies(copy) == {"ok": False, "files": 1, "missing_or_changed": [out.name]}


def test_rotation_keeps_days_weeks_months_and_only_its_own_files(tmp_path):
    d = tmp_path / "sealed"
    d.mkdir()
    names = [f"askesis-2099{m:02d}{day:02d}-023000.db.gz.age" for m in (1, 2, 3) for day in range(1, 29)]
    for n in names:
        (d / n).write_bytes(b"x")
    (d / "notes.txt").write_text("keep")
    removed = seal.rotate(d, keep_daily=14, keep_weekly=8, keep_monthly=12)
    kept = sorted(p.name for p in d.glob("*.age"))
    assert (d / "notes.txt").exists() and len(removed) == len(names) - len(kept)
    assert kept[-14:] == sorted(names)[-14:]                       # last 14 days
    assert "askesis-20990128-023000.db.gz.age" in kept           # newest of January (month rule)
    assert "askesis-20990127-023000.db.gz.age" not in kept and "askesis-20990101-023000.db.gz.age" not in kept
    assert json.loads((d / "manifest.json").read_text())["files"]


def load_gate():
    loader = importlib.machinery.SourceFileLoader("ak_gate", str(ROOT / "deploy" / "ak-gate"))
    spec = importlib.util.spec_from_loader("ak_gate", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("command,allowed", [
    ("ak review --stdout", True), ("bin/ak show week", True), ("ak log weight 300 --date 2099-01-01", True),
    ("bash", False), ("sh -c 'ak show week'", False), ("scp -f /var/lib/askesis/askesis.db", False),
    ("sqlite3 /var/lib/askesis/askesis.db", False), ("", False), ("ak 'unterminated", False),
])
def test_agent_key_runs_only_ak_without_a_shell(tmp_path, monkeypatch, command, allowed):
    gate = load_gate()
    env_file = tmp_path / "env"
    env_file.write_text("ASKESIS_CONFIG=/etc/askesis/askesis.toml\n# comment\n")
    calls = []
    monkeypatch.setattr(gate, "ENV_FILE", str(env_file))
    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(gate.os, "execve", lambda path, argv, env: calls.append((path, argv, env)))
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", command)
    rc = gate.main()
    if allowed:
        path, argv, env = calls[0]
        assert path == gate.AK and argv[0] == gate.AK and argv[1:] == command.split()[1:]
        assert env["ASKESIS_CONFIG"] == "/etc/askesis/askesis.toml" and "SSH_ORIGINAL_COMMAND" not in env
    else:
        assert rc == 126 and not calls


def test_server_units_and_firewall_expose_nothing_by_default():
    units = {p.name: p.read_text() for p in (ROOT / "deploy" / "systemd").iterdir()}
    for name, text in units.items():
        if name.endswith(".service"):
            assert "User=askesis" in text and "NoNewPrivileges=yes" in text and "ProtectSystem=strict" in text
            for cmd in re.findall(r"^ExecStart=(\S+)", text, re.M):
                assert cmd == "/opt/askesis/bin/ak"
    backup = units["askesis-backup.service"]
    order = [line.split("ak ", 1)[1] for line in backup.splitlines() if line.startswith("ExecStart=")]
    assert order == ["backup", "backup verify --notify", "backup seal", "backup offsite"]  # never seal unverified
    fw = (ROOT / "deploy" / "nftables.conf").read_text()
    assert "policy drop" in fw and 'iifname "nordlynx" ip saddr @peers tcp dport { 22, 8765 } accept' in fw
    assert "flush ruleset" not in fw and "delete table inet askesis" in fw  # never wipes the VPN client's rules
    assert "0.0.0.0" not in fw and "8765 accept" not in fw.replace("{ 22, 8765 } accept", "")
    sshd = (ROOT / "deploy" / "sshd-askesis.conf").read_text()
    for line in ("PasswordAuthentication no", "PermitRootLogin no", "AllowTcpForwarding no"):
        assert line in sshd


def test_installer_passes_shellcheck_and_never_pipes_the_network_into_a_shell():
    text = (ROOT / "deploy" / "install.sh").read_text()
    assert not re.search(r"curl[^\n|]*\|\s*(ba)?sh", text)
    assert "UV_VERSION=" in text and "--frozen" in text
    if shutil.which("shellcheck"):
        res = subprocess.run(["shellcheck", "-S", "warning", str(ROOT / "deploy" / "install.sh")],
                             capture_output=True, text=True)
        assert res.returncode == 0, res.stdout


def test_server_paths_come_from_the_environment(monkeypatch, tmp_path):
    from askesis import config

    monkeypatch.setenv("ASKESIS_BACKUP_SEALED_DIR", str(tmp_path / "sealed"))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "db.sqlite"))
    cfg = config.load(private=tmp_path / "missing.toml")
    assert cfg.backup_sealed_dir == tmp_path / "sealed" and cfg.db_path == tmp_path / "db.sqlite"
    assert cfg.backup_age_recipients == [] and cfg.backup_offsite == ""
    assert os.environ.get("ASKESIS_BACKUP_SEALED_DIR")
