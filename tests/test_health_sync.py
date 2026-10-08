"""Daily Health sync from the iOS Shortcut (synthetic payloads only): same rules as the export import, idempotent,
manual always wins, nutrition partial until confirmed, weight off by default, device tokens, probe stores nothing."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, date, datetime

import pytest
import synthetic as syn
from starlette.testclient import TestClient

from askesis import config
from askesis.ingestion import health_sync as hs
from askesis.ingestion.pipeline import ingest
from askesis.store.db import connect
from askesis.web import auth
from askesis.web.app import create_app, paths

NOW = datetime(2025, 3, 13, 7, 30, tzinfo=UTC)  # the sync runs in the morning of 13 March


def block(*rows):
    """rows of (value, unit, start, end, source) → parallel lists, as the Shortcut sends them."""
    cols = list(zip(*rows, strict=True))
    return dict(zip(("value", "unit", "start", "end", "source"), (list(c) for c in cols), strict=True))


def payload(probe=False, **samples):
    return {"v": 1, "window_start": "2025-03-10T07:00:00+01:00", "probe": probe, "samples": samples}


STEPS = block((1000, "count", "2025-03-11T09:00:00+01:00", "2025-03-11T10:00:00+01:00", "iPhone"),
              ("1500", "count", "2025-03-12T09:00:00+01:00", "2025-03-12T10:00:00+01:00", "iPhone"),
              (900, "count", "2025-03-10T09:00:00+01:00", "2025-03-10T10:00:00+01:00", "iPhone"),  # partial day
              (700, "count", "2025-03-13T08:00:00+01:00", "2025-03-13T08:20:00+01:00", "iPhone"))  # today: open
FOOD = block((9000, "kcal", "2025-03-11T13:00:00+01:00", "2025-03-11T13:00:00+01:00", "AppNutrizione"))
PROTEIN = block(("500,5", "g", "2025-03-11T13:00:00+01:00", "2025-03-11T13:00:00+01:00", "AppNutrizione"))
SLEEP = block(("Core", "", "2025-03-12T23:30:00+01:00", "2025-03-13T03:00:00+01:00", "Watch"),
              ("Sonno profondo", "", "2025-03-13T03:00:00+01:00", "2025-03-13T04:00:00+01:00", "Watch"),
              ("Sveglio", "", "2025-03-13T04:00:00+01:00", "2025-03-13T04:10:00+01:00", "Watch"),
              ("A letto", "", "2025-03-12T23:00:00+01:00", "2025-03-13T06:00:00+01:00", "iPhone"))
RHR = block((30, "count/min", "2025-03-12T00:00:00+01:00", "2025-03-12T23:59:00+01:00", "Watch"))
HRV = block((250, "ms", "2025-03-12T03:00:00+01:00", "2025-03-12T03:01:00+01:00", "Watch"),
            (240, "ms", "2025-03-12T05:00:00+01:00", "2025-03-12T05:01:00+01:00", "Watch"))
WEIGHT = block((240, "kg", "2025-03-12T07:00:00+01:00", "2025-03-12T07:00:00+01:00", "Bilancia"))
WORKOUTS = {"type": ["Corsa", "Allenamento tradizionale per la forza", "Yoga"],
            "start": ["2025-03-12T18:00:00+01:00", "2025-03-11T18:00:00+01:00", "2025-03-11T08:00:00+01:00"],
            "end": ["2025-03-12T18:30:00+01:00", "2025-03-11T18:55:00+01:00", "2025-03-11T08:30:00+01:00"],
            "duration": [1800, None, None], "distance": ["5,0", None, None], "distance_unit": ["km", None, None],
            "source": ["Watch", "Watch", "Watch"]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "s.db"))
    cfg = config.load()
    return cfg, connect(cfg.db_path)


def current(conn, entity):
    return {r["local_date"]: (r["source_id"], json.loads(r["payload"])) for r in conn.execute(
        "SELECT local_date, source_id, payload FROM v_current WHERE entity_type = ?", (entity,))}


def full():
    return payload(steps=STEPS, kcal=FOOD, protein=PROTEIN, sleep=SLEEP, rhr=RHR, hrv=HRV, weight=WEIGHT,
                   workouts=WORKOUTS)


# ------------------------------------------------------------------ mapping, same rules as the export import
def test_sync_maps_every_type_with_the_export_rules(env):
    cfg, conn = env
    res = hs.run(conn, cfg, full(), received_at=NOW)
    assert res["inserted"] > 0 and res["rejected"] == 0
    steps = current(conn, "daily_activity")
    assert set(steps) == {"2025-03-11", "2025-03-12"}  # partial first day and the open day are left out
    assert steps["2025-03-12"][1] == {"steps": 1500} and steps["2025-03-12"][0] == "apple_health"
    food = current(conn, "nutrition_day")["2025-03-11"][1]
    assert food == {"energy_kcal": 9000, "protein_g": 500.5, "completeness": "partial",
                    "logging_method": "via_apple_health"}
    sleep = current(conn, "sleep_session")["2025-03-13"][1]
    assert sleep == {"asleep_s": int(4.5 * 3600)}  # awake not counted; one source per night (the one with stages)
    assert current(conn, "resting_hr_daily")["2025-03-12"][1] == {"bpm": 30}
    hrv = current(conn, "hrv_daily")["2025-03-12"][1]
    assert hrv == {"sdnn_ms": 245.0, "n_samples": 2, "method": "apple_sdnn_daily_mean"}
    run = current(conn, "running_session")["2025-03-12"][1]
    assert run == {"distance_m": 5000.0, "elapsed_s": 1800}
    assert "2025-03-11" in current(conn, "training_session")
    assert res["report"]["types"]["workouts"]["labels"] == ["Allenamento tradizionale per la forza", "Corsa", "Yoga"]


def test_weight_is_not_synced_unless_enabled(env, tmp_path):
    cfg, conn = env
    res = hs.run(conn, cfg, full(), received_at=NOW)
    assert "2025-03-12" not in current(conn, "body_weight")
    assert res["report"]["not_synced"]
    (tmp_path / "private.toml").write_text('timezone = "Europe/Berlin"\nhealth_sync_weight = true\n')
    hs.run(conn, config.load(), full(), received_at=NOW)
    assert current(conn, "body_weight")["2025-03-12"][1] == {"value_kg": 240.0}


def test_resending_is_idempotent(env):
    cfg, conn = env
    hs.run(conn, cfg, full(), received_at=NOW)
    n = conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0]
    again = hs.run(conn, cfg, full(), received_at=NOW)
    assert again["inserted"] == 0 and again["already"] > 0
    assert conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == n


def test_manual_always_wins(env):
    cfg, conn = env
    ingest(conn, syn.constant_intake(1, start=date(2025, 3, 11), kcal=8000), "s")
    res = hs.run(conn, cfg, full(), received_at=NOW)
    assert current(conn, "nutrition_day")["2025-03-11"][1]["energy_kcal"] == 8000
    assert res["skipped_other_source"] >= 1
    assert "già registrati a mano" in hs.summary_line(res)


def test_changed_total_becomes_a_correction(env):
    cfg, conn = env
    hs.run(conn, cfg, payload(steps=STEPS), received_at=NOW)
    more = dict(STEPS)
    more = {k: v + [x] for (k, v), x in zip(STEPS.items(), (500, "count", "2025-03-12T18:00:00+01:00",
                                                             "2025-03-12T19:00:00+01:00", "iPhone"), strict=True)}
    hs.run(conn, cfg, payload(steps=more), received_at=NOW)
    assert current(conn, "daily_activity")["2025-03-12"][1] == {"steps": 2000}
    n = conn.execute("SELECT COUNT(*) FROM v_current WHERE entity_type='daily_activity'").fetchone()[0]
    assert n == 2  # superseded, not duplicated


def test_excluded_sources_are_ignored(env, tmp_path):
    (tmp_path / "private.toml").write_text('timezone = "Europe/Berlin"\nhealth_exclude_sources = ["iPhone"]\n')
    cfg = config.load()
    _, conn = env
    res = hs.run(conn, cfg, payload(steps=STEPS), received_at=NOW)
    assert not current(conn, "daily_activity") and res["excluded_sources"] == ["iPhone"]


# ------------------------------------------------------------------ probe and bad input
def test_probe_stores_nothing_and_reports_no_values(env):
    cfg, conn = env
    res = hs.run(conn, cfg, full() | {"probe": True}, received_at=NOW)
    assert res["inserted"] == 0 and conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == 0
    text = json.dumps(res)
    for value in ("9000", "500.5", "250", "1500"):
        assert value not in text
    assert res["report"]["types"]["sleep"]["labels"] == ["A letto", "Core", "Sonno profondo", "Sveglio"]
    assert hs.summary_line(res).startswith("Prova: nulla salvato.")


def test_unreadable_samples_are_counted_never_guessed(env):
    cfg, conn = env
    bad = block(("tanti", "count", "2025-03-11T09:00:00+01:00", "2025-03-11T10:00:00+01:00", "iPhone"),
                (1000, "count", "ieri mattina", "2025-03-11T10:00:00+01:00", "iPhone"))
    sleep = block(("Boh", "", "2025-03-12T23:30:00+01:00", "2025-03-13T03:00:00+01:00", "Watch"))
    res = hs.run(conn, cfg, payload(steps=bad, sleep=sleep, mystery={}), received_at=NOW)
    t = res["report"]["types"]
    assert t["steps"]["understood"] == 0 and sum(t["steps"]["problems"].values()) == 2
    assert t["sleep"]["problems"] == {"etichetta del sonno non riconosciuta": 1}
    assert res["report"]["ignored_types"] == ["mystery"] and res["inserted"] == 0


@pytest.mark.parametrize("bad", [{}, {"v": 2, "samples": {}}, {"v": 1, "samples": {}},
                                 {"v": 1, "window_start": "2099-01-01T00:00:00Z", "samples": {}},
                                 {"v": 1, "window_start": "2025-03-10", "samples": []}])
def test_bad_payloads_are_rejected(env, bad):
    cfg, conn = env
    with pytest.raises(hs.SyncError):
        hs.run(conn, cfg, bad, received_at=NOW)


def test_date_formats():
    tz = "Europe/Berlin"
    assert hs.parse_when("2025-03-11T09:00:00Z", tz) == datetime(2025, 3, 11, 9, tzinfo=UTC)
    assert hs.parse_when("2025-03-11 10:00:00 +0100", tz) == datetime(2025, 3, 11, 9, tzinfo=UTC)
    assert hs.parse_when("2025-03-11T10:00:00", tz) == datetime(2025, 3, 11, 9, tzinfo=UTC)  # naive = local
    assert hs.parse_number("7,5 ms") == 7.5


# ------------------------------------------------------------------ partial days and confirmation
def test_partial_day_confirmation_supersedes_and_later_syncs_skip_it(env):
    cfg, conn = env
    hs.run(conn, cfg, full(), received_at=NOW)
    (day,) = hs.partial_days(conn)
    rec = hs.confirmation_record(conn, cfg, day["day"])
    assert not ingest(conn, [rec], "manual").rejected
    src, pl = current(conn, "nutrition_day")["2025-03-11"]
    assert src == "manual" and pl["completeness"] == "complete" and pl["energy_kcal"] == 9000
    assert not hs.partial_days(conn)
    with pytest.raises(hs.SyncError):
        hs.confirmation_record(conn, cfg, day["day"])
    again = hs.run(conn, cfg, full(), received_at=NOW)
    assert again["inserted"] == 0 and current(conn, "nutrition_day")["2025-03-11"][0] == "manual"


# ------------------------------------------------------------------ tokens and endpoint
def test_tokens_are_hashed_and_revocable(tmp_path):
    path = tmp_path / "t.json"
    secret, rec = hs.create_token(path, "iPhone")
    assert secret.split(".", 1)[1] not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert hs.check_token(path, f"Bearer {secret}") == rec["id"]
    assert hs.check_token(path, f"Bearer {rec['id']}.sbagliato") is None
    assert hs.check_token(path, None) is None and hs.check_token(path, secret) is None
    assert hs.revoke_token(path, rec["id"]) and hs.check_token(path, f"Bearer {secret}") is None


@pytest.fixture
def client(env):
    cfg, _ = env
    auth.set_password(paths(cfg)[0], secrets.token_urlsafe(16))
    secret, _ = hs.create_token(hs.tokens_path(cfg), "iPhone")
    return TestClient(create_app(cfg), base_url="http://127.0.0.1"), secret, cfg


def test_endpoint_needs_a_valid_token(client):
    c, secret, _ = client
    assert c.post("/api/health-sync", json=full()).status_code == 401
    assert c.post("/api/health-sync", json=full(), headers={"Authorization": "Bearer x.y"}).status_code == 401


def test_endpoint_probe_then_sync(client):
    c, secret, cfg = client
    h = {"Authorization": f"Bearer {secret}"}
    r = c.post("/api/health-sync?prova=1", json=full(), headers=h)
    assert r.status_code == 200 and r.json()["result"]["probe"] and r.json()["message"].startswith("Prova")
    r = c.post("/api/health-sync", json=payload(steps=STEPS, kcal=FOOD), headers=h)
    body = r.json()
    assert r.status_code == 200 and body["ok"] and body["result"]["inserted"] == 4  # server clock: 13/3 is closed
    assert "1500" not in r.text and "9000" not in r.text  # counts only, never values
    (tok,) = hs.tokens(hs.tokens_path(cfg))
    assert tok["last_used"] and tok["last_result"]["inserted"] == 4


def test_endpoint_bad_json_saves_nothing(client):
    c, secret, cfg = client
    r = c.post("/api/health-sync", content=b"{non json", headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 400 and r.json()["message"].startswith("Nulla salvato")
    assert connect(cfg.db_path).execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == 0


def test_endpoint_does_not_accept_the_dashboard_session_alone(client):
    c, _secret, _ = client
    r = c.post("/api/health-sync", json=full(), cookies={"askesis_session": "x"})
    assert r.status_code == 401
