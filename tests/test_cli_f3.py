from datetime import timedelta

import pytest
import synthetic as syn
import yaml
from test_f3 import prereg
from typer.testing import CliRunner

from askesis.cli.main import app
from askesis.config import load
from askesis.ingestion.pipeline import ingest
from askesis.store.db import connect


@pytest.fixture
def cli(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "cli.db"))
    conn = connect(load().db_path)
    ingest(conn, syn.athlete() + syn.linear_weights(29) + syn.constant_intake(29), "synthetic")
    conn.close()
    runner = CliRunner()
    return lambda *a: runner.invoke(app, list(a), catch_exceptions=False)


def test_full_intervention_flow_and_why(cli, tmp_path):
    f = tmp_path / "prereg.yaml"
    f.write_text(yaml.safe_dump(prereg(), allow_unicode=True))
    assert "proposto intervento n. 1" in cli("intervention", "propose", str(f), "--title", "Fase",
                                              "--category", "phase_start_fat_loss").output
    out = cli("intervention", "approve", "1", "--verbatim", "approvo", "--reasoning", "deficit moderato").output
    assert "attivato · 3 versioni" in out
    assert "nutrition_target: «target» v1" in cli("plan", "show", "--date", syn.START.isoformat()).output
    nxt = cli("plan", "next", "--date", syn.START.isoformat()).output
    assert "bench_press: 3×6" in nxt and "carico da calibrare" in nxt
    ev = cli("intervention", "evaluate", "1", "--date", (syn.START + timedelta(days=28)).isoformat()).output
    assert "compatibile con l'esito atteso" in ev
    why = cli("why", "1").output
    assert "Perché" in why and "approvo" in why and "concluded" in why
    assert "concluded" in cli("intervention", "list").output


def test_safety_event_shows_flag_immediately(cli):
    out = cli("log", "event", "--kind", "symptom", "--desc", "test sintetico", "--flag", "chest_pain",
              "--date", syn.START.isoformat())
    assert "URGENTE" in out.output and "112" in out.output
    assert "URGENTE" in cli("safety", "check", "--date", syn.START.isoformat()).output


def test_monthly_retrospective(cli):
    out = cli("review", "--month", "--date", "2025-03-15", "--stdout").output
    assert "Retrospettiva mensile — 2025-03" in out and "| Settimana" in out and "weight_ema@1" in out
