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


def test_remote_access_is_configuration_only(tmp_path, monkeypatch):
    """A network layer in front of 127.0.0.1 (any provider) only needs its host name and HTTPS cookies in config."""
    private = tmp_path / "p.toml"
    private.write_text('timezone = "Europe/Berlin"\nweb_allowed_hosts = ["127.0.0.1", "nodo-a.example.ts"]\n'
                       "web_secure_cookies = true\n")
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "r.db"))
    cfg = config.load()
    auth.set_password(paths(cfg)[0], secrets.token_urlsafe(16))
    app = create_app(cfg)
    assert TestClient(app, base_url="https://nodo-a.example.ts").get("/login").status_code == 200
    assert TestClient(app, base_url="http://other.example").get("/login").status_code == 400
    r = TestClient(app, base_url="https://nodo-a.example.ts").get("/login")
    assert "secure" in r.headers.get("set-cookie", "").lower()


def test_only_loopback_and_allowed_clients_may_connect(tmp_path, monkeypatch):
    from askesis.web.app import check_bind

    private = tmp_path / "p.toml"
    private.write_text('timezone = "Europe/Berlin"\nweb_allowed_clients = ["100.64.0.2"]\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "c.db"))
    cfg = config.load()
    auth.set_password(paths(cfg)[0], secrets.token_urlsafe(16))
    app = create_app(cfg)
    ok = TestClient(app, base_url="http://127.0.0.1", client=("100.64.0.2", 5000)).get("/login")
    other = TestClient(app, base_url="http://127.0.0.1", client=("192.168.1.50", 5000)).get("/login")
    assert ok.status_code == 200 and other.status_code == 403
    with pytest.raises(ValueError):
        check_bind(["127.0.0.1", "0.0.0.0"])


def test_missing_interfaces_do_not_stop_loopback():
    from askesis.web.app import bind_sockets

    socks, missing = bind_sockets(["127.0.0.1", "192.0.2.1"], 0)  # 192.0.2.1: documentation range, never local
    try:
        assert [s.getsockname()[0] for s in socks] == ["127.0.0.1"] and missing == ["192.0.2.1"]
    finally:
        for s in socks:
            s.close()


def test_static_files_are_cacheable_and_pages_are_not(env):
    client, _, password = env
    login(client, password)
    page = client.get("/")
    v = re.search(r'/static/app\.css\?v=([0-9a-f]{10})', page.text).group(1)
    assert page.headers["cache-control"] == "no-store"
    css = client.get(f"/static/app.css?v={v}")
    assert css.status_code == 200 and "max-age" in css.headers["cache-control"]
    assert "default-src 'self'" in css.headers["content-security-policy"]
    assert 'class="ring"' in page.text and "Peso di tendenza" in page.text


def test_login_form_is_keychain_friendly(env):
    client, _, _ = env
    html = client.get("/login").text
    assert 'autocomplete="username"' in html and 'autocomplete="current-password"' in html
    assert 'name="remember"' in html and "30 giorni" in html


def test_wrong_password_states_the_wait(env):
    client, _, _ = env
    r = login(client, "wrong-password-123")
    assert r.status_code == 401 and "Puoi riprovare tra" in r.text and 'data-wait="' in r.text
    again = login(client, "wrong-password-123")
    assert again.status_code == 429 and "Troppi tentativi" in again.text and "Puoi riprovare tra" in again.text


def remember_login(client, password):
    token = csrf_of(client.get("/login").text)
    return client.post("/login", data={"csrf": token, "password": password, "remember": "1"}, follow_redirects=False)


def test_remembered_device_skips_the_password_until_revoked(env):
    client, cfg, password = env
    r = remember_login(client, password)
    cookie = r.cookies.get("askesis_device")
    header = r.headers["set-cookie"].lower()
    assert cookie and "httponly" in header and "samesite=strict" in header
    stored = paths(cfg)[2].read_text()
    assert cookie.split(".", 1)[1] not in stored  # only the hash is kept
    assert oct(paths(cfg)[2].stat().st_mode)[-3:] == "600"
    fresh = TestClient(client.app, base_url="http://127.0.0.1", cookies={"askesis_device": cookie})
    assert fresh.get("/", follow_redirects=False).status_code == 200  # no password, new session
    forged = TestClient(client.app, base_url="http://127.0.0.1", cookies={"askesis_device": cookie[:-2] + "xx"})
    assert forged.get("/", follow_redirects=False).status_code == 303
    did = cookie.split(".", 1)[0]
    page = client.get("/accesso")
    assert "questo dispositivo" in page.text
    client.post("/accesso", data={"csrf": csrf_of(page.text), "revoke": did}, follow_redirects=False)
    assert fresh.get("/", follow_redirects=False).headers["location"] == "/login"  # its session ends too
    again = TestClient(client.app, base_url="http://127.0.0.1", cookies={"askesis_device": cookie})
    assert again.get("/", follow_redirects=False).status_code == 303


def test_remembered_device_expires_on_the_declared_date(env, monkeypatch):
    client, cfg, password = env
    cookie = remember_login(client, password).cookies.get("askesis_device")
    rec = auth.device_for(paths(cfg)[2], cookie)
    assert rec["expires"] - rec["created"] == pytest.approx(cfg.web_remember_days * 86400)
    assert auth.device_for(paths(cfg)[2], cookie, now=rec["expires"] + 1) is None
    auth.device_for(paths(cfg)[2], cookie, touch=True)
    assert auth.device_for(paths(cfg)[2], cookie)["expires"] == rec["expires"]  # use never extends it


def test_password_change_ends_sessions_and_devices(env):
    client, cfg, password = env
    cookie = remember_login(client, password).cookies.get("askesis_device")
    assert client.get("/", follow_redirects=False).status_code == 200
    auth.set_password(paths(cfg)[0], secrets.token_urlsafe(16))
    auth.revoke(paths(cfg)[2])  # what `web set-password` does
    assert client.get("/", follow_redirects=False).status_code == 303
    other = TestClient(client.app, base_url="http://127.0.0.1", cookies={"askesis_device": cookie})
    assert other.get("/", follow_redirects=False).status_code == 303


def test_remembered_device_does_not_bypass_the_other_checks(env, tmp_path, monkeypatch):
    client, cfg, password = env
    cookie = remember_login(client, password).cookies.get("askesis_device")
    stranger = TestClient(client.app, base_url="http://127.0.0.1", client=("192.168.1.50", 5000),
                          cookies={"askesis_device": cookie})
    assert stranger.get("/").status_code == 403
    evil = TestClient(client.app, base_url="http://evil.example", cookies={"askesis_device": cookie})
    assert evil.get("/").status_code == 400
    trusted = TestClient(client.app, base_url="http://127.0.0.1", cookies={"askesis_device": cookie})
    trusted.get("/")
    before = count(cfg)
    trusted.post("/", data={"line": "p 66.1", "action": "save"})  # still needs the CSRF token
    assert count(cfg) == before


def test_logout_forgets_this_device(env):
    client, cfg, password = env
    remember_login(client, password)
    assert len(auth.devices(paths(cfg)[2])) == 1
    client.post("/logout", data={"csrf": csrf_of(client.get("/").text)}, follow_redirects=False)
    assert auth.devices(paths(cfg)[2]) == []


def _coach_env(env):
    import os

    from test_cli_f3 import SYNTHETIC_SOURCES
    from test_f3 import prereg

    from askesis.interventions import registry as reg

    client, cfg, password = env
    reg.propose(connect(cfg.db_path), "Fase di prova", "phase_start_fat_loss", prereg(value_sources=SYNTHETIC_SOURCES))
    rep = Path(os.environ["ASKESIS_REPORTS_DIR"])
    rep.mkdir(exist_ok=True)
    (rep / "review-2025-W11.md").write_text("# Review\n\nSettimana regolare.\n")
    (rep / "review-2025-W11-commento.md").write_text("# Commento\n\nIl peso è sceso di 4,7 kg.\n")
    login(client, password)
    return client, cfg


def test_coach_shows_validated_and_flags_unverified_texts(env):
    client, _ = _coach_env(env)
    page = client.get("/coach")
    assert "Da decidere" in page.text and "Fase di prova" in page.text
    assert 'aria-label="1 proposte in attesa"' in page.text  # badge on the tab bar
    flagged = client.get("/coach?f=review-2025-W11-commento.md").text
    assert "Da verificare" in flagged and "4,7" in flagged
    assert "Validato" in client.get("/coach?f=review-2025-W11.md").text
    assert "pyproject" not in client.get("/coach?f=../../pyproject.toml").text.split("<main")[1]


def test_decision_needs_explicit_word_and_confirmation(env):
    from askesis.interventions import registry as reg

    client, cfg = _coach_env(env)
    token = csrf_of(client.get("/coach").text)
    vague = client.post("/coach/decisione", data={"csrf": token, "kind": "intervention", "ref": "1", "text": "ok"})
    assert "approvo" in vague.text and "Conferma la decisione" not in vague.text
    step1 = client.post("/coach/decisione", data={"csrf": token, "kind": "intervention", "ref": "1",
                                                  "text": "approvo", "reason": "pronto"})
    assert "Conferma: approvo" in step1.text and "Nulla è ancora stato registrato" in step1.text
    c = connect(cfg.db_path)
    assert reg.status(c, reg.get(c, 1)["id"]) == "proposed"  # nothing recorded at step 1
    nonce = re.search(r'name="nonce" value="([^"]+)"', step1.text).group(1)
    forged = client.post("/coach/conferma", data={"csrf": token, "nonce": "x" + nonce})
    assert "nulla è stato registrato" in forged.text.lower() and reg.status(c, reg.get(c, 1)["id"]) == "proposed"
    step1 = client.post("/coach/decisione", data={"csrf": token, "kind": "intervention", "ref": "1", "text": "approvo"})
    nonce = re.search(r'name="nonce" value="([^"]+)"', step1.text).group(1)
    done = client.post("/coach/conferma", data={"csrf": token, "nonce": nonce})
    assert "approvato" in done.text and reg.status(c, reg.get(c, 1)["id"]) == "activated"
    replay = client.post("/coach/conferma", data={"csrf": token, "nonce": nonce})  # one confirmation, one decision
    assert "nulla è stato registrato" in replay.text.lower()


def test_decision_without_csrf_records_nothing(env):
    from askesis.interventions import registry as reg

    client, cfg = _coach_env(env)
    r = client.post("/coach/decisione", data={"kind": "intervention", "ref": "1", "text": "approvo"})
    assert "Sessione scaduta" in r.text
    c = connect(cfg.db_path)
    assert reg.status(c, reg.get(c, 1)["id"]) == "proposed"
