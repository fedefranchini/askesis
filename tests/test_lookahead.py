"""No look-ahead: a result for a period must never depend on data after that period or after the knowledge cutoff.

The metric test is generic: it compares EVERY metric the engine produces (present and future ones), so a new metric
that peeks at later data fails here without anyone having to add it to a list.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
import synthetic as syn
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_f3 import health, prereg

from askesis.analytics import energy, engine, review, training
from askesis.cli import f3
from askesis.core.ids import new_id
from askesis.ingestion.pipeline import ingest
from askesis.interventions import registry as reg
from askesis.safety import rules as safety
from askesis.store.db import connect

START = syn.START  # Monday
EXERCISES = [("bench_press", "panca"), ("back_squat", "squat"), (None, "esercizio libero")]


# ------------------------------------------------------------------ random synthetic inputs (no database: fast)
def build(rnd: random.Random, n: int) -> engine.Inputs:
    """Random world over n days. Every data stream starts on a random day and has long gaps, so that periods
    WITHOUT data followed by data (the classic look-ahead trap) are common."""
    inp = engine.Inputs()

    def active(stream_start: int, gaps: list[tuple[int, int]], i: int, p: float) -> bool:
        return i >= stream_start and not any(a <= i < b for a, b in gaps) and rnd.random() < p

    def plan() -> tuple[int, list[tuple[int, int]]]:
        start = rnd.choice([0, rnd.randint(0, n - 1)])
        gaps = [(g, g + rnd.randint(3, 14)) for g in (rnd.randint(0, n) for _ in range(rnd.randint(0, 2)))]
        return start, gaps

    streams = {k: plan() for k in ("w", "n", "s", "r", "st", "c", "v")}
    w = rnd.uniform(55, 80)
    for i in range(n):
        d = START + timedelta(days=i)
        inp.dates.append(d)
        w += rnd.gauss(-0.03, 0.3)
        if active(*streams["w"], i, 0.8):
            inp.weighins.append((d, f"{d}T06:00:00+00:00", round(w, 2), rnd.random() < 0.9))
        if active(*streams["n"], i, 0.85):
            comp = rnd.choice(["complete", "complete", "complete", "partial", "not_logged"])
            kcal = None if comp == "not_logged" else rnd.uniform(1200, 2600)
            inp.nutrition.append(energy.NutritionDay(d, kcal, rnd.uniform(60, 180) if kcal else None, comp))
        if active(*streams["s"], i, 0.35):
            sid = new_id()
            for _ in range(rnd.randint(2, 8)):
                ex_id, raw = rnd.choice(EXERCISES)
                inp.sets.append(
                    training.SetRow(
                        d,
                        sid,
                        ex_id,
                        raw,
                        rnd.choice(["working", "working", "warmup"]),
                        rnd.choice([0.0, 20.0, 47.5, 60.0, 82.5]),
                        "external",
                        rnd.randint(1, 15),
                        rnd.choice([None, 0, 1, 2, 3, 5]),
                        None,
                    )
                )
        if active(*streams["r"], i, 0.25):
            inp.runs.append(
                training.RunRow(d, rnd.uniform(2000, 12000), rnd.randint(900, 4500), rnd.choice([None, 140, 155, 170]))
            )
        if active(*streams["st"], i, 0.6):
            inp.steps[d] = rnd.randint(2000, 15000)
        if active(*streams["c"], i, 0.3):
            inp.context.append((d, "custom_key", float(rnd.randint(0, 10))))
        if active(*streams["v"], i, 0.2):
            inp.waist.append((d, [round(rnd.uniform(65, 90), 1) for _ in range(rnd.randint(1, 3))]))
    # attributes: some known from the start, some only later, some changing over time
    attr_day = lambda: START + timedelta(days=rnd.choice([0, rnd.randint(0, n - 1)]))  # noqa: E731
    inp.attr_history += [
        (attr_day(), "height_cm", 165.0),
        (attr_day(), "sex_for_formulas", rnd.choice(["male", "female"])),
        (attr_day(), "age_years", float(rnd.randint(20, 60))),
    ]
    for _ in range(rnd.randint(0, 3)):
        inp.attr_history.append((attr_day(), "activity_factor", rnd.choice([1.3, 1.5, 1.7])))
    return inp


@st.composite
def inputs(draw) -> tuple[engine.Inputs, date]:
    n = draw(st.integers(min_value=14, max_value=70))
    inp = build(random.Random(draw(st.integers(0, 10**6))), n)
    return inp, START + timedelta(days=draw(st.integers(min_value=6, max_value=n - 2)))


def truncate(inp: engine.Inputs, cut: date) -> engine.Inputs:
    """The same world as seen on `cut`: everything dated after it removed."""
    return replace(
        inp,
        weighins=[x for x in inp.weighins if x[0] <= cut],
        waist=[x for x in inp.waist if x[0] <= cut],
        nutrition=[x for x in inp.nutrition if x.day <= cut],
        sets=[x for x in inp.sets if x.day <= cut],
        runs=[x for x in inp.runs if x.day <= cut],
        steps={d: v for d, v in inp.steps.items() if d <= cut},
        context=[x for x in inp.context if x[0] <= cut],
        attr_history=[x for x in inp.attr_history if x[0] <= cut],
        dates=[d for d in inp.dates if d <= cut],
    )


def comparable(m):
    r = lambda x: None if x is None else round(x, 9)  # noqa: E731
    return (r(m.value), r(m.lo), r(m.hi), m.n_obs, r(m.dq), m.unit, m.epistemic)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(inputs())
def test_every_metric_ignores_data_after_its_period(case):
    inp, cut = case
    full = engine.compute(inp, START, max(inp.dates))
    seen = engine.compute(truncate(inp, cut), START, cut)
    full_by_key = {m.key(): m for m in full if m.period_end <= cut}
    seen_by_key = {m.key(): m for m in seen if m.period_end <= cut}
    assert full_by_key.keys() == seen_by_key.keys()
    for key, m in full_by_key.items():
        assert comparable(m) == comparable(seen_by_key[key]), f"look-ahead in {key}"
    assert {m.metric_id for m in full}  # the property is checked on the engine's real output


def test_generic_test_covers_every_metric_kind():
    """Guard: if the engine grows a metric that the generator never produces, extend the generator."""
    produced: set[str] = set()
    for seed in range(20):
        inp = build(random.Random(seed), 60)
        produced |= {m.metric_id for m in engine.compute(inp, START, max(inp.dates))}
    expected = {
        "weight_daily",
        "weight_ema",
        "weight_ma7",
        "weight_rate_14d",
        "weight_rate_pct_14d",
        "adaptive_tdee",
        "intake_mean_7d",
        "protein_mean_7d",
        "hard_sets",
        "volume_per_muscle_week",
        "e1rm_best_week",
        "run_volume_km",
        "steps_mean_week",
        "context_mean_week",
        "waist_session",
        "dq_score",
    }
    assert expected <= produced, expected - produced


# ------------------------------------------------------------------ knowledge cutoff (database, bitemporal)
@pytest.mark.parametrize("seed", range(5))
def test_records_recorded_after_cutoff_have_no_influence(tmp_path, seed):
    rnd = random.Random(seed)
    cutoff = datetime(2025, 3, 20, tzinfo=UTC)
    recs = syn.athlete() + syn.linear_weights(28) + syn.constant_intake(28) + syn.strength_session(START)
    for r in recs:  # each record becomes known at a random instant
        r["recorded_at"] = cutoff + timedelta(hours=rnd.randint(-400, 400))
    a, b = connect(tmp_path / "a.db"), connect(tmp_path / "b.db")
    ingest(a, recs, "s")
    ingest(b, [r for r in recs if r["recorded_at"] <= cutoff], "s")
    end = START + timedelta(days=27)
    va = engine.compute(engine.load_inputs(a, cutoff), START, end)
    vb = engine.compute(engine.load_inputs(b, cutoff), START, end)
    assert engine.digest(va) == engine.digest(vb)


# ------------------------------------------------------------------ safety, interventions, review, retrospective
def _pair(tmp_path, base, later):
    a, b = connect(tmp_path / "a.db"), connect(tmp_path / "b.db")
    ingest(a, base, "s")
    ingest(b, base + later, "s")
    return a, b


def test_safety_flags_ignore_later_data(tmp_path):
    on = START + timedelta(days=13)
    base = syn.athlete() + syn.linear_weights(14) + syn.constant_intake(14, kcal=1100)
    later = syn.linear_weights(14, START + timedelta(days=14), 50.0, -0.5) + [
        health(START + timedelta(days=20), kind="symptom", red_flags=["chest_pain"])
    ]
    a, b = _pair(tmp_path, base, later)
    fa = [(f.rule_id, f.tier, f.message) for f in safety.evaluate(a, on, persist=False)]
    fb = [(f.rule_id, f.tier, f.message) for f in safety.evaluate(b, on, persist=False)]
    assert fa == fb


def test_intervention_evaluation_ignores_later_data(tmp_path):
    t0 = datetime(2025, 3, 3, 8, 0, tzinfo=UTC)
    base = syn.athlete() + syn.linear_weights(29) + syn.constant_intake(29)
    later = syn.linear_weights(20, START + timedelta(days=29), 40.0, 0.0)
    out = []
    for conn in _pair(tmp_path, base, later):
        iid = reg.propose(conn, "Fase", "phase_start_fat_loss", prereg(), at=t0)
        reg.approve(conn, iid, "approvo", "r", "moderate", at=t0)
        res = reg.evaluate(conn, iid, START + timedelta(days=21), at=t0 + timedelta(days=21))
        out.append({k: res[k] for k in ("actual", "adherence", "confounders", "conclusion")})
    assert out[0] == out[1]


def test_weekly_review_ignores_later_data(tmp_path):
    ws = START + timedelta(days=14)
    base = syn.athlete() + syn.linear_weights(21) + syn.constant_intake(21)
    later = syn.linear_weights(14, START + timedelta(days=21), 90.0, 1.0)
    a, b = _pair(tmp_path, base, later)
    md = [
        review.render(engine.compute(engine.load_inputs(c), ws - timedelta(days=28), ws + timedelta(days=6)), ws)
        for c in (a, b)
    ]
    assert md[0] == md[1]


def test_monthly_intervention_status_is_as_of_month_end(tmp_path):
    conn = connect(tmp_path / "m.db")
    ingest(conn, syn.athlete() + syn.linear_weights(29) + syn.constant_intake(29), "s")
    t0 = datetime(2025, 3, 3, 8, 0, tzinfo=UTC)
    iid = reg.propose(conn, "Fase", "phase_start_fat_loss", prereg(), at=t0)
    reg.approve(conn, iid, "approvo", "r", "moderate", at=t0)
    reg.evaluate(conn, iid, START + timedelta(days=28), at=datetime(2025, 4, 2, tzinfo=UTC))  # concluded in April
    march = f3.intervention_status_as_of(conn, date(2025, 3, 31))
    assert march[0]["status"] == "activated"  # not "concluded": that happened later
    assert f3.intervention_status_as_of(conn, date(2025, 4, 30))[0]["status"] == "concluded"
