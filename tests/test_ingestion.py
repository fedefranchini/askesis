import json
from datetime import UTC, date, datetime, time

import pytest

from askesis.config import Config
from askesis.ingestion import manual, staging
from askesis.ingestion.pipeline import ingest
from askesis.parsers.text import parse_day

CFG = Config(
    timezone="Europe/Berlin", nutrition_cutoff=time(3, 0), weigh_time=time(7, 0), db_path=None,
    backup_dir=None, staging_dir=None, source_id="manual_cli", context_aliases={"xvar": "custom_key"},
)
NOW = datetime(2026, 1, 13, 8, 0, tzinfo=UTC)  # 09:00 local on 2026-01-13
DAY = date(2026, 1, 13)


def build(line):
    return [r for it in parse_day(line, CFG.context_aliases) for r in manual.build(it, CFG, DAY, NOW)]


def test_food_and_steps_go_to_previous_day(conn):
    recs = build("cibo 1850 115 · passi 7000")
    assert {r["entity_type"]: r["local_date"] for r in recs} == {
        "nutrition_day": date(2026, 1, 12), "daily_activity": date(2026, 1, 12)}
    assert len(ingest(conn, recs, "test").inserted) == 2


def test_weight_uses_assumed_morning_time(conn):
    rec = build("p 74.6")[0]
    assert rec["occurred_at"] == datetime(2026, 1, 13, 6, 0, tzinfo=UTC)  # 07:00 local (CET)
    assert rec["original_values"] == {"time_assumed": True}


def test_gym_links_sets_to_session_and_catalog(conn):
    recs = build("pesi: panca 60x8 r2, 60x7 r1 · esercizio inventato 20x10")
    session, *sets = recs
    assert session["entity_type"] == "training_session"
    assert all(s["payload"]["session_id"] == session["id"] for s in sets)
    assert sets[0]["payload"]["exercise_id"] == "bench_press"
    assert "exercise_id" not in sets[2]["payload"]
    r = ingest(conn, recs, "test")
    assert len(r.inserted) == 4
    assert any("non in catalogo" in m for _, _, m in r.issues)


def test_context_alias_maps_to_generic_key(conn):
    rec = build("xvar 4")[0]
    assert rec["entity_type"] == "daily_context" and rec["payload"] == {"key": "custom_key", "value": 4.0}


def test_sleep_assigned_to_wake_date():
    rec = build("sonno 7h")[0]
    assert rec["local_date"] == DAY
    assert (rec["end_at"] - rec["start_at"]).total_seconds() == 7 * 3600
    assert rec["end_at"] == datetime(2026, 1, 13, 6, 0, tzinfo=UTC)  # wake at 07:00 local


SYNTHETIC_STAGING = [
    {"id": "11111111-0000-0000-0000-000000000001", "entity_type": "body_weight", "schema_version": "0.1",
     "occurred_at": "2026-01-10T07:05:00+01:00", "tz": "Europe/Berlin", "local_date": "2026-01-10",
     "recorded_at": "2026-01-10T07:06:00+01:00", "source": "manual_chat", "entry_method": "manual",
     "supersedes_id": None, "missing_reason": None, "payload": {"value_kg": 74.2, "fasted": True}, "notes": ""},
    {"id": "11111111-0000-0000-0000-000000000002", "entity_type": "training_session", "schema_version": "0.1",
     "occurred_at": "2026-01-10T18:00:00+01:00", "tz": "Europe/Berlin", "local_date": "2026-01-10",
     "recorded_at": "2026-01-10T20:00:00+01:00", "source": "manual_chat", "entry_method": "manual",
     "supersedes_id": None, "missing_reason": None,
     "payload": {"session_id": "s1", "start_at": "2026-01-10T18:00:00+01:00",
                 "sets": [{"exercise_raw": "squat", "set_type": "working", "load_kg": 60, "reps": 8, "rir": 2},
                          {"exercise_raw": "squat", "set_type": "working", "load_kg": 60, "reps": 8}]},
     "notes": ""},
    {"id": "11111111-0000-0000-0000-000000000003", "entity_type": "nutrition_summary_reported",
     "schema_version": "0.1", "occurred_at": "2026-01-10T21:00:00+01:00", "tz": "Europe/Berlin",
     "local_date": "2026-01-10", "recorded_at": "2026-01-10T21:00:00+01:00", "source": "manual_chat",
     "entry_method": "manual", "supersedes_id": None, "missing_reason": None,
     "payload": {"source": "app weekly report", "total_kcal": 14000}, "notes": ""},
    {"id": "11111111-0000-0000-0000-000000000004", "entity_type": "decision", "schema_version": "0.1",
     "recorded_at": "2026-01-10T21:30:00+01:00", "tz": "Europe/Berlin", "type": "profile_confirmation",
     "athlete_response_verbatim": "ok", "scope": "test", "status": "accepted"},
]


@pytest.fixture
def staging_file(tmp_path):
    p = tmp_path / "2026-01.ndjson"
    p.write_text("\n".join(json.dumps(r) for r in SYNTHETIC_STAGING) + "\n")
    return p


def test_staging_import_and_idempotency(conn, staging_file):
    records, digest = staging.read([staging_file])
    assert [r["entity_type"] for r in records] == [
        "body_weight", "training_session", "set_record", "set_record", "reported_summary", "decision_log"]
    first = ingest(conn, records, "staging_v0", digest)
    assert len(first.inserted) == 6 and not first.rejected
    records2, _ = staging.read([staging_file])
    second = ingest(conn, records2, "staging_v0", digest)
    assert not second.inserted and len(second.duplicates) == 6


def test_staging_preserves_bitemporal_recorded_at(conn, staging_file):
    records, _ = staging.read([staging_file])
    ingest(conn, records, "staging_v0")
    row = conn.execute("SELECT recorded_at FROM raw_record WHERE entity_type='body_weight'").fetchone()
    assert row["recorded_at"] == "2026-01-10T06:06:00+00:00"
