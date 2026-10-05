"""Apple Health export import (synthetic export.zip only)."""

from __future__ import annotations

import json
import zipfile
from datetime import date

import pytest
import synthetic as syn

from askesis import config
from askesis.ingestion import apple_health as ah
from askesis.ingestion.pipeline import ingest
from askesis.store.db import connect

HK = "HKQuantityTypeIdentifier"


def rec(t, start, end, value, unit="", src="iPhone"):
    return (f'<Record type="{t}" sourceName="{src}" unit="{unit}" creationDate="{end}" startDate="{start}" '
            f'endDate="{end}" value="{value}"/>')


def sleep(stage, start, end, src="Watch"):
    return rec("HKCategoryTypeIdentifierSleepAnalysis", start, end, f"HKCategoryValueSleepAnalysis{stage}", src=src)


def export(tmp_path, steps_iphone=(3000, 2000), name="esportazione.zip", export_date="2025-03-20 09:00:00 +0100",
           xml_name="export.xml"):
    body = [
        f'<ExportDate value="{export_date}"/>',
        rec(f"{HK}BodyMass", "2025-03-10 07:00:00 +0100", "2025-03-10 07:00:00 +0100", "67.5", "kg", "Salute"),
        rec(f"{HK}StepCount", "2025-03-10 09:00:00 +0100", "2025-03-10 10:00:00 +0100", steps_iphone[0], "count"),
        rec(f"{HK}StepCount", "2025-03-10 15:00:00 +0100", "2025-03-10 16:00:00 +0100", steps_iphone[1], "count"),
        rec(f"{HK}StepCount", "2025-03-10 09:00:00 +0100", "2025-03-10 16:00:00 +0100", "4500", "count", "Watch"),
        rec(f"{HK}StepCount", "2025-03-20 08:00:00 +0100", "2025-03-20 08:30:00 +0100", "900", "count"),  # open day
        rec(f"{HK}RestingHeartRate", "2025-03-10 00:00:00 +0100", "2025-03-10 23:59:00 +0100", "58", "count/min",
            "Watch"),
        sleep("AsleepCore", "2025-03-10 23:30:00 +0100", "2025-03-11 03:00:00 +0100"),
        sleep("AsleepDeep", "2025-03-11 03:00:00 +0100", "2025-03-11 04:00:00 +0100"),
        sleep("AsleepREM", "2025-03-11 04:00:00 +0100", "2025-03-11 06:30:00 +0100"),
        sleep("InBed", "2025-03-10 23:00:00 +0100", "2025-03-11 07:00:00 +0100", src="iPhone"),
        rec(f"{HK}DietaryEnergyConsumed", "2025-03-10 08:00:00 +0100", "2025-03-10 08:00:00 +0100", "400", "kcal",
            "NutritionApp"),
        rec(f"{HK}DietaryEnergyConsumed", "2025-03-10 13:00:00 +0100", "2025-03-10 13:00:00 +0100", "700", "kcal",
            "NutritionApp"),
        rec(f"{HK}DietaryEnergyConsumed", "2025-03-11 01:30:00 +0100", "2025-03-11 01:30:00 +0100", "300", "kcal",
            "NutritionApp"),  # before the 03:00 cutoff: belongs to 10 March
        rec(f"{HK}DietaryProtein", "2025-03-10 13:00:00 +0100", "2025-03-10 13:00:00 +0100", "50", "g",
            "NutritionApp"),
        rec(f"{HK}DietaryEnergyConsumed", "2025-03-12 13:00:00 +0100", "2025-03-12 13:00:00 +0100", "900", "kcal",
            "NutritionApp"),  # a manual record exists for this day
        rec(f"{HK}HeartRate", "2025-03-10 10:00:00 +0100", "2025-03-10 10:00:00 +0100", "80", "count/min", "Watch"),
        ('<Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" '
         'sourceName="Watch" startDate="2025-03-12 18:00:00 +0100" endDate="2025-03-12 18:30:00 +0100">'
         f'<WorkoutStatistics type="{HK}DistanceWalkingRunning" sum="5.0" unit="km"/>'
         f'<WorkoutStatistics type="{HK}HeartRate" average="150.4" maximum="171.6" unit="count/min"/></Workout>'),
        ('<Workout workoutActivityType="HKWorkoutActivityTypeTraditionalStrengthTraining" duration="55" '
         'durationUnit="min" sourceName="Watch" startDate="2025-03-13 18:00:00 +0100" '
         'endDate="2025-03-13 18:55:00 +0100"/>'),
    ]
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="it_IT">' + "".join(body) + "</HealthData>"
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"apple_health_export/{xml_name}", xml)
        zf.writestr("apple_health_export/export_cda.xml", "<x/>")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "h.db"))
    cfg = config.load()
    conn = connect(cfg.db_path)
    manual = syn.constant_intake(1, start=date(2025, 3, 12))
    ingest(conn, manual, "s")
    return cfg, conn


def by(plan, entity):
    return [r for r in plan.records if r["entity_type"] == entity]


def test_mapping_rules(env, tmp_path):
    cfg, conn = env
    plan = ah.build(ah.parse(export(tmp_path)), cfg, conn)
    (steps,) = by(plan, "daily_activity")
    assert steps["payload"] == {"steps": 5000} and steps["device_id"] == "iPhone"  # max source, never the sum
    assert plan.counts[("daily_activity", "escluso: giorno non chiuso all'esportazione")] == 1
    (sl,) = by(plan, "sleep_session")
    assert sl["local_date"] == date(2025, 3, 11) and sl["payload"]["asleep_s"] == 7 * 3600
    assert sl["device_id"] == "Watch"  # the source with sleep stages wins
    food = {r["local_date"]: r["payload"] for r in by(plan, "nutrition_day")}
    assert food[date(2025, 3, 10)] == {"energy_kcal": 1400, "protein_g": 50.0, "completeness": "partial",
                                       "logging_method": "via_apple_health"}
    assert date(2025, 3, 12) not in food  # the manual record of the same day wins
    assert plan.counts[("nutrition_day", "saltato: esiste già un dato di un'altra sorgente")] == 1
    (run,) = by(plan, "running_session")
    assert run["payload"] == {"distance_m": 5000.0, "elapsed_s": 1800, "avg_hr": 150, "max_hr": 172}
    assert by(plan, "training_session")[0]["original_values"]["elapsed_s"] == 3300
    assert by(plan, "body_weight")[0]["payload"] == {"value_kg": 67.5}
    assert by(plan, "resting_hr_daily")[0]["payload"] == {"bpm": 58}


def test_import_is_idempotent_and_valid(env, tmp_path):
    cfg, conn = env
    path = export(tmp_path)
    plan = ah.build(ah.parse(path), cfg, conn)
    r = ingest(conn, plan.records, "apple_health_export", source_kind="imported")
    assert not r.rejected and len(r.inserted) == len(plan.records)
    again = ah.build(ah.parse(path), cfg, conn)
    assert again.records == [] and all(k[1] in ("già presente", "escluso: giorno non chiuso all'esportazione",
                                               "saltato: esiste già un dato di un'altra sorgente")
                                       for k in again.counts)


def test_changed_daily_total_becomes_a_correction(env, tmp_path):
    cfg, conn = env
    first = ah.build(ah.parse(export(tmp_path, name="a.zip")), cfg, conn)
    ingest(conn, first.records, "apple_health_export", source_kind="imported")
    later = ah.build(ah.parse(export(tmp_path, steps_iphone=(3000, 2600), name="b.zip")), cfg, conn)
    (steps,) = by(later, "daily_activity")
    assert steps["payload"] == {"steps": 5600} and steps["supersedes_id"]
    r = ingest(conn, later.records, "apple_health_export", source_kind="imported")
    assert not r.rejected
    cur = conn.execute("SELECT payload FROM v_current WHERE entity_type = 'daily_activity'").fetchall()
    assert [json.loads(x["payload"]) for x in cur] == [{"steps": 5600}]  # old value kept in history, not current


def test_not_imported_types_are_counted_and_bad_files_rejected(env, tmp_path):
    data = ah.parse(export(tmp_path))
    assert data.not_imported[f"{HK}HeartRate"] == 1
    bad = tmp_path / "x.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("readme.txt", "nope")
    with pytest.raises(ValueError, match="XML principale"):
        ah.parse(bad)


def test_cli_previews_then_saves(env, tmp_path):
    from typer.testing import CliRunner

    from askesis.cli.main import app

    path = export(tmp_path)
    runner = CliRunner()
    pv = runner.invoke(app, ["import-health", str(path)])
    assert "ANTEPRIMA" in pv.output and "daily_activity" in pv.output and "HeartRate 1" in pv.output
    saved = runner.invoke(app, ["import-health", str(path), "--yes"])
    assert "inseriti" in saved.output and "rifiutati 0" in saved.output


def test_localised_export_file_names(env, tmp_path):
    cfg, conn = env
    path = export(tmp_path, name="dati esportati.zip", xml_name="dati esportati.xml")
    assert by(ah.build(ah.parse(path), cfg, conn), "daily_activity")[0]["payload"] == {"steps": 5000}


def test_same_session_written_by_two_apps_is_kept_once():
    from datetime import datetime, timedelta

    t0 = datetime(2025, 3, 13, 18, 0).astimezone()
    w = {"type": "HKWorkoutActivityTypeTraditionalStrengthTraining", "start": t0, "end": t0 + timedelta(minutes=55),
         "source": "LiftApp", "elapsed_s": 3300, "distance_m": None, "avg_hr": None, "max_hr": None}
    watch = w | {"source": "Watch", "start": t0 + timedelta(minutes=2), "avg_hr": 120,
                 "type": "HKWorkoutActivityTypeFunctionalStrengthTraining"}
    other_day = w | {"start": t0 + timedelta(days=1), "end": t0 + timedelta(days=1, minutes=50)}
    kept = ah.dedup_workouts([w, watch, other_day])
    assert [k["source"] for k in kept] == ["Watch", "LiftApp"]


def test_excluded_source_is_ignored(env, tmp_path):
    cfg, conn = env
    plan = ah.build(ah.parse(export(tmp_path)), cfg, conn, exclude_sources={"iphone"})
    (steps,) = by(plan, "daily_activity")
    assert steps["payload"] == {"steps": 4500} and steps["device_id"] == "Watch"
