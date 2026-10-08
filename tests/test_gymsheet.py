"""Gym sheet page (synthetic data only): preview/confirm flow and no duplicates against the Apple Notes import."""

from __future__ import annotations

import re
import secrets
from datetime import date, timedelta

import pytest
import synthetic as syn
from starlette.testclient import TestClient
from test_f3 import programme
from test_gymnote import write_results
from test_web import count, csrf_of, login
from typer.testing import CliRunner

from askesis import config
from askesis.cli.main import app as cli_app
from askesis.ingestion import gymnote, gymsheet
from askesis.ingestion.pipeline import ingest
from askesis.plan import store
from askesis.store.db import connect
from askesis.web import auth
from askesis.web.app import create_app, paths

DAY = syn.START  # Monday, in the past: allowed
D = DAY.isoformat()
OK = [("300", "20", "2"), ("300", "20", "2"), ("285", "20", "1")]  # clearly synthetic values


@pytest.fixture
def env(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "gs.db"))
    monkeypatch.setattr("askesis.cli.f3._settings", lambda: {"bench_press": "sedile sintetico 9"})
    cfg = config.load()
    conn = connect(cfg.db_path)
    ingest(conn, syn.athlete() + syn.linear_weights(21), "synthetic")
    store.add_version(conn, "programme", "base", programme(), DAY, None)
    conn.commit()
    conn.close()
    password = secrets.token_urlsafe(16)
    auth.set_password(paths(cfg)[0], password)
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1")
    login(client, password)
    return client, cfg


def form(client, rows=OK, day=D, action="preview", run="", n=3, **extra):
    data = {"csrf": csrf_of(client.get(f"/palestra?data={day}").text), "day": day, "action": action,
            "ex_0": "bench_press", "n_0": str(n), "run": run, "notes": ""}
    for j, (kg, reps, rir) in enumerate(rows):
        data |= {f"kg_0_{j}": kg, f"reps_0_{j}": reps, f"rir_0_{j}": rir}
    return data | extra


def save(client, rows=OK, day=D, **kw):
    """Preview then confirm, as the browser does."""
    pv = client.post("/palestra", data=form(client, rows, day, **kw))
    nonce = re.search(r'name="nonce" value="([^"]+)"', pv.text)
    assert nonce, pv.text
    return client.post("/palestra", data=form(client, rows, day, action="save", confirmed="1", nonce=nonce[1], **kw))


def db(cfg):
    return connect(cfg.db_path)


def n_sets(cfg):
    return db(cfg).execute("SELECT COUNT(*) FROM raw_record WHERE entity_type='set_record'").fetchone()[0]


def test_get_shows_the_plan_and_writes_nothing(env):
    client, cfg = env
    before = count(cfg)
    r = client.get(f"/palestra?data={D}")
    assert r.status_code == 200 and "Panca piana" in r.text
    assert "3 × 6–8 · RIR 2 · carico da calibrare" in r.text and "recupero 2–3 min" in r.text
    assert "macchina: sedile sintetico 9" in r.text
    assert 'inputmode="decimal"' in r.text and 'inputmode="numeric"' in r.text
    assert 'value=""' in r.text and "Risultati già registrati" not in r.text
    assert count(cfg) == before
    assert 'href="/palestra"' in r.text


def test_preview_writes_nothing_and_save_inserts(env):
    client, cfg = env
    before = count(cfg)
    pv = client.post("/palestra", data=form(client))
    assert "Anteprima: nulla è stato salvato" in pv.text and "Conferma e salva" in pv.text
    assert "300 kg × 20 @RIR 2" in pv.text and count(cfg) == before
    r = save(client)
    assert "Salvato" in r.text and count(cfg) == before + 4  # session + 3 sets
    assert "Risultati già registrati per questa data" in r.text  # fields now hold the current values
    assert n_sets(cfg) == 3


def test_resave_the_same_form_creates_nothing(env):
    client, cfg = env
    save(client)
    after = count(cfg)
    again = client.post("/palestra", data=form(client))
    assert "Nulla di nuovo da salvare" in again.text and "Già registrati e invariati: 4" in again.text
    assert "Conferma e salva" not in again.text
    forced = client.post("/palestra", data=form(client, action="save", confirmed="1", nonce="x"))
    assert "Conferma scaduta" in forced.text
    assert count(cfg) == after


def test_changed_value_is_a_correction_not_a_duplicate(env):
    client, cfg = env
    save(client)
    rows = [OK[0], OK[1], ("285", "20", "1")]
    rows[2] = ("285", "19", "1")
    r = save(client, rows)
    assert "1 correzione" in r.text
    c = db(cfg)
    assert sorted(x["reps"] for x in c.execute("SELECT reps FROM v_set_record")) == [19, 20, 20]
    assert n_sets(cfg) == 4  # history kept, only one new row


def test_confirmation_is_bound_to_the_previewed_values(env):
    client, cfg = env
    pv = client.post("/palestra", data=form(client))
    nonce = re.search(r'name="nonce" value="([^"]+)"', pv.text)[1]
    before = count(cfg)
    other = [("300", "20", "2"), ("300", "20", "2"), ("300", "20", "2")]
    r = client.post("/palestra", data=form(client, other, action="save", confirmed="1", nonce=nonce))
    assert "Conferma scaduta o valori cambiati" in r.text and count(cfg) == before
    # a nonce is single use
    r = client.post("/palestra", data=form(client, action="save", confirmed="1", nonce=nonce))
    assert "Conferma scaduta" in r.text and count(cfg) == before


def test_unreadable_row_is_an_error_and_nothing_is_saved_for_it(env):
    client, cfg = env
    before = count(cfg)
    rows = [("300", "20", "2"), ("trecento", "20", ""), ("300", "", "")]
    pv = client.post("/palestra", data=form(client, rows))
    assert "Non letto, nulla salvato per «Panca piana»" in pv.text and "carico non leggibile" in pv.text
    assert "Conferma e salva" not in pv.text and count(cfg) == before
    client.post("/palestra", data=form(client, rows, action="save", confirmed="1", nonce="x"))
    assert count(cfg) == before and n_sets(cfg) == 0
    miss = client.post("/palestra", data=form(client, [("300", "", ""), ("", "", ""), ("", "", "")]))
    assert "ripetizioni mancanti" in miss.text


def test_untouched_rows_are_not_saved_and_extra_rows_are_capped(env):
    client, cfg = env
    r = save(client, [("300", "20", ""), ("", "", ""), ("", "", "")])
    assert "Salvato" in r.text and n_sets(cfg) == 1
    page = client.post("/palestra", data=form(client, action="more_0"))
    assert page.text.count('name="reps_0_') == 4
    many = client.post("/palestra", data=form(client, action="more_0", n=5))
    assert many.text.count('name="reps_0_') == 5  # 3 planned + 2 extra, never more
    capped = client.post("/palestra", data=form(client, action="more_0", n=99))
    assert capped.text.count('name="reps_0_') == 5 and "+ serie" not in capped.text


def test_comma_decimal_and_run_line(env):
    client, cfg = env
    r = save(client, [("300,5", "20", "1,5")], run="5km 30:00 fc140")
    assert "Salvato" in r.text
    c = db(cfg)
    assert c.execute("SELECT load_kg, rir FROM v_set_record").fetchone()[:] == (300.5, 1.5)
    assert c.execute("SELECT COUNT(*) FROM raw_record WHERE entity_type='running_session'").fetchone()[0] == 1
    bad = client.post("/palestra", data=form(client, [], run="boh"))
    assert "Non letto, nulla salvato per «Corsa»" in bad.text


def test_run_text_round_trip():
    from askesis.parsers.text import parse_run

    for text in ("5km 30:00 fc140", "tm easy 10.2km 1:05:30 fc150 fcmax170 rpe6.5", "800m 4:10"):
        p = parse_run(text)
        assert parse_run(gymsheet.run_text(p)) == p


def test_missing_csrf_and_future_date_are_rejected(env):
    client, cfg = env
    before = count(cfg)
    data = form(client)
    data.pop("csrf")
    r = client.post("/palestra", data=data)
    assert "Sessione scaduta" in r.text and count(cfg) == before
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert client.get(f"/palestra?data={tomorrow}").status_code == 400
    r = client.post("/palestra", data=form(client, day=tomorrow))
    assert r.status_code == 400 and "futuro" in r.text and count(cfg) == before
    assert client.get("/palestra?data=boh").status_code == 400
    anon = TestClient(client.app, base_url="http://127.0.0.1")
    assert anon.get("/palestra", follow_redirects=False).headers["location"] == "/login"


def test_no_session_planned_says_so(env):
    client, cfg = env
    tuesday = (DAY + timedelta(days=1)).isoformat()
    r = client.get(f"/palestra?data={tuesday}")
    assert "Giorno senza sessione" in r.text and 'name="reps_' not in r.text


# ------------------------------------------------------------------ against the Apple Notes import
@pytest.fixture
def notes(monkeypatch):
    fake = {"body": None}
    import askesis.notes_bridge as nb

    monkeypatch.setattr(nb, "upsert", lambda title, body: fake.update(body=body) or "note-id")
    monkeypatch.setattr(nb, "read", lambda title: fake["body"])
    runner = CliRunner()
    return (lambda *a: runner.invoke(cli_app, list(a), catch_exceptions=False)), fake


NOTE = "300x20 r2, 300x20 r2, 285x20 r1"


def test_page_then_note_reconcile_without_duplicates(env, notes):
    client, cfg = env
    run, fake = notes
    save(client)
    total = count(cfg)
    run("gym-note", "create", "--for", D)
    write_results(fake, {"bench_press": NOTE})
    out = run("gym-note", "import", "--for", D, "--yes").output
    assert "nulla di nuovo" in out and "invariati: 4" in out and count(cfg) == total
    write_results(fake, {"bench_press": "300x20 r2, 300x20 r2, 285x19 r1"})  # one value differs
    out = run("gym-note", "import", "--for", D, "--yes").output
    assert "inseriti 1" in out
    assert count(cfg) == total + 1 and n_sets(cfg) == 4
    assert sorted(x["reps"] for x in db(cfg).execute("SELECT reps FROM v_set_record")) == [19, 20, 20]


def test_note_then_page_reconcile_without_duplicates(env, notes):
    client, cfg = env
    run, fake = notes
    run("gym-note", "create", "--for", D)
    write_results(fake, {"bench_press": NOTE})
    assert "inseriti 4" in run("gym-note", "import", "--for", D, "--yes").output
    total = count(cfg)
    page = client.get(f"/palestra?data={D}")
    assert "Risultati già registrati per questa data" in page.text and 'value="285"' in page.text
    same = client.post("/palestra", data=form(client))
    assert "Nulla di nuovo da salvare" in same.text and count(cfg) == total
    changed = save(client, [OK[0], OK[1], ("285", "18", "1")])
    assert "1 correzione" in changed.text and count(cfg) == total + 1 and n_sets(cfg) == 4
    # and the note read again afterwards sees that value as already imported
    write_results(fake, {"bench_press": "300x20 r2, 300x20 r2, 285x18 r1"})
    assert "nulla di nuovo" in run("gym-note", "import", "--for", D, "--yes").output


def test_missing_rows_are_reported_not_retracted(env):
    client, cfg = env
    save(client)
    total = count(cfg)
    pv = client.post("/palestra", data=form(client, [OK[0], ("", "", ""), ("", "", "")]))
    assert "Panca piana, serie 2" in pv.text and "Non vengono ritirati" in pv.text
    assert count(cfg) == total


# ------------------------------------------------------------------ comment, flags, privacy of the log
def test_comment_is_written_validated_and_never_echoed(env, capsys):
    client, cfg = env
    save(client, [("300", "20", "2")] * 2)
    second = (DAY + timedelta(weeks=1)).isoformat()
    capsys.readouterr()
    r = save(client, [("305", "20", "2")] * 2, day=second)
    assert "Salvato" in r.text and "Dopo la seduta" in r.text and 'href="/"' in r.text
    out = capsys.readouterr()
    assert "305" not in out.out and "305" not in out.err
    f = config.reports_dir() / f"seduta-{second}.md"
    assert f.exists() and "305" in f.read_text()
    assert f"/coach?f=seduta-{second}.md" in r.text
    coach = client.get(f"/coach?f=seduta-{second}.md")
    assert coach.status_code == 200 and "305" in coach.text


def test_unvalidated_comment_goes_to_da_verificare(env, monkeypatch):
    client, cfg = env
    from askesis.analytics import session_comment

    monkeypatch.setattr(session_comment, "render", lambda conn, values, day: "Un numero senza fonte: 123456 kg.")
    r = save(client)
    assert "Commento da verificare" in r.text
    rep = config.reports_dir()
    assert (rep / f"seduta-{D}.DA-VERIFICARE.md").exists() and not (rep / f"seduta-{D}.md").exists()
    assert "DA-VERIFICARE" in r.text


def test_safety_flags_are_shown_after_saving(env, monkeypatch):
    client, cfg = env
    from askesis.safety import rules as safety

    real = safety.open_flags
    monkeypatch.setattr(safety, "open_flags", lambda c: real(c) or [
        {"tier": "T1", "message": "segnale sintetico", "actions": "[\"azione sintetica\"]", "id": "x",
         "local_date": D}])
    r = save(client)
    assert "segnale sintetico" in r.text and 'href="/safety"' in r.text


def test_reading_a_planned_day_records_no_rule_execution(env):
    client, cfg = env
    client.get(f"/palestra?data={D}")
    assert db(cfg).execute("SELECT COUNT(*) FROM rule_execution").fetchone()[0] == 0


def test_no_inline_script_or_style_in_the_template():
    t = (config.ROOT / "src/askesis/web/templates/palestra.html").read_text()
    assert "<script" not in t and " style=" not in t and not re.search(r'(src|href)="https?://', t)
    assert gymnote.SOURCE_ID == "gym_note"
