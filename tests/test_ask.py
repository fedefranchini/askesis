"""«Chiedi ai dati» on a synthetic athlete: routing on the evaluation set (traps 100%, regular ≥ 95%), every answer
validated or an explicit «non lo so», numbers equal to an independent recomputation, read only, POST only."""

from __future__ import annotations

import json
import re
import secrets
from datetime import date, timedelta
from pathlib import Path

import pytest
import synthetic as syn
import yaml
from starlette.testclient import TestClient
from test_f3 import phase, programme

from askesis import ask, config
from askesis.analytics import body, engine
from askesis.core.ids import new_id
from askesis.ingestion.pipeline import ingest
from askesis.plan import store
from askesis.store.db import connect
from askesis.web import auth
from askesis.web.app import create_app, paths

P = date(2025, 3, 17)  # phase start (a Monday)
TODAY = date(2025, 4, 2)  # a Wednesday, third week of the phase
EVAL = yaml.safe_load((Path(__file__).parent / "ask_eval.yaml").read_text())


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("ASKESIS_REPORTS_DIR", str(tmp_path / "reports"))
    c = connect(tmp_path / "ask.db")
    recs = syn.athlete() + syn.linear_weights(31) + syn.constant_intake(30, kcal=1900, prot=120)
    for d in (P, P + timedelta(days=7), P + timedelta(days=14)):
        recs += syn.strength_session(d)
    ingest(c, recs, "synthetic")
    store.add_version(c, "phase", "fase", phase(), P, None)
    store.add_version(c, "programme", "base", programme(), P, None)
    store.add_version(c, "nutrition_target", "target", {"energy_kcal": 1800, "protein_g": 130, "method": "prior_only",
                                                        "protein_basis": {"method": "per_kg_body_weight"}}, P, None)
    c.commit()
    yield c
    c.close()


def _items(kind: str):
    return [pytest.param(it, id=f"{kind}:{it['q'][:40]}") for it in EVAL[kind]]


def _check(conn, it) -> ask.Answer:
    ans = ask.ask(conn, it["q"], TODAY)
    for w in it.get("has", []):
        assert w.lower() in ans.text.lower(), ans.text
    for w in it.get("lacks", []):
        assert w.lower() not in ans.text.lower(), ans.text
    assert ans.ok or "non lo so" in ans.text.lower(), ans.issues  # validated, or an explicit «non lo so»
    return ans


@pytest.mark.parametrize("it", _items("traps"))
def test_traps_are_always_handled(conn, it):
    """100% required: changes refused, health questions answered only by the fixed safety texts."""
    ans = _check(conn, it)
    assert ans.intent == it["intent"], (it["q"], ans.intent, ans.text)


def test_regular_questions_are_routed_well(conn):
    wrong = []
    for it in EVAL["regular"]:
        ans = _check(conn, it)
        if ans.intent != it["intent"]:
            wrong.append((it["q"], it["intent"], ans.intent))
    assert len(EVAL["regular"]) + len(EVAL["traps"]) >= 100
    assert len(wrong) <= 0.05 * len(EVAL["regular"]), wrong


def test_numbers_equal_an_independent_recomputation(conn):
    inp = engine.load_inputs(conn)
    ema = body.weight_ema_series({d: v for d, v in body.daily_weights(inp.weighins).items() if d <= TODAY})
    start, now = ema[max(d for d in ema if d < P)], ema[max(ema)]
    w = ask.ask(conn, "quanto peso ho perso dall'inizio?", TODAY).text
    assert f"{start:.1f}".replace(".", ",") in w and f"{now:.1f}".replace(".", ",") in w
    assert f"{now - start:+.1f}".replace(".", ",").replace("-", "−") in w
    n = ask.ask(conn, "come sto andando questa settimana con calorie e proteine?", TODAY).text
    assert "1900 kcal" in n and "1800 kcal" in n and "+100 kcal" in n  # synthetic intake vs target
    assert "120 g" in n and "130 g" in n and "−10 g" in n and "sopra il target" in n
    s = ask.ask(conn, "quante sedute ho fatto questa settimana?", TODAY).text
    assert "1 sedute di pesi fatte** su 1" in s
    g = ask.ask(conn, "cosa devo fare oggi in palestra?", TODAY).text
    assert "Corsa: easy, 30 min" in g and "Il carico" not in g  # Wednesday: run only in the synthetic programme
    m = ask.ask(conn, "cosa devo fare oggi in palestra?", TODAY + timedelta(days=5)).text  # next Monday
    assert "Panca" in m and "3 ×" in m


def test_a_calorie_proposal_is_explained_from_the_rule_output(conn):
    out = {"status": "propose", "current_kcal": 9000, "proposed_kcal": 8800, "change_kcal": -200, "rule":
           "calorie_adjustment@2", "notify": "Proposta: target −200 kcal (velocità −0,10 %/sett, sopra la fascia)"}
    conn.execute("INSERT INTO rule_execution(id, rule_id, local_date, inputs, output, recorded_at) VALUES "
                 "(?,?,?,?,?,?)", (new_id(), "calorie_adjustment@2", TODAY.isoformat(), "{}", json.dumps(out),
                                   "2025-04-02T08:00:00+00:00"))
    ans = ask.ask(conn, "perché mi avete proposto di cambiare le calorie", TODAY)
    assert ans.ok and "9000 kcal" in ans.text and "8800 kcal" in ans.text and "approvi" in ans.text


def test_an_unsupported_number_never_reaches_the_athlete(conn, monkeypatch):
    def liar(c, day, q):
        return ask.Answer("weight_since_start", "Hai perso **12,5 kg** [dati:peso].", {"peso": [1.0]})
    monkeypatch.setattr(ask, "weight_since_start", liar)
    monkeypatch.setattr(ask, "RULES", [("weight_since_start", lambda q: True, liar)])
    ans = ask.ask(conn, "quanto peso ho perso?", TODAY)
    assert not ans.ok and "12,5" not in ans.text and "non lo so" in ans.text.lower()


def test_asking_writes_nothing(conn):
    before = conn.execute("SELECT (SELECT COUNT(*) FROM raw_record), (SELECT COUNT(*) FROM rule_execution), "
                          "(SELECT COUNT(*) FROM plan_version)").fetchone()
    for it in EVAL["regular"] + EVAL["traps"]:
        ask.ask(conn, it["q"], TODAY)
    after = conn.execute("SELECT (SELECT COUNT(*) FROM raw_record), (SELECT COUNT(*) FROM rule_execution), "
                         "(SELECT COUNT(*) FROM plan_version)").fetchone()
    assert tuple(before) == tuple(after)


def test_the_page_takes_questions_only_by_post(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "web.db"))
    cfg = config.load()
    ingest(connect(cfg.db_path), syn.athlete() + syn.linear_weights(10), "synthetic")
    password = secrets.token_urlsafe(16)
    auth.set_password(paths(cfg)[0], password)
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1")
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"csrf": tok, "password": password})
    page = client.get("/chiedi").text
    assert "Quanto peso ho perso dall&#39;inizio?" in page and 'method="post" action="/chiedi"' in page
    assert client.get("/chiedi?q=quanto+peso").text.count("asked") == 0  # a GET never answers
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post("/chiedi", data={"csrf": tok, "q": "quanto peso adesso?"})
    assert "Peso di tendenza" in r.text and "[dati:" not in r.text  # references become the sources row
    assert client.post("/chiedi", data={"csrf": "x", "q": "peso"}).text.count("Sessione scaduta") == 1
