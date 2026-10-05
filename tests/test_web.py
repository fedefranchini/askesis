"""Local dashboard (synthetic data only): auth, CSRF, same logging path as the CLI, safe report access."""

from __future__ import annotations

import re
import secrets
from datetime import date
from pathlib import Path

import pytest
import synthetic as syn
from starlette.testclient import TestClient

from askesis import config, services
from askesis.ingestion.pipeline import ingest
from askesis.store.db import connect
from askesis.web import auth
from askesis.web.app import create_app, paths

TEMPLATES = Path(__file__).parents[1] / "src/askesis/web/templates"


@pytest.fixture
def env(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "web.db"))
    cfg = config.load()
    ingest(connect(cfg.db_path), syn.athlete() + syn.linear_weights(21) + syn.constant_intake(21), "synthetic")
    password = secrets.token_urlsafe(16)  # generated test credential, never a real one
    auth.set_password(paths(cfg)[0], password)
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1")
    return client, cfg, password


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client, password):
    token = csrf_of(client.get("/login").text)
    return client.post("/login", data={"csrf": token, "password": password}, follow_redirects=False)


def count(cfg) -> int:
    return connect(cfg.db_path).execute("SELECT COUNT(*) FROM raw_record").fetchone()[0]


def test_pages_require_login_and_wrong_password_fails(env):
    client, cfg, password = env
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/series").status_code == 401
    bad = login(client, "wrong-password-123")
    assert "Password non corretta" in bad.text
    assert "Troppi tentativi" in login(client, password).text  # throttled right after a failure
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1")  # fresh process: no pending delay
    ok = login(client, password)
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    assert "Riga del giorno" in client.get("/").text


def test_password_file_holds_only_a_hash(env):
    _, cfg, password = env
    text = paths(cfg)[0].read_text()
    assert password not in text and "scrypt" in text
    assert oct(paths(cfg)[0].stat().st_mode)[-3:] == "600"


def test_security_headers_and_hosts(env):
    client, _, password = env
    r = client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    evil = TestClient(client.app, base_url="http://evil.example")
    assert evil.get("/login").status_code == 400
    for t in TEMPLATES.glob("*.html"):  # no external resources in any page
        assert not re.search(r'(src|href)="https?://', t.read_text()), t.name


def test_post_without_csrf_saves_nothing(env):
    client, cfg, password = env
    login(client, password)
    before = count(cfg)
    r = client.post("/", data={"line": "p 66.1", "action": "save"})
    assert "Sessione scaduta" in r.text and count(cfg) == before


def test_preview_saves_nothing_and_save_matches_the_cli_path(env):
    client, cfg, password = env
    login(client, password)
    token = csrf_of(client.get("/").text)
    before = count(cfg)
    pv = client.post("/", data={"csrf": token, "line": "p 66.1 · cibo 1750 95", "day": "2025-03-24",
                                "action": "preview"})
    assert "nulla è stato salvato" in pv.text and count(cfg) == before
    saved = client.post("/", data={"csrf": token, "line": "p 66.1 · cibo 1750 95", "day": "2025-03-24",
                                   "action": "save"})
    assert "Salvato" in saved.text and count(cfg) == before + 2
    cli_records = services.build_day("p 66.1 · cibo 1750 95", cfg, date(2025, 3, 24))
    rows = connect(cfg.db_path).execute(
        "SELECT entity_type, local_date, payload FROM raw_record ORDER BY rowid DESC LIMIT 2").fetchall()
    import json

    web = sorted((r["entity_type"], r["local_date"], json.loads(r["payload"])) for r in rows)
    cli = sorted((r["entity_type"], str(r["local_date"]), r["payload"]) for r in cli_records)
    assert web == cli


def test_workouts_need_explicit_confirmation(env):
    client, cfg, password = env
    login(client, password)
    token = csrf_of(client.get("/").text)
    before = count(cfg)
    data = {"csrf": token, "line": "pesi: panca 50x8 r2, 50x7 r1", "day": "2025-03-24", "action": "save"}
    first = client.post("/", data=data)
    assert "Conferma e salva" in first.text and count(cfg) == before
    second = client.post("/", data=data | {"confirmed": "1"})
    assert "Salvato" in second.text and count(cfg) > before


def test_ready_made_fields_become_the_dictation_line(env):
    client, cfg, password = env
    login(client, password)
    token = csrf_of(client.get("/").text)
    r = client.post("/", data={"csrf": token, "line": "", "day": "2025-03-24", "peso": "66.3", "kcal": "1700",
                               "proteine": "90", "libero_kcal": "300", "action": "save"})
    assert "Salvato" in r.text and "2.000 kcal" in r.text


def test_reports_are_limited_to_listed_files(env, monkeypatch, tmp_path):
    client, cfg, password = env
    login(client, password)
    assert client.get("/review?f=../../pyproject.toml").status_code == 200
    assert "pyproject" not in client.get("/review?f=../../pyproject.toml").text.split("<main")[1]


def test_series_api_returns_engine_metrics(env):
    client, _, password = env
    login(client, password)
    d = client.get("/api/series").json()
    assert len(d["weight"]["daily"]) == 21 and d["weight"]["refs"] == ["weight_daily@1", "weight_ema@1"]
    assert d["tdee"]["ref"] == "adaptive_tdee@1"
