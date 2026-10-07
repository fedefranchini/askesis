"""Today summary (synthetic data): numbers from data and metrics, colours only against a real target or threshold."""

from __future__ import annotations

from datetime import timedelta

import pytest
import synthetic as syn
from test_derive import UNTIL, approved

from askesis.analytics import engine, today
from askesis.analytics.params import p
from askesis.analytics.training import SetRow
from askesis.ingestion.pipeline import ingest
from askesis.plan import store
from askesis.store.db import connect

MON = syn.START
LIFT = {"exercise": "bench_press", "sets": 3, "rep_range": [6, 8], "target_rir": 2}
PROG = {"microcycle": [{"day": d, "name": d, "lifts": [LIFT]} for d in ("mon", "wed", "fri")],
        "minimal_week": [{"day": "mon", "name": "m", "lifts": [LIFT]}]}


def target(**kw) -> dict:
    base = {"energy_kcal": 1800, "protein_g": 120, "protein_basis": {"method": "per_kg_bw"}, "method": "prior_only"}
    return base | kw


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    ingest(c, syn.athlete() + syn.linear_weights(21) + syn.constant_intake(21), "s")
    return c


def tiles(c, day) -> dict:
    return {t["key"]: t for t in today.summary(c, day)["tiles"]}


def test_energy_and_protein_colours_follow_the_versioned_tolerances(conn):
    day = MON + timedelta(days=10)
    t = tiles(conn, day)
    assert t["energy"]["status"] == "neutral" and t["energy"]["progress"] is None  # no target: no colour
    assert t["energy"]["value"] == syn.KCAL
    store.add_version(conn, "nutrition_target", "t", target(), MON, None)
    t = tiles(conn, day)
    assert t["energy"]["status"] == "ok"  # 1900 vs 1800: 5.6% ≤ 10% (calorie_adjustment@2)
    assert t["energy"]["detail"]["tolerance"]["ref"] == "calorie_adjustment@2"
    assert t["protein"]["status"] == "ok" and t["protein"]["progress"] == pytest.approx(1.0)
    store.add_version(conn, "nutrition_target", "t", target(energy_kcal=1700, protein_g=200), MON + timedelta(days=5),
                      None)
    t = tiles(conn, day)
    assert t["energy"]["status"] == "warn"  # 1900 vs 1700: 11.8%
    assert t["protein"]["status"] == "warn"  # synthetic intake far below 200 × (1 − tolerance)
    store.add_version(conn, "nutrition_target", "t", target(protein_g=60), MON + timedelta(days=6), None)
    assert tiles(conn, day)["protein"]["status"] == "ok"  # above target is never flagged


def test_weight_has_no_colour_and_states_trend_against_noise(conn, tmp_path):
    t = tiles(conn, MON + timedelta(days=20))["weight"]
    assert t["status"] == "neutral" and t["ref"] == "weight_ema@1"
    assert "oltre il rumore" in t["sub"] and t["detail"]["hi"] < 0
    flat = connect(tmp_path / "flat.db")
    ingest(flat, syn.athlete() + syn.linear_weights(21, slope=0.0), "s")
    assert "nel rumore" in tiles(flat, MON + timedelta(days=20))["weight"]["sub"]


def test_sessions_count_only_days_already_past():
    plans = [(MON, "2025-01-01", "programme", PROG)]
    wed = MON + timedelta(days=2)
    done_mon = [SetRow(MON, "s1", "bench_press", "panca", "working", 50, "external", 8, 2, None)]
    ok = today.sessions_tile(engine.Inputs(sets=done_mon, plans=plans), wed)
    assert (ok.value, ok.detail["planned"], ok.status) == (1, 3, "ok")  # Wednesday's session is still possible
    late = today.sessions_tile(engine.Inputs(plans=plans), wed)
    assert late.status == "warn" and late.sub == "1 da recuperare"
    assert today.sessions_tile(engine.Inputs(), wed).status == "neutral"


def test_sleep_uses_the_verified_threshold_and_needs_enough_nights():
    day = MON + timedelta(days=6)
    few = engine.Inputs(sleep=[(day - timedelta(days=i), 6 * 3600) for i in range(3)])
    assert today.sleep_tile(few, day).status == "neutral"
    short = engine.Inputs(sleep=[(day - timedelta(days=i), 6 * 3600) for i in range(5)])
    long = engine.Inputs(sleep=[(day - timedelta(days=i), 7.5 * 3600) for i in range(5)])
    assert today.sleep_tile(short, day).status == "warn" and today.sleep_tile(long, day).status == "ok"
    assert today.sleep_tile(long, day).detail["threshold"] == p("sleep_recommended_min_h")


def test_steps_are_coloured_only_with_a_target_in_the_plan():
    day = MON + timedelta(days=8)
    steps = {MON + timedelta(days=i): 9000 for i in range(8)}
    plain = today.steps_tile(engine.Inputs(steps=steps), day)
    assert plain.status == "neutral" and plain.progress is None and plain.value == 9000
    plans = [(MON, "2025-01-01", "nutrition_target", target(steps_target=8000))]
    assert today.steps_tile(engine.Inputs(steps=steps, plans=plans), day).status == "ok"
    plans = [(MON, "2025-01-01", "nutrition_target", target(steps_target=10000))]
    assert today.steps_tile(engine.Inputs(steps=steps, plans=plans), day).status == "warn"


def test_phase_progress_and_upcoming_start(conn):
    assert today.phase_progress(conn, engine.load_inputs(conn), UNTIL) is None
    approved(conn)
    up = today.phase_progress(conn, engine.load_inputs(conn), UNTIL)
    assert up["upcoming"] and up["start"] == (UNTIL + timedelta(days=1)).isoformat()
    phase = {"phase": "fat_loss", "exit_criteria": [{"description": "x"}], "max_duration_weeks": 12,
             "next_phase": "maintenance"}
    store.add_version(conn, "phase", "f", phase, MON + timedelta(days=7), None)
    ph = today.phase_progress(conn, engine.load_inputs(conn), MON + timedelta(days=20))
    assert (ph["week"], ph["of"]) == (2, 12) and ph["weight_change_kg"] < 0


def test_no_look_ahead(conn, tmp_path):
    day = MON + timedelta(days=13)
    before = today.summary(conn, day)
    ingest(conn, syn.linear_weights(5, MON + timedelta(days=21), 50.0, 0.0), "s")
    assert today.summary(conn, day)["tiles"] == before["tiles"]


def test_todo_lists_missing_entries(conn):
    assert today.todo(conn, MON + timedelta(days=10)) == ["Check-in del mattino"]
    assert today.todo(conn, MON + timedelta(days=30)) == ["Pesata di oggi", "Cibo di ieri", "Check-in del mattino"]


def test_session_done_today(conn):
    day = MON + timedelta(days=10)
    assert today.summary(conn, day)["session_done"] is False
    ingest(conn, syn.strength_session(day), "s")
    assert today.summary(conn, day)["session_done"] is True
