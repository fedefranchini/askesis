import csv
import hashlib
import json
import os
import stat
from datetime import datetime

import pytest
import synthetic as syn
from typer.testing import CliRunner

from askesis import config as config_mod
from askesis.cli.main import app
from askesis.ingestion.pipeline import ingest
from askesis.store import export as ex
from askesis.store import repository as repo
from askesis.store.db import connect

NOW = datetime(2030, 5, 6, 7, 8, 9)


@pytest.fixture
def env(tmp_path):
    data = tmp_path / "data"
    conn = connect(data / "t.db")
    ingest(conn, syn.athlete() + syn.linear_weights(5, w0=140.0) + syn.constant_intake(3, kcal=4100.0), "synthetic")
    conn.commit()
    yield conn, data / "t.db", tmp_path / "backups"
    conn.close()


def _dest(env, out=None):
    _, db, bk = env
    return ex.resolve_destination(out, db, bk, NOW)


def _run(env, out=None, fmt="both"):
    d = _dest(env, out)
    return ex.export(env[0], d, fmt, NOW)


def test_export_files_counts_hashes_perms(env):
    conn = env[0]
    res = _run(env)
    d = res.directory
    assert d == env[1].parent.resolve() / "exports" / "2030-05-06_070809"
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    doc = json.loads((d / "askesis-export.json").read_text())
    assert doc["format"] == "askesis-export" and doc["version"] == 1 and doc["schema_version"] >= 3
    for t, rows in doc["tables"].items():
        if t != "v_current":
            assert len(rows) == conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]  # noqa: S608
            with (d / f"{t}.csv").open(encoding="utf-8") as f:
                assert len(list(csv.DictReader(f))) == len(rows)
    assert isinstance(doc["tables"]["raw_record"][0]["payload"], dict)
    man = json.loads((d / "manifest.json").read_text())
    assert man["note"] == ex.NOTE and man["engine_version"]
    for f in man["files"]:
        p = d / f["file"]
        assert hashlib.sha256(p.read_bytes()).hexdigest() == f["sha256"]
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE((d / "manifest.json").stat().st_mode) == 0o600
    assert hashlib.sha256((d / "manifest.json").read_bytes()).hexdigest() == res.manifest_sha256
    with (d / "current_body_weight.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 5 and rows[0]["value_kg"] == "140.0"


def test_format_selection(env):
    d = _run(env, fmt="json").directory
    assert sorted(p.name for p in d.iterdir()) == ["askesis-export.json", "manifest.json"]


def test_retracted_and_corrected_in_history_not_current(env):
    conn = env[0]
    ids = [r[0] for r in conn.execute("SELECT id FROM v_current WHERE entity_type = 'body_weight' ORDER BY local_date")]
    repo.retract(conn, ids[0], "test")
    conn.commit()
    res = _run(env)
    doc = json.loads((res.directory / "askesis-export.json").read_text())
    assert ids[0] in {r["id"] for r in doc["tables"]["raw_record"]}
    assert ids[0] not in {r["id"] for r in doc["tables"]["v_current"]}
    assert doc["tables"]["retraction"][0]["record_id"] == ids[0]
    with (res.directory / "current_body_weight.csv").open(encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 4


def test_no_secret_tables_or_columns(env):
    doc = json.loads((_run(env).directory / "askesis-export.json").read_text())
    for t, rows in doc["tables"].items():
        assert not ex.SECRET.search(t)
        assert not any(ex.SECRET.search(c) for r in rows[:1] for c in r)


def test_secret_table_is_skipped(env):
    conn = env[0]
    conn.execute("CREATE TABLE device_token (id TEXT, hash TEXT)")
    conn.execute("INSERT INTO device_token VALUES ('a', 'b')")
    conn.commit()
    res = _run(env)
    assert res.skipped == ["device_token"]
    assert not (res.directory / "device_token.csv").exists()


def _nothing_written(env, tmp_path, before):
    assert {p for p in tmp_path.rglob("*")} == before


def snapshot(tmp_path):
    return {p for p in tmp_path.rglob("*")}


def test_refuses_outside_allowed(env, tmp_path):
    other = tmp_path / "elsewhere" / "x"
    before = snapshot(tmp_path)
    with pytest.raises(ex.ExportError, match="non privata"):
        _dest(env, other)
    assert snapshot(tmp_path) == before


def test_refuses_symlink_escape(env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (env[1].parent / "link").symlink_to(outside)
    before = snapshot(tmp_path)
    with pytest.raises(ex.ExportError, match="non privata"):
        _dest(env, env[1].parent / "link" / "x")
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("sub", ["Library/Mobile Documents/com~apple~CloudDocs", "Library/CloudStorage/Foo",
                                 "Dropbox", "google drive", "iCloudDrive"])
def test_refuses_cloud_paths(env, tmp_path, sub):
    before = snapshot(tmp_path)
    with pytest.raises(ex.ExportError, match="sincronizzata con un cloud"):
        _dest(env, env[1].parent / sub / "x")
    assert snapshot(tmp_path) == before


def test_refuses_repo_path_not_ignored(env):
    target = config_mod.ROOT / "docs" / "x-export-test"
    with pytest.raises(ex.ExportError, match="rifiutata"):
        _dest(env, target)
    assert not target.exists()


def test_git_ignore_check_and_allowed_db_inside_repo(tmp_path):
    root = config_mod.ROOT
    assert ex._git_ignored(root / "private" / "x-export-test", root)
    assert not ex._git_ignored(root / "docs" / "x-export-test", root)
    db = root / "docs" / "fake.db"  # DB folder inside the repo but not ignored: never created, only resolved
    with pytest.raises(ex.ExportError, match="non ignorata"):
        ex.resolve_destination(None, db, tmp_path / "b", NOW)
    assert not (root / "docs" / "exports").exists()


def test_refuses_when_git_unavailable(env, monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ex.ExportError):
        _dest(env, config_mod.ROOT / "private" / "x-export-test")


def test_allows_ignored_private_dir(env):
    target = config_mod.ROOT / "private" / "x-export-test"
    assert _dest(env, target) == target.resolve()
    assert not target.exists()


def test_refuses_existing_directory(env):
    existing = env[1].parent / "exports" / "again"
    existing.mkdir(parents=True)
    with pytest.raises(ex.ExportError, match="esiste già"):
        _dest(env, existing)


def test_backup_dir_allowed(env):
    assert _dest(env, env[2] / "exp") == (env[2] / "exp").resolve()


@pytest.fixture
def cli(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "data" / "cli.db"))
    monkeypatch.setenv("ASKESIS_BACKUP_DIR", str(tmp_path / "backups"))
    runner = CliRunner()
    return lambda *a: runner.invoke(app, list(a), catch_exceptions=False)


def test_cli_export_and_refusal(cli, tmp_path):
    cli("log", "weight", "140.5", "--date", "2030-01-13")
    r = cli("export")
    assert r.exit_code == 0, r.output
    assert "askesis-export.json" in r.output and "manifest sha256" in r.output
    assert "140.5" not in r.output and "AirDrop" in r.output
    (d,) = (tmp_path / "data" / "exports").iterdir()
    assert (d / "manifest.json").exists()
    out = tmp_path / "public" / "x"
    bad = cli("export", "--out", str(out))
    assert bad.exit_code == 1 and "esportazione rifiutata" in bad.output
    assert not out.exists() and not out.parent.exists()
    assert cli("export", "--format", "xml").exit_code == 1
    assert os.path.isdir(tmp_path / "data" / "exports")
