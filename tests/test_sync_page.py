"""Health sync page (synthetic data only): two-step token creation/revocation, secret shown once, partial days."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import synthetic as syn
from starlette.testclient import TestClient
from test_web import csrf_of, login

from askesis import config
from askesis.ingestion import health_sync
from askesis.ingestion.pipeline import ingest
from askesis.store.db import connect
from askesis.web import auth
from askesis.web.app import create_app, paths

TEMPLATE = Path(__file__).parents[1] / "src/askesis/web/templates/sincronizzazione.html"
DAY = date(2025, 3, 5)
PROBE = {"v": 1, "window_start": "2025-03-01T00:00:00+01:00", "probe": True,
         "samples": {"steps": {"value": [1000], "unit": ["count"], "start": ["2025-03-02T10:00:00+01:00"],
                               "end": ["2025-03-02T10:05:00+01:00"], "source": ["Sintetico"]}}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "sp.db"))
    monkeypatch.setattr("askesis.web.app.now_utc", lambda: datetime(2025, 3, 10, 12, 0, tzinfo=UTC))
    cfg = config.load()
    conn = connect(cfg.db_path)
    ingest(conn, syn.athlete() + syn.linear_weights(21), "synthetic")
    rec = syn._env("nutrition_day", DAY, {"energy_kcal": 9000, "protein_g": 500, "completeness": "partial"},
                   occurred_at=datetime(2025, 3, 5, 12, 0, tzinfo=UTC))
    rec |= {"source_id": "apple_health", "source_record_id": "synthetic-day", "entry_method": "imported"}
    ingest(conn, [rec], "apple_health_sync", source_kind="imported")
    conn.close()
    password = secrets.token_urlsafe(16)
    auth.set_password(paths(cfg)[0], password)
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1")
    login(client, password)
    return client, cfg


def post(client, **data):
    return client.post("/sincronizzazione", data={"csrf": csrf_of(client.get("/sincronizzazione").text)} | data)


def two_step(client, kind, arg):
    first = post(client, action=f"ask_{kind}", arg=arg)
    nonce = re.search(r'name="nonce" value="([^"]+)"', first.text)
    assert nonce, first.text
    return first, post(client, action=f"do_{kind}", arg=arg, nonce=nonce[1])


def make_token(client, label="iPhone"):
    _, done = two_step(client, "create", label)
    return done, re.search(r'class="secret-field"[^>]*>([^<]+)</textarea>', done.text)[1]


def current_day(cfg):
    return connect(cfg.db_path).execute(
        "SELECT source_id, payload FROM v_current WHERE entity_type='nutrition_day' AND local_date=?",
        (DAY.isoformat(),)).fetchone()


def test_requires_login(env):
    _, cfg = env
    anon = TestClient(create_app(cfg), base_url="http://127.0.0.1")
    assert anon.get("/sincronizzazione", follow_redirects=False).headers["location"] == "/login"
    post = anon.post("/sincronizzazione", data={"action": "do_create"}, follow_redirects=False)
    assert post.status_code in (303, 401)


def test_token_creation_secret_once_and_works(env):
    client, cfg = env
    first, done = two_step(client, "create", "iPhone di prova")
    assert "Creare un nuovo token" in first.text and "readonly" not in first.text
    assert health_sync.tokens(health_sync.tokens_path(cfg)) != []
    secret = re.search(r'class="secret-field"[^>]*>([^<]+)</textarea>', done.text)[1]
    assert "non verrà più mostrato" in done.text
    assert secret not in client.get("/sincronizzazione").text
    assert "iPhone di prova" in client.get("/sincronizzazione").text
    r = client.post("/api/health-sync", json=PROBE, headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 200 and r.json()["ok"]
    page = client.get("/sincronizzazione").text
    assert "Prova: nulla salvato" in page and "steps" in page and "count" in page and "Sintetico" not in page


def test_first_step_creates_nothing_and_stale_nonce_is_refused(env):
    client, cfg = env
    path = health_sync.tokens_path(cfg)
    post(client, action="ask_create", arg="iPhone")
    assert health_sync.tokens(path) == []
    bad = post(client, action="do_create", arg="iPhone", nonce="sbagliato")
    assert "nulla è stato modificato" in bad.text and health_sync.tokens(path) == []
    assert "nulla è stato modificato" in post(client, action="do_create", arg="iPhone", nonce="x").text  # nonce used up


def test_revoke_two_step_then_token_rejected(env):
    client, cfg = env
    _, secret = make_token(client)
    tid = health_sync.tokens(health_sync.tokens_path(cfg))[0]["id"]
    first = post(client, action="ask_revoke", arg=tid)
    assert "Confermi la revoca di «iPhone»?" in first.text
    assert client.post("/api/health-sync", json=PROBE, headers={"Authorization": f"Bearer {secret}"}).status_code == 200
    nonce = re.search(r'name="nonce" value="([^"]+)"', first.text)[1]
    assert "Token revocato" in post(client, action="do_revoke", arg=tid, nonce=nonce).text
    assert client.post("/api/health-sync", json=PROBE, headers={"Authorization": f"Bearer {secret}"}).status_code == 401


def test_missing_csrf_is_rejected(env):
    client, cfg = env
    r = client.post("/sincronizzazione", data={"action": "ask_create", "arg": "x"})
    assert "Richiesta non valida" in r.text
    r = client.post("/sincronizzazione", data={"csrf": "sbagliato", "action": "do_create", "arg": "x", "nonce": "y"})
    assert "Richiesta non valida" in r.text and health_sync.tokens(health_sync.tokens_path(cfg)) == []


def test_confirm_partial_day(env):
    client, cfg = env
    page = client.get("/sincronizzazione").text
    assert "05/03/2025" in page and "9000" in page.replace(" ", "") and "500" in page
    assert current_day(cfg)["source_id"] == "apple_health"
    _, done = two_step(client, "confirm", DAY.isoformat())
    assert "Giornata confermata: ora entra in TDEE e aderenza." in done.text
    row = current_day(cfg)
    assert row["source_id"] == "manual" and '"complete"' in row["payload"]
    assert health_sync.partial_days(connect(cfg.db_path)) == []
    again = post(client, action="ask_confirm", arg=DAY.isoformat())
    assert "Nulla da confermare" in again.text


def test_confirm_stale_or_mismatched_nonce_saves_nothing(env):
    client, cfg = env
    first = post(client, action="ask_confirm", arg=DAY.isoformat())
    nonce = re.search(r'name="nonce" value="([^"]+)"', first.text)[1]
    other = post(client, action="do_confirm", arg="2025-03-06", nonce=nonce)  # nonce bound to another day
    assert "nulla è stato modificato" in other.text and current_day(cfg)["source_id"] == "apple_health"
    assert "nulla è stato modificato" in post(client, action="do_confirm", arg=DAY.isoformat(), nonce=nonce).text
    assert current_day(cfg)["source_id"] == "apple_health"


def test_template_has_no_inline_script_or_style():
    html = TEMPLATE.read_text()
    assert not re.search(r"<script|<style|\sstyle=|\son[a-z]+=|javascript:", html)
