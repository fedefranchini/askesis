"""Running records, record timeline and the dashboard section (synthetic values only)."""

from __future__ import annotations

import secrets
from datetime import date, timedelta

import pytest
import synthetic as syn
from starlette.testclient import TestClient

from askesis import config
from askesis.analytics import engine, records
from askesis.analytics.base import MetricValue
from askesis.analytics.training import RunRow
from askesis.ingestion.pipeline import ingest
from askesis.store.db import connect
from askesis.web import auth
from askesis.web.app import create_app, paths

D0 = date(2030, 1, 7)
FAR = date(2031, 1, 1)


def day(n: int) -> date:
    return D0 + timedelta(days=n)


def run(n, km, mins, **kw):
    return RunRow(day(n), km * 1000, int(mins * 60), 95, **kw)


def rec(runs, subject=None, end=FAR):
    out = records.run_records(runs, D0, end)
    return [m for m in out if subject is None or m.subject == subject]


def test_first_run_is_baseline_not_a_record():
    assert rec([run(0, 35, 400)]) == []


def test_distance_and_duration_records():
    runs = [run(0, 35, 400), run(2, 36, 380), run(4, 33, 450)]
    dist = rec(runs, "run:distance")
    assert [(m.period_start, m.value, m.detail["previous_km"]) for m in dist] == [(day(2), 36.0, 35.0)]
    dur = rec(runs, "run:duration")
    assert [(m.period_start, m.value, m.detail["previous_min"]) for m in dur] == [(day(4), 450.0, 400.0)]


def test_pace_records_use_buckets_and_moving_time():
    runs = [run(0, 35, 350), run(2, 38, 340)]  # 10:00/km then 8:57/km: record in every bucket
    for x in (3, 5, 10):
        got = rec(runs, f"run:pace@{x}km:outdoor")
        assert len(got) == 1 and got[0].unit == "s/km" and got[0].detail["previous_s_km"] == 600.0
    short = [run(0, 4, 40), run(2, 38, 340)]  # the baseline of the 10 km bucket is the long run itself
    assert rec(short, "run:pace@10km:outdoor") == []
    assert len(rec(short, "run:pace@3km:outdoor")) == 1
    moving = [run(0, 35, 350), run(2, 35, 400, moving_s=300 * 60)]  # elapsed slower, moving faster
    assert rec(moving, "run:pace@10km:outdoor")[0].value == pytest.approx(300 * 60 / 35)


def test_pace_environments_are_separate():
    runs = [run(0, 35, 350), run(2, 35, 300, environment="treadmill"), run(4, 35, 280, environment="treadmill")]
    assert rec(runs, "run:pace@10km:outdoor") == []
    assert [m.period_start for m in rec(runs, "run:pace@10km:treadmill")] == [day(4)]


def test_same_day_compared_with_earlier_days_only_and_best_wins():
    runs = [run(0, 35, 350), run(2, 36, 330), run(2, 37, 320)]
    assert [m.value for m in rec(runs, "run:distance")] == [37.0]
    pace = rec(runs, "run:pace@10km:outdoor")
    assert len(pace) == 1 and pace[0].value == pytest.approx(320 * 60 / 37)
    assert rec([run(0, 35, 350), run(0, 40, 300)]) == []  # same day as the only history: baseline


def test_no_look_ahead_and_window():
    runs = [run(0, 35, 350), run(2, 36, 330), run(5, 30, 250), run(7, 41, 300)]
    for k in range(1, len(runs) + 1):
        a = records.run_records(runs[:k], D0, FAR)
        b = records.run_records(runs, D0, runs[k - 1].day)
        assert a == b
    assert records.run_records(runs, day(3), day(6)) == [m for m in records.run_records(runs, D0, FAR)
                                                         if day(3) <= m.period_start <= day(6)]


def test_engine_integration():
    inp = engine.Inputs(runs=[run(0, 35, 400), run(2, 36, 380), run(9, 50, 500)])
    got = [m for m in engine.compute(inp, D0, day(3)) if m.metric_id == "run_record"]
    assert got and all(m.period_start <= day(3) and m.ref == "run_record@1" for m in got)


def mv(metric, subject, d, value, unit, ep="MEASUREMENT", **detail):
    return MetricValue(metric, 1, subject, d, d, value, unit, ep, 2, detail=detail)


def test_timeline_order_and_texts():
    vals = [
        mv("strength_record", "exercise:bench_press:load", day(1), 300.0, "kg", previous=285.0),
        mv("strength_record", "exercise:bench_press:reps@80", day(2), 9, "reps", load_kg=80.0, previous_reps=8),
        mv("strength_record", "exercise:bench_press:e1rm", day(3), 330.0, "kg", "ESTIMATE", previous_kg=322.5),
        mv("run_record", "run:distance", day(4), 52.0, "km", previous_km=50.5),
        mv("run_record", "run:duration", day(5), 600.0, "min", previous_min=570.0),
        mv("run_record", "run:pace@5km:outdoor", day(6), 310.0, "s/km", previous_s_km=320.0),
        mv("run_record", "run:pace@5km:treadmill", day(6), 305.0, "s/km", previous_s_km=320.0),
        mv("e1rm_session", "exercise:bench_press", day(7), 1.0, "kg"),
    ]
    tl = records.timeline(vals)
    assert [i["date"] for i in tl] == sorted((i["date"] for i in tl), reverse=True) and len(tl) == 7
    texts = {i["text"] for i in tl}
    assert "Panca piana: carico più alto, 300 kg (prima 285 kg)" in texts
    assert "Panca piana: 9 ripetizioni a 80 kg (prima 8)" in texts
    assert "Panca piana: e1RM stimato 330,0 kg (prima 322,5 kg)" in texts
    assert "Corsa: distanza più lunga, 52,0 km (prima 50,5 km)" in texts
    assert "Corsa: durata più lunga, 600 min (prima 570 min)" in texts
    assert "Corsa: passo medio più veloce su almeno 5 km, 5:10/km (prima 5:20/km)" in texts
    assert "Corsa: passo medio più veloce su almeno 5 km, 5:05/km (prima 5:20/km) · tapis roulant" in texts
    first = tl[-1]
    assert first["kind"] == "forza" and first["ref"] == "strength_record@1" and first["epistemic"] == "misura"
    assert {i["epistemic"] for i in tl} == {"misura", "stima"}


# ---------------------------------------------------------------- dashboard
@pytest.fixture
def web(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "rec.db"))
    cfg = config.load()
    password = secrets.token_urlsafe(16)  # generated test credential
    auth.set_password(paths(cfg)[0], password)
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1")
    return client, cfg, password


def _login(client, password):
    import re

    page = client.get("/login").text
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    client.post("/login", data={"csrf": token, "password": password}, follow_redirects=False)


def test_dashboard_empty_state(web):
    client, _, password = web
    _login(client, password)
    page = client.get("/andamenti").text
    assert "Record personali" in page
    assert "Nessun record ancora: la prima seduta o corsa fa da riferimento." in page


def test_dashboard_shows_run_record(web):
    client, cfg, password = web
    ingest(connect(cfg.db_path), syn.athlete() + syn.linear_weights(5) + [
        syn.run(syn.START, 35, 21000, 95), syn.run(syn.START + timedelta(days=2), 41, 24000, 95)], "synthetic")
    _login(client, password)
    page = client.get("/andamenti").text
    assert "Record personali" in page
    assert "Corsa: distanza più lunga, 41,0 km (prima 35,0 km)" in page
    assert "run_record@1" in page and "Nessun record ancora" not in page
