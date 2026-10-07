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
    monkeypatch.setenv("ASKESIS_NOW", "2025-04-01T08:00:00+02:00")  # the server's "today" matches the synthetic data
    cfg = config.load()
    conn = connect(cfg.db_path)
    from datetime import timedelta

    from askesis.plan import store

    ingest(conn, syn.athlete() + syn.linear_weights(28) + syn.constant_intake(28)
           + [r for w in range(3) for r in syn.strength_session(syn.START + timedelta(weeks=w))], "synthetic")
    lift = {"exercise": "bench_press", "sets": 3, "rep_range": [6, 8], "target_rir": 2}
    store.add_version(conn, "nutrition_target", "t", {"energy_kcal": 1800, "protein_g": 110, "protein_basis": {},
                                                      "method": "prior_only"}, syn.START, None)
    store.add_version(conn, "programme", "p", {"microcycle": [{"day": "mon", "name": "A", "lifts": [lift]}],
                                               "minimal_week": [{"day": "mon", "name": "A", "lifts": [lift]}]},
                      syn.START, None)
    conn.commit()
    from test_cli_f3 import SYNTHETIC_SOURCES
    from test_f3 import prereg

    from askesis.interventions import registry as reg

    reg.propose(conn, "Fase di prova", "phase_start_fat_loss", prereg(value_sources=SYNTHETIC_SOURCES))
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
        page.click("form.entry button[value=save]")
        page.wait_for_load_state("networkidle")
        assert "Salvato" in page.content()
        page.goto(f"{url}/andamenti")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#weight canvas")  # first question open by default, its chart drawn
        assert page.locator(".q-rate .verdict").count() == 1
        assert page.locator("canvas").count() == 1  # other charts are drawn only when opened
        for det in page.locator("details[data-q]").all():
            if det.get_attribute("open") is None:
                det.locator("summary").click()
        for _ in range(50):  # wait until every open chart is drawn (wait_for_function would need 'unsafe-eval')
            if page.evaluate("[...document.querySelectorAll('details[open] .chart')].every(e => e.dataset.drawn)"):
                break
            page.wait_for_timeout(100)
        assert page.locator("canvas").count() >= 2
        for card in page.locator("details[data-q] .chart").all():  # every chart: drawn or a clear empty message
            assert card.locator("canvas").count() == 1 or card.inner_text().strip()
        assert page.locator(".q-empty .empty-state").count() + page.locator("details[data-q]").count() == 4
        browser.close()
    assert errors == []  # includes Content-Security-Policy violations


def test_coach_decision_flow_on_a_phone(server):
    url, password = server
    errors = []
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # browser binary not installed
            pytest.skip(f"chromium non installato: {exc}")
        page = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True).new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{url}/login")
        page.fill("#password", password)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        assert page.locator(".nav .badge-pending").inner_text() == "1"
        page.click(".nav a[href='/coach']")
        page.fill("input[name=text]", "approvo")
        page.click("form.decide button[type=submit]")
        assert page.locator(".confirm").is_visible() and "Nulla è ancora stato registrato" in page.content()
        page.click(".confirm-actions button[type=submit]")
        page.wait_for_load_state("networkidle")
        assert "approvato" in page.locator(".notice.ok").inner_text()
        assert page.locator(".nav .badge-pending").count() == 0
        scroll = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert scroll <= 0  # no horizontal scrolling on a phone
        browser.close()
    assert errors == []


def test_checkin_on_a_phone_by_tapping_the_scales(server):
    url, password = server
    errors = []
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # browser binary not installed
            pytest.skip(f"chromium non installato: {exc}")
        page = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True).new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{url}/login")
        page.fill("#password", password)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        form = page.locator("section.checkin form").first
        assert form.is_visible()  # open when today's check-in is missing
        box = form.locator("label[for='ck_sonno-10']").bounding_box()
        assert box["height"] >= 44 and box["width"] >= 28  # tappable on a phone, ten in a row
        form.locator("label[for='ck_sonno-6']").tap()
        assert form.locator("fieldset.scale").first.locator(".picked").inner_text() == "6 · tra «Discreto» e «Buono»"
        form.locator("label[for='ck_stress-3']").tap()
        form.locator("button[value=save]").tap()
        page.wait_for_load_state("networkidle")
        assert "sonno 6 (" in page.locator(".receipt.saved").inner_text()
        assert page.locator("#ck_sonno-6").is_checked()  # prefilled after saving
        scroll = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert scroll <= 0  # no horizontal scrolling on a phone
        browser.close()
    assert errors == []
