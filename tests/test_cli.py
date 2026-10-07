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
    assert "✓ Peso 13/1: 74,6 kg a digiuno" in r.output
    assert "✓ Cibo 12/1: 1.850 kcal, 115 g proteine" in r.output
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
    assert "Peso 13/1: 74,4 kg" in cli("fix", rid, '{"value_kg": 74.4}').output
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


def test_workout_requires_confirmation(cli):
    line = "pesi: panca 60x8 r2, 60x7 r1 · trazioni bwx6 r2 · corsa 4km 24:00 fc150"
    preview = cli("day", line, "--date", "2026-01-14").output
    assert "ANTEPRIMA" in preview and "--yes" in preview
    assert "Panca piana: 60 kg × 8 @RIR 2 · 60 kg × 7 @RIR 1" in preview
    assert "Trazioni: corpo libero × 6 @RIR 2" in preview
    assert "Corsa 14/1: 4 km in 24:00 (6:00/km), FC media 150" in preview
    assert "Panca" not in cli("show", "day", "--date", "2026-01-14").output  # nothing saved
    saved = cli("day", line, "--date", "2026-01-14", "--yes").output
    assert "✓ Sessione pesi 14/1:" in saved and "inseriti" in saved


def test_simple_values_saved_immediately(cli):
    out = cli("day", "p 74.6 · passi 9000", "--date", "2026-01-15").output
    assert "ANTEPRIMA" not in out and "✓ Passi 14/1: 9.000" in out


def test_unknown_exercise_is_marked(cli):
    out = cli("day", "pesi: esercizio strano 20x10 r2", "--date", "2026-01-16").output
    assert "esercizio strano (non in catalogo): 20 kg × 10 @RIR 2" in out


def test_free_meal_estimate_end_to_end(cli):
    r = cli("day", "cibo 1500 95 +400/20", "--date", "2026-01-13")
    assert r.exit_code == 0, r.output
    assert "✓ Cibo 12/1: 1.900 kcal (di cui ~400 kcal stimate, pasto libero), 115 g proteine" in r.output


def test_restore_test_passes_and_detects_tampering(cli, tmp_path):
    import sqlite3

    cli("day", "p 74.6 · cibo 1850 115", "--date", "2026-01-13")
    cli("backup")
    ok = cli("backup", "verify")
    assert ok.exit_code == 0 and "✓ test di ripristino" in ok.output and '"metrics_digest_equal": true' in ok.output
    snap = sorted((tmp_path / "backups").glob("askesis-*.db"))[-1]
    raw = sqlite3.connect(snap)
    raw.execute("DROP TRIGGER IF EXISTS raw_record_ro_u")
    for (name,) in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='raw_record'"):
        raw.execute(f"DROP TRIGGER {name}")
    raw.execute("UPDATE raw_record SET payload_hash = 'tampered' WHERE entity_type = 'body_weight'")
    raw.commit()
    raw.close()
    bad = cli("backup", "verify")
    assert bad.exit_code == 1 and '"raw_record": 1' in bad.output


def test_launch_agents_are_only_shown_without_install(cli, tmp_path):
    out = cli("backup", "agent").output
    assert "local.askesis.backup" in out and "local.askesis.restore-test" in out and "Nulla installato" in out
    assert "<key>StartCalendarInterval</key>" in out


def test_dashboard_agent_is_only_shown_and_binds_localhost(cli):
    out = cli("web", "agent").output
    assert "local.askesis.dashboard" in out and "<key>RunAtLoad</key><true/>" in out and "Nulla installato" in out
    assert "--host" not in out and "0.0.0.0" not in out  # serve binds 127.0.0.1 only
    assert "local.askesis.web-check" in out and "<key>StartInterval</key><integer>600</integer>" in out


@pytest.mark.parametrize("cmd", [
    ["day"], ["fix"], ["retract"], ["review"], ["validate"], ["why"], ["import-staging"], ["import-health"],
    ["import-hevy"],
    ["metrics", "compute"], ["metrics", "rebuild"], ["backup", "verify"], ["backup", "agent"], ["web", "serve"],
    ["web", "agent"], ["web", "set-password"], ["web", "check"], ["safety", "check"], ["plan", "show"],
    ["plan", "next"],
    ["plan", "calorie-check"], ["plan", "calorie-apply"], ["intervention", "propose"], ["intervention", "approve"],
    ["intervention", "activate"], ["intervention", "evaluate"], ["intervention", "list"], ["show", "week"],
])
def test_commands_used_by_routines_and_docs_exist(cmd):
    """Regression guard: a refactoring must never silently drop a command."""
    r = CliRunner().invoke(app, [*cmd, "--help"])
    assert r.exit_code == 0, f"comando mancante: {' '.join(cmd)}"
