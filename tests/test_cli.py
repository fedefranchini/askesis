import os
from datetime import datetime, timedelta

import pytest
from typer.testing import CliRunner

from askesis.cli.main import app
from askesis.store import backup


@pytest.fixture
def cli(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n[context_aliases]\nxvar = "custom_key"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("ASKESIS_BACKUP_DIR", str(tmp_path / "backups"))
    runner = CliRunner()
    return lambda *args: runner.invoke(app, list(args), catch_exceptions=False)


def test_day_line_end_to_end(cli):
    r = cli("day", "p 74.6 · cibo 1850 115 · xvar 3", "--date", "2026-01-13")
    assert r.exit_code == 0, r.output
    assert "✓ peso 74.6 kg (2026-01-13)" in r.output
    assert "cibo 2026-01-12: 1850 kcal · 115 g proteine [complete]" in r.output
    assert "custom_key = 3.0" in r.output
    again = cli("day", "p 74.6", "--date", "2026-01-13")
    assert "già presente" in again.output


def test_dry_run_saves_nothing(cli):
    r = cli("day", "p 74.6", "--date", "2026-01-13", "--dry-run")
    assert "nulla salvato" in r.output
    assert "Settimana" in cli("show", "week", "--date", "2026-01-13").output
    assert "74.6" not in cli("show", "day", "--date", "2026-01-13").output


def test_bad_line_is_reported(cli):
    r = CliRunner().invoke(app, ["day", "boh 3"])
    assert r.exit_code != 0


def test_fix_and_retract(cli):
    cli("log", "weight", "74.6", "--date", "2026-01-13")
    line = cli("show", "day", "--date", "2026-01-13").output.strip().splitlines()[0]
    rid = line.split()[0]
    assert "peso 74.4" in cli("fix", rid, '{"value_kg": 74.4}').output
    shown = cli("show", "day", "--date", "2026-01-13").output
    assert "74.4" in shown and "74.6" not in shown
    new_id = shown.strip().splitlines()[0].split()[0]
    assert "ritirato" in cli("retract", new_id, "--reason", "test").output
    assert cli("show", "day", "--date", "2026-01-13").output.strip() == ""


def test_hard_dq_rejection_visible(cli):
    assert "rifiutato" in cli("log", "weight", "740", "--date", "2026-01-13").output


def test_backup_command(cli, tmp_path):
    cli("init")
    r = cli("backup")
    assert "backup askesis-" in r.output
    assert len(os.listdir(tmp_path / "backups")) == 1


def test_backup_rotation(tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    start = datetime(2026, 1, 1, 22, 0)
    for i in range(40):  # 40 daily backups
        (d / f"askesis-{start + timedelta(days=i):%Y%m%d-%H%M%S}.db").write_text("x")
    (d / "unrelated.txt").write_text("keep me")
    backup.rotate(d)
    left = sorted(p.name for p in d.iterdir())
    assert "unrelated.txt" in left
    assert len([n for n in left if n.startswith("askesis-")]) <= 7 + 4
    assert f"askesis-{start + timedelta(days=39):%Y%m%d-%H%M%S}.db" in left
