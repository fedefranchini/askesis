"""Dashboard in a real browser (Playwright, headless Chromium). Skipped when the browser is not installed (e.g. CI).
Synthetic data and a generated test password only."""

from __future__ import annotations

import secrets
import socket
import threading
import time

import pytest
import synthetic as syn

playwright = pytest.importorskip("playwright.sync_api")

from askesis import config  # noqa: E402
from askesis.ingestion.pipeline import ingest  # noqa: E402
from askesis.store.db import connect  # noqa: E402
from askesis.web import auth  # noqa: E402
from askesis.web.app import create_app, paths  # noqa: E402


@pytest.fixture
def server(tmp_path, monkeypatch):
    import uvicorn

    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "b.db"))
    cfg = config.load()
    ingest(connect(cfg.db_path), syn.athlete() + syn.linear_weights(28) + syn.constant_intake(28)
           + syn.strength_session(syn.START), "synthetic")
    password = secrets.token_urlsafe(16)
    auth.set_password(paths(cfg)[0], password)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(cfg), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", password
    srv.should_exit = True
    t.join(timeout=5)


def test_login_log_and_charts_without_console_errors(server):
    url, password = server
    errors = []
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # browser binary not installed
            pytest.skip(f"chromium non installato: {exc}")
        page = browser.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{url}/login")
        page.fill("#password", password)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        page.fill("#line", "p 66.2")
        page.fill("#day", "2025-03-31")
        page.click("button[value=save]")
        page.wait_for_load_state("networkidle")
        assert "Salvato" in page.content()
        page.goto(f"{url}/andamenti")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#weight canvas")
        assert page.locator("canvas").count() >= 3
        browser.close()
    assert errors == []  # includes Content-Security-Policy violations
