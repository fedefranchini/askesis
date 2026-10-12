"""Reporting an illness: safety notice (T1, stronger with fever or symptoms below the neck), food marked as not
logged (never zero), symptoms update and 'Sto meglio' closing the episode and its notice. Dashboard and CLI."""

from __future__ import annotations

import json
import re
import secrets
from datetime import date, datetime, timedelta

import pytest
import synthetic as syn
from starlette.testclient import TestClient
from typer.testing import CliRunner

from askesis import config
from askesis.cli.main import app as cli_app
from askesis.ingestion import illness_log
from askesis.ingestion.pipeline import ingest
from askesis.safety import rules as safety
from askesis.store.db import connect
from askesis.web import auth
from askesis.web.app import create_app, paths

NOW = "2099-03-10T09:00:00+01:00"
TODAY = date(2099, 3, 10)


@pytest.fixture
def env(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "ill.db"))
    monkeypatch.setenv("ASKESIS_NOW", NOW)
    cfg = config.load()
    conn = connect(cfg.db_path)
    ingest(conn, syn.athlete(), "synthetic")
    password = secrets.token_urlsafe(16)  # generated test credential
    auth.set_password(paths(cfg)[0], password)
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1")
    r = client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
    client.post("/login", data={"csrf": csrf, "username": "atleta", "password": password})
    return client, cfg, conn


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _illness_flags(conn):
    return [f for f in safety.open_flags(conn) if f["rule_id"] == "safety.illness@1"]


def test_symptoms_decide_fever_and_below_the_neck():
    pl = illness_log._payload(["raffreddore", "gola"], "")
    assert pl["fever"] is False and pl["below_neck"] is False and pl["description"] == "raffreddore, mal di gola"
    pl = illness_log._payload(["febbre"], "brividi")
    assert pl["fever"] and pl["below_neck"] and pl["description"] == "febbre, brividi"
    with pytest.raises(illness_log.IllnessError):
        illness_log._payload([], "  ")


def test_report_update_and_recover_from_the_dashboard(env):
    client, cfg, conn = env
    page = client.get("/").text
    assert "Non sto bene oggi" in page and 'name="sym_febbre"' in page
    r = client.post("/", data={"csrf": _csrf(page), "action": "ill_report", "sym_raffreddore": "1", "sym_gola": "1",
                               "food_today": "1"})
    assert "Malessere dal 10/3: raffreddore, mal di gola" in r.text
    assert "Cibo 10/3: non registrato (nessuna stima)" in r.text
    assert "Malessere dal 10/03" in r.text and "Niente allenamento intenso" in r.text and "Sto meglio" in r.text
    food = conn.execute("SELECT payload FROM v_current WHERE entity_type='nutrition_day' AND local_date=?",
                        (TODAY.isoformat(),)).fetchone()
    assert json.loads(food["payload"]) == {"completeness": "not_logged"}
    flags = _illness_flags(conn)
    assert len(flags) == 1 and flags[0]["tier"] == "T1" and "intenso" in flags[0]["message"]
    with pytest.raises(safety.SafetyBlock):  # no calorie cut or added load while ill
        safety.gate(conn, "energy_deficit_increase")

    # fever later on: same episode, a new version, stronger notice
    r = client.post("/", data={"csrf": _csrf(r.text), "action": "ill_update", "sym_raffreddore": "1",
                               "sym_febbre": "1"})
    assert "Niente allenamento finché non passa" in r.text
    assert conn.execute("SELECT COUNT(*) FROM raw_record WHERE entity_type='health_event'").fetchone()[0] == 2
    assert len({f["local_date"] for f in _illness_flags(conn)}) == 1

    r = client.post("/", data={"csrf": _csrf(r.text), "action": "ill_recover"})
    assert "concluso, ultimo giorno 10/3" in r.text and "Sto meglio" not in r.text
    assert _illness_flags(conn) == []  # the notice closes with the episode
    safety.gate(conn, "energy_deficit_increase")  # nothing blocks any more


def test_a_report_needs_a_symptom_and_csrf(env):
    client, cfg, conn = env
    page = client.get("/").text
    r = client.post("/", data={"csrf": _csrf(page), "action": "ill_report"})
    assert "scegli almeno un sintomo" in r.text
    client.post("/", data={"csrf": "wrong", "action": "ill_report", "sym_gola": "1"})
    assert conn.execute("SELECT COUNT(*) FROM raw_record WHERE entity_type='health_event'").fetchone()[0] == 0


def test_an_episode_nobody_closed_stops_counting(env):
    client, cfg, conn = env
    recs = illness_log.report(cfg, datetime.fromisoformat(NOW), TODAY - timedelta(days=30), ["gola"])
    ingest(conn, recs, "test")
    safety.evaluate(conn, TODAY - timedelta(days=30))
    assert len(_illness_flags(conn)) == 1
    safety.evaluate(conn, TODAY)  # beyond illness_max_open_days: closed by itself
    assert _illness_flags(conn) == [] and illness_log.open_episode(conn, TODAY) is None


def test_cli_ill_and_recovered(env):
    client, cfg, conn = env
    run = CliRunner()
    r = run.invoke(cli_app, ["log", "ill", "raffreddore", "gola", "--food-not-logged"], catch_exceptions=False)
    assert r.exit_code == 0, r.output
    assert "Malessere dal 10/3: raffreddore, mal di gola" in r.output and "non registrato" in r.output
    again = run.invoke(cli_app, ["log", "ill", "gola"])
    assert again.exit_code != 0 and "già un malessere aperto" in again.output
    bad = run.invoke(cli_app, ["log", "ill", "sciatica"])
    assert bad.exit_code != 0
    r = run.invoke(cli_app, ["log", "recovered"], catch_exceptions=False)
    assert r.exit_code == 0 and "concluso" in r.output
    assert illness_log.open_episode(conn, TODAY) is None and _illness_flags(conn) == []
