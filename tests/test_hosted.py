"""Hosted mode (Turso): the same app over the libSQL driver, with the private files kept in the database.

The network is replaced by a local libSQL file through the same wrapper (store.remote.connect_local): every query,
trigger and view runs on the libSQL engine, and nothing may be written next to the database on disk.
"""

from __future__ import annotations

import re
import secrets
import sqlite3

import pytest

pytest.importorskip("libsql")  # driver of the hosted database (extra "cloud")
import synthetic as syn
from starlette.testclient import TestClient
from test_f3 import programme
from test_gymsheet import DAY, save
from test_web import csrf_of, login

from askesis import config
from askesis.ingestion import health_sync
from askesis.ingestion.pipeline import ingest
from askesis.plan import store
from askesis.store import remote, state
from askesis.store.db import connect
from askesis.web import auth
from askesis.web.app import create_app, paths


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "disk" / "never.db"))
    monkeypatch.setenv("ASKESIS_REPORTS_DIR", str(tmp_path / "disk" / "reports"))
    monkeypatch.setenv("ASKESIS_DB_URL", "libsql://synthetic.example")
    monkeypatch.setenv("ASKESIS_DB_TOKEN", "synthetic-token")
    engine_file = str(tmp_path / "turso.db")
    seen = []

    def fake_connect(url, token):
        seen.append((url, token))
        return remote.connect_local(engine_file)

    monkeypatch.setattr(remote, "connect", fake_connect)
    cfg = config.load()
    assert cfg.hosted and cfg.db_target == "libsql://synthetic.example"
    conn = connect(cfg.db_target, cfg.db_token)
    ingest(conn, syn.athlete() + syn.linear_weights(21) + syn.constant_intake(21), "synthetic")
    store.add_version(conn, "programme", "base", programme(), DAY, None)
    conn.commit()
    password = secrets.token_urlsafe(16)  # generated test credential
    auth.set_password(paths(cfg)[0], password)
    client = TestClient(create_app(cfg), base_url="https://127.0.0.1")  # hosting: HTTPS-only cookies
    yield client, cfg, password, tmp_path, seen
    state.use_database(None)


def test_rows_read_like_sqlite_rows(tmp_path):
    c = remote.connect_local(str(tmp_path / "r.db"))
    c.execute("CREATE TABLE t(a, b)")
    with c:
        c.execute("INSERT INTO t VALUES (?, ?)", (1, "x"))
    r = c.execute("SELECT a, b FROM t").fetchone()
    assert r[0] == 1 and r["b"] == "x" and dict(r) == {"a": 1, "b": "x"} and list(r) == [1, "x"]
    assert c.execute("SELECT a FROM t WHERE a = 2").fetchone() is None
    with pytest.raises(IndexError):
        r["missing"]


def test_the_app_runs_on_libsql_and_keeps_private_files_in_the_database(hosted):
    client, cfg, password, tmp_path, seen = hosted
    assert seen and all(t == "synthetic-token" for _, t in seen)
    assert login(client, password).status_code == 403  # hosting is the internet: the password alone never enters
    code = auth.create_invite(paths(cfg)[0].parent / "web_invite.json")
    token = csrf_of(client.get("/login").text)
    r = client.post("/login", data={"csrf": token, "password": password, "invite": code}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/accesso"
    again = TestClient(client.app, base_url="https://127.0.0.1")
    r = again.post("/login", data={"csrf": csrf_of(again.get("/login").text), "password": password, "invite": code})
    assert r.status_code == 401  # single use
    page = client.get("/").text
    r = client.post("/", data={"csrf": csrf_of(page), "day": syn.START.isoformat(), "line": "", "peso": "240,5",
                               "action": "save"})
    assert "Salvato" in r.text
    r = save(client)  # gym sheet: preview, confirm, session comment
    assert "Salvato" in r.text
    tid = health_sync.create_token(health_sync.tokens_path(cfg), "iPhone")[0]
    assert health_sync.check_token(health_sync.tokens_path(cfg), f"Bearer {tid}")
    log = auth.recent_attempts(paths(cfg)[0].parent / "web_access.log")
    assert {"method": "password+invito", "ok": True}.items() <= log[-2].items() and log[-1]["ok"] is False
    disk = tmp_path / "disk"
    assert not disk.exists() or not any(disk.rglob("*")), "nothing may be written on disk in hosted mode"
    keys = {r[0] for r in connect(cfg.db_target, cfg.db_token).execute("SELECT key FROM app_state")}
    folder = paths(cfg)[0].parent.name  # "data" in production, a test folder here
    assert {f"{folder}/web_auth.json", f"{folder}/web_session.key", f"{folder}/sync_tokens.json"} <= keys
    assert any(k.startswith("reports/seduta-") for k in keys)
    assert "seduta" in client.get("/coach").text.lower()


def test_raw_records_stay_append_only_on_libsql(hosted):
    _, cfg, _, _, _ = hosted
    conn = connect(cfg.db_target, cfg.db_token)
    rid = conn.execute("SELECT id FROM raw_record LIMIT 1").fetchone()[0]
    with pytest.raises(ValueError, match="append-only"):
        conn.execute("DELETE FROM raw_record WHERE id = ?", (rid,))


def test_backup_copies_everything_into_a_local_sqlite_file(hosted, tmp_path):
    _, cfg, _, _, _ = hosted
    conn = connect(cfg.db_target, cfg.db_token)
    dest = sqlite3.connect(str(tmp_path / "copy.db"))
    conn.backup(dest)
    for table in ("raw_record", "plan_version", "app_state"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert dest.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == n
    names = {r[0] for r in dest.execute("SELECT name FROM sqlite_master WHERE type IN ('trigger', 'view')")}
    assert "raw_record_no_delete" in names and "v_current" in names
    with pytest.raises(sqlite3.DatabaseError):
        dest.execute("DELETE FROM raw_record")
    assert re.match(r"^[0-9a-f-]+$", conn.execute("SELECT id FROM raw_record LIMIT 1").fetchone()[0])
