"""Adherence and noise metrics for the weekly review (synthetic data)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from askesis.analytics import adherence as ad
from askesis.analytics import training
from askesis.analytics.energy import NutritionDay
from askesis.analytics.params import p

MON = date(2025, 3, 3)
SUN = MON + timedelta(days=6)


def plans(target_from=MON, **target):
    t = {"energy_kcal": 1800, "protein_g": 100} | target
    lift = {"exercise": "bench_press", "sets": 3, "rep_range": [6, 8], "target_rir": 2}
    prog = {"microcycle": [{"day": d, "name": d, "lifts": [lift]} for d in ("mon", "wed", "fri")]
            + [{"day": "sat", "name": "run", "run": {"kind": "easy"}}],
            "minimal_week": [{"day": "mon", "name": "m", "lifts": [lift]}]}
    return [(target_from, "2025-01-01", "nutrition_target", t), (MON, "2025-01-01", "programme", prog)]


def test_intake_vs_target_uses_complete_days_and_a_confidence_interval():
    days = [NutritionDay(MON + timedelta(days=i), 1900 + 20 * i, 100, "complete") for i in range(5)]
    days.append(NutritionDay(SUN, 900, 40, "partial"))  # ignored
    m = {x.metric_id: x for x in ad.vs_target(days, plans(), MON, SUN)}
    e = m["intake_vs_target_week"]
    assert e.value == pytest.approx(140) and e.n_obs == 5 and e.lo > 0  # beyond day-to-day noise
    assert m["protein_vs_target_week"].value == 0


def test_target_valid_later_is_not_used():
    days = [NutritionDay(MON + timedelta(days=i), 1900, 100, "complete") for i in range(5)]
    assert ad.vs_target(days, plans(target_from=SUN + timedelta(days=1)), MON, SUN) == []


def test_sessions_vs_plan_counts_lifting_sessions_and_minimal_week():
    sets = [training.SetRow(MON, "s1", "bench_press", "panca", "working", 50, "external", 8, 2, None),
            training.SetRow(MON + timedelta(days=2), "s2", "bench_press", "panca", "working", 50, "external", 8, 2,
                            None)]
    m = ad.sessions_vs_plan(sets, plans(), MON, SUN)
    assert m.value == 2 and m.detail["planned"] == 3
    period = (MON, "2025-01-01", "scheduled_period",
              {"label": "x", "start": MON.isoformat(), "end": SUN.isoformat(), "modifiers": {"use_minimal_week": True}})
    assert ad.sessions_vs_plan(sets, plans() + [period], MON, SUN).detail["planned"] == 1


def test_waist_change_noise_needs_enough_repeated_sessions():
    w = [(MON - timedelta(days=21), [80.0, 80.4]), (MON - timedelta(days=14), [79.8, 80.2]),
         (MON - timedelta(days=7), [79.6, 80.0]), (MON, [78.0, 78.4])]
    (m,) = ad.waist_change(w, MON, SUN)
    tem = math.sqrt(0.08)  # each pair differs by 0.4 cm → sample variance 0.08
    md = p("minimal_difference_z") * math.sqrt(2) * tem / math.sqrt(2)
    assert m.value == pytest.approx(-1.6) and m.detail["minimal_difference_cm"] == pytest.approx(md)
    assert m.hi < 0  # real change
    (early,) = ad.waist_change(w[-2:], MON, SUN)
    assert early.lo is None and early.detail["reason"] == "noise_not_estimable"


def test_e1rm_change_against_past_week_to_week_spread():
    weeks = [(MON - timedelta(days=7 * k) + timedelta(days=6), v) for k, v in
             zip(range(6, -1, -1), [60, 61, 60, 61.5, 61, 62, 70], strict=True)]
    (m,) = ad.e1rm_change({"bench_press": weeks}, MON, SUN)
    assert m.value == pytest.approx(8) and m.lo > 0
    (few,) = ad.e1rm_change({"bench_press": weeks[-3:]}, MON, SUN)
    assert few.lo is None
