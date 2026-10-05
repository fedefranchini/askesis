"""Hevy workout export import (synthetic CSV only)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from askesis import config
from askesis.ingestion import hevy
from askesis.ingestion.pipeline import ingest
from askesis.plan import rules
from askesis.store.db import connect

HEADER = ("title,start_time,end_time,description,exercise_title,superset_id,exercise_notes,set_index,set_type,"
          "{w},reps,distance_km,duration_seconds,rpe")
ROWS = [
    ("Push A", "Bench Press (Barbell)", 0, "warmup", "30", "10", ""),
    ("Push A", "Bench Press (Barbell)", 1, "normal", "50", "8", "8"),
    ("Push A", "Bench Press (Barbell)", 2, "failure", "50", "6", "10"),
    ("Push A", "Lateral Raise (Dumbbell)", 0, "normal", "6", "12", ""),
    ("Push A", "Pull Up", 0, "normal", "", "7", ""),
    ("Push A", "Treadmill", 0, "normal", "", "", ""),
]
MAPPING = {"Bench Press (Barbell)": "bench_press", "Lateral Raise (Dumbbell)": "lateral_raise",
           "Pull Up": "pull_up", "Treadmill": "ignore"}


def csv_file(tmp_path, unit="weight_kg", factor=1.0, name="w.csv"):
    lines = [HEADER.format(w=unit)]
    for title, ex, idx, kind, w, reps, rpe in ROWS:
        w2 = str(round(float(w) * factor, 2)) if w else ""
        dur = "600" if ex == "Treadmill" else ""
        lines.append(f'{title},"10 Mar 2025, 18:00","10 Mar 2025, 19:10",,"{ex}",,,{idx},{kind},{w2},{reps},,{dur},'
                     f"{rpe}")
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n")
    return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "hv.db"))
    cfg = config.load()
    return cfg, connect(cfg.db_path)


def test_unmapped_exercises_block_the_import(env, tmp_path):
    cfg, conn = env
    sessions = hevy.parse(csv_file(tmp_path), cfg.timezone)
    plan = hevy.build(sessions, {"Bench Press (Barbell)": "bench_press"}, cfg, conn)
    assert plan.records == [] and "Pull Up" in plan.unmapped


def test_proposals_respect_equipment_and_are_only_proposals():
    assert hevy.propose("Bench Press (Barbell)") == "bench_press"
    assert hevy.propose("Bench Press (Dumbbell)") == "db_bench_press"
    assert hevy.propose("Underwater Basket Weaving") is None


def test_sets_are_mapped_and_import_is_idempotent(env, tmp_path):
    cfg, conn = env
    sessions = hevy.parse(csv_file(tmp_path), cfg.timezone)
    plan = hevy.build(sessions, MAPPING, cfg, conn)
    sets = [r["payload"] for r in plan.records if r["entity_type"] == "set_record"]
    assert [s["set_type"] for s in sets[:3]] == ["warmup", "working", "working"]
    assert sets[1]["rpe"] == 8 and sets[2].get("to_failure") is True and "rir" not in sets[1]
    pull = next(s for s in sets if s["exercise_raw"] == "Pull Up")
    assert pull["load_kind"] == "bodyweight" and pull["load_kg"] == 0
    assert not any(s["exercise_raw"] == "Treadmill" for s in sets)
    r = ingest(conn, plan.records, "hevy_export", source_kind="imported")
    assert not r.rejected
    again = hevy.build(hevy.parse(csv_file(tmp_path), cfg.timezone), MAPPING, cfg, conn)
    assert again.records == [] and again.counts[("seduta", "già presente")] == 1
    hist = rules.exposures(conn, "bench_press", date(2025, 3, 11))  # starting point for double progression
    assert hist and max(hist[-1].loads) == 50


def test_pounds_are_converted(env, tmp_path):
    cfg, _ = env
    sessions = hevy.parse(csv_file(tmp_path, unit="weight_lbs", factor=1 / hevy.LB, name="lb.csv"), cfg.timezone)
    assert sessions[0].sets[1].load_kg == pytest.approx(50, abs=0.01)


def test_hevy_session_supersedes_the_overlapping_health_session(env, tmp_path):
    cfg, conn = env
    start = datetime(2025, 3, 10, 17, 5, tzinfo=UTC)  # 18:05 in Berlin, inside the Hevy session
    ingest(conn, [{"id": "h1", "entity_type": "training_session", "tz": cfg.timezone, "local_date": date(2025, 3, 10),
                   "recorded_at": start, "source_id": "apple_health", "source_record_id": "x",
                   "entry_method": "imported", "payload": {"session_kind": "strength"}, "start_at": start,
                   "end_at": start + timedelta(minutes=60)}], "apple_health_export", source_kind="imported")
    plan = hevy.build(hevy.parse(csv_file(tmp_path), cfg.timezone), MAPPING, cfg, conn)
    session = next(r for r in plan.records if r["entity_type"] == "training_session")
    assert session["supersedes_id"] == "h1"
    assert not ingest(conn, plan.records, "hevy_export", source_kind="imported").rejected
    cur = conn.execute("SELECT source_id FROM v_current WHERE entity_type = 'training_session'").fetchall()
    assert [r["source_id"] for r in cur] == ["hevy"]


def test_unknown_date_format_is_not_guessed(env, tmp_path):
    cfg, _ = env
    p = tmp_path / "bad.csv"
    p.write_text(HEADER.format(w="weight_kg") + '\nX,"March the tenth",,,"Bench Press (Barbell)",,,0,normal,50,8,,,\n')
    with pytest.raises(hevy.HevyError, match="formato data"):
        hevy.parse(p, cfg.timezone)


def test_cli_writes_a_mapping_draft_then_previews(env, tmp_path):
    import yaml
    from typer.testing import CliRunner

    from askesis.cli.main import app

    runner = CliRunner()
    f, m = csv_file(tmp_path), tmp_path / "map.yaml"
    blocked = runner.invoke(app, ["import-hevy", str(f), "--mapping", str(m)])
    assert blocked.exit_code == 1 and "DA CONFERMARE (proposta: bench_press)" in blocked.output
    runner.invoke(app, ["import-hevy", str(f), "--mapping", str(m), "--write-mapping"])
    assert "# \"Bench Press (Barbell)\": bench_press" in m.read_text()  # proposals stay commented
    m.write_text(yaml.safe_dump({"exercises": MAPPING}))
    pv = runner.invoke(app, ["import-hevy", str(f), "--mapping", str(m)])
    assert pv.exit_code == 0 and "ANTEPRIMA" in pv.output
