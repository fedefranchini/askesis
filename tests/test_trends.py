"""Trends page as four questions (synthetic data): verdicts from rules and parameters, clear empty states."""

from __future__ import annotations

from datetime import timedelta

import pytest
import synthetic as syn

from askesis.analytics import engine, trends
from askesis.analytics.params import p
from askesis.ingestion.pipeline import ingest
from askesis.plan import store
from askesis.store.db import connect

MON = syn.START
DAY = MON + timedelta(days=20)
LIFT = {"sets": 3, "rep_range": [6, 8], "target_rir": 2}
PROG = {"microcycle": [
    {"day": "mon", "name": "A", "lifts": [{"exercise": "bench_press", **LIFT}, {"exercise": "lateral_raise", **LIFT},
                                          {"exercise": "back_squat", **LIFT}, {"exercise": "leg_press", **LIFT}]},
    {"day": "thu", "name": "B", "lifts": [{"exercise": "romanian_deadlift", **LIFT}]}],
    "minimal_week": [{"day": "mon", "name": "m", "lifts": [{"exercise": "bench_press", **LIFT}]}]}
PHASE = {"phase": "fat_loss", "exit_criteria": [{"description": "x"}], "max_duration_weeks": 12,
         "next_phase": "maintenance"}


def target(**kw):
    return {"energy_kcal": 1800, "protein_g": 110, "protein_basis": {}, "method": "prior_only"} | kw


def world(tmp_path, slope=-0.05, name="t", phase=True, weights=21):
    c = connect(tmp_path / f"{name}.db")
    ingest(c, syn.athlete() + syn.linear_weights(weights, slope=slope) + syn.constant_intake(21), "s")
    if phase:
        store.add_version(c, "phase", "f", PHASE, MON, None)
    return c


def q(c, key, day=DAY, days=28):
    return next(x for x in trends.questions(c, day, days) if x["key"] == key)


@pytest.mark.parametrize("slope,status", [(-0.05, "ok"), (-0.02, "warn"), (-0.2, "warn"), (0.0, "noise")])
def test_rate_is_judged_against_the_target_band(tmp_path, slope, status):
    r = q(world(tmp_path, slope), "rate")
    assert r["status"] == status and "calorie_adjustment@2" in r["caption"]
    assert r["spark"] and r["unit"] == "% a settimana"


def test_rate_without_a_fat_loss_phase_has_no_target(tmp_path):
    r = q(world(tmp_path, phase=False), "rate")
    assert r["status"] == "neutral" and r["verdict"] == "in calo oltre il rumore" and not r["caption"]


def test_rate_empty_state_says_what_is_missing(tmp_path):
    r = q(world(tmp_path, weights=5), "rate", day=MON + timedelta(days=4))
    need = p("weight_rate_min_points")["14"]
    assert r["status"] == "empty" and f"Servono almeno {need} pesate" in r["empty"] and "ne hai 5" in r["empty"]


def test_plan_adherence(tmp_path):
    c = world(tmp_path)
    assert q(c, "plan")["status"] == "empty" and "Nessun piano attivo" in q(c, "plan")["empty"]
    store.add_version(c, "nutrition_target", "t", target(), MON, None)
    assert q(c, "plan")["status"] == "ok"  # 1900 vs 1800, protein above target
    store.add_version(c, "nutrition_target", "t", target(energy_kcal=1600), MON + timedelta(days=1), None)
    assert q(c, "plan")["status"] == "warn"
    store.add_version(c, "programme", "p", PROG, MON, None)
    facts = q(c, "plan")["facts"]
    assert any(f.startswith("sedute 0 su") for f in facts)


def test_fundamentals_are_the_first_multi_joint_lifts():
    inp = engine.Inputs(plans=[(MON, "x", "programme", PROG)])
    assert trends.fundamentals(inp, DAY) == ["bench_press", "back_squat", "romanian_deadlift"]


def test_strength_states(tmp_path):
    c = world(tmp_path)
    assert "Nessun programma attivo" in q(c, "strength")["empty"]
    store.add_version(c, "programme", "p", PROG, MON, None)
    assert "Panca piana" in q(c, "strength")["empty"]  # no sets yet: names the lifts
    for w in range(3):
        ingest(c, syn.strength_session(MON + timedelta(weeks=w)), "s")
    s = q(c, "strength")
    assert s["status"] == "neutral" and s["verdict"] == "rumore non ancora stimabile" and s["value"]


def test_recovery_flags_short_sleep(tmp_path):
    from askesis.analytics import trends as t

    inp = engine.Inputs(sleep=[(DAY - timedelta(days=i), 6 * 3600) for i in range(6)])
    r = t.q_recovery(None, inp, DAY, 28)
    assert r.status == "warn" and r.spark_target == p("sleep_recommended_min_h")


def test_no_look_ahead(tmp_path):
    c = world(tmp_path)
    before = trends.questions(c, DAY, 28)
    ingest(c, syn.linear_weights(5, DAY + timedelta(days=1), 40.0, 0.0), "s")
    assert trends.questions(c, DAY, 28) == before


def test_unknown_period_falls_back_to_four_weeks(tmp_path):
    c = world(tmp_path)
    assert trends.questions(c, DAY, 9999) == trends.questions(c, DAY, 28)


def test_stale_weighins_do_not_give_a_rate(tmp_path):
    r = q(world(tmp_path), "rate", day=DAY + timedelta(days=30))
    assert r["status"] == "empty" and "Ultima pesata" in r["empty"]


def test_plan_starting_today_explains_when_the_first_comparison_comes(tmp_path):
    c = world(tmp_path)
    store.add_version(c, "nutrition_target", "t", target(), DAY, None)  # in force from today
    r = q(c, "plan")
    assert r["status"] == "empty" and r["empty"].startswith("Piano in vigore dal") and not r["empty"].endswith(": .")
