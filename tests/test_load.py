"""Foster session load, weekly load, monotony and strain."""

from __future__ import annotations

import math
from datetime import timedelta

import synthetic as syn

from askesis.analytics import engine
from askesis.analytics.load import load_week, session_load
from askesis.ingestion.pipeline import ingest
from askesis.store.db import connect

MON = syn.START
SUN = MON + timedelta(days=6)


def d(i):
    return MON + timedelta(days=i)


def by(values, metric):
    return {m.metric_id: m for m in values}[metric]


def week(fb, strength=(), runs=(), end=SUN):
    return load_week(fb, strength, runs, MON, end)


def test_session_load_is_rpe_times_minutes():
    fb = [(d(0), "strength", 6.0, 60), (d(1), "run", 4.0, None), (d(2), "run", None, 30), (d(9), "run", 5.0, 30)]
    [m] = session_load(fb, MON, SUN)
    assert (m.metric_id, m.subject, m.value, m.unit, m.epistemic, m.n_obs) == \
           ("session_load", "session:strength", 360.0, "AU", "ESTIMATE", 1)
    assert m.period_start == m.period_end == d(0) and m.detail == {"rpe_cr10": 6.0, "minutes": 60}


def test_weekly_sum_and_monotony_with_rest_days():
    fb = [(d(0), "strength", 5.0, 60), (d(2), "strength", 7.0, 60), (d(4), "run", 4.0, 45)]
    v = week(fb, strength=[d(0), d(2)], runs=[d(4)])
    assert by(v, "training_load_week").value == 900.0 and by(v, "training_load_week").dq == 1.0
    daily = [300, 0, 420, 0, 180, 0, 0]  # rest days count as zero
    mean = 900 / 7
    sd = math.sqrt(sum(x * x for x in daily) / 7 - mean**2)
    mono = by(v, "training_monotony_week")
    assert mono.value is not None and math.isclose(mono.value, mean / sd, rel_tol=1e-9) and mono.unit == "ratio"
    assert math.isclose(by(v, "training_strain_week").value, 900 * mean / sd, rel_tol=1e-9)
    assert by(v, "training_load_week").detail["sessions_recorded"] == 3


def test_several_sessions_on_one_day_are_summed():
    fb = [(d(0), "strength", 5.0, 60), (d(0), "run", 4.0, 30)]
    v = week(fb)
    assert by(v, "training_load_week").value == 420.0
    assert by(v, "training_monotony_week").value is not None


def test_sd_zero_gives_no_monotony():
    fb = [(d(i), "strength", 5.0, 60) for i in range(7)]
    v = week(fb)
    assert by(v, "training_load_week").value == 2100.0
    for k in ("training_monotony_week", "training_strain_week"):
        assert by(v, k).value is None and by(v, k).detail["reason"] == "sd_zero"


def test_missing_feedback_blocks_monotony_and_strain():
    fb = [(d(0), "strength", 5.0, 60)]
    v = week(fb, strength=[d(0), d(2)], runs=[d(4)])
    tl = by(v, "training_load_week")
    assert tl.value == 300.0 and tl.detail["missing_feedback"] == [f"{d(2)}:strength", f"{d(4)}:run"]
    assert tl.detail["sessions_with_load"] == 1 and tl.detail["sessions_recorded"] == 3
    assert math.isclose(tl.dq, 1 / 3)
    for k in ("training_monotony_week", "training_strain_week"):
        assert by(v, k).value is None and by(v, k).detail["reason"] == "missing_feedback"


def test_feedback_without_minutes_counts_as_recorded_but_missing():
    v = week([(d(1), "run", 5.0, None)])
    assert by(v, "training_load_week").detail["missing_feedback"] == [f"{d(1)}:run"]
    assert by(v, "training_load_week").dq == 0.0


def test_empty_week_emits_nothing():
    assert week([]) == []
    assert week([(d(8), "run", 5.0, 30)]) == []


def test_no_look_ahead():
    fb = [(d(0), "strength", 5.0, 60), (d(2), "strength", 7.0, 60)]
    later = fb + [(d(3), "run", 6.0, 40), (d(10), "run", 6.0, 40)]
    end = d(2)  # a partial week closed on Wednesday
    a = [(m.metric_id, m.value, m.dq, m.detail) for m in load_week(fb, [d(0), d(2)], [], MON, end)]
    b = [(m.metric_id, m.value, m.dq, m.detail) for m in load_week(later, [d(0), d(2), d(3)], [d(3)], MON, end)]
    assert a == b


def _checkin(day, kind, rpe, minutes):
    return syn._env("subjective_checkin", day, {"moment": "post_session", "session_kind": kind,
                                                "session_rpe_cr10": rpe, "session_minutes": minutes})


def test_engine_integration(tmp_path):
    c = connect(tmp_path / "l.db")
    recs = syn.athlete() + syn.strength_session(d(0)) + [syn.run(d(2), 5.0, 1800, 150)]
    recs += [_checkin(d(0), "strength", 6, 60), _checkin(d(2), "run", 4, 30)]
    ingest(c, recs, "synthetic")
    _, values, _ = engine.run(c, MON, SUN)
    assert by(values, "training_monotony_week").detail["reason"] == "week_in_progress"  # no data after Wednesday
    ingest(c, syn.linear_weights(1, start=d(7)), "synthetic")  # a record on the next Monday: the week is closed
    _, values, _ = engine.run(c, MON, SUN)
    loads = {m.subject: m for m in values if m.metric_id == "session_load"}
    assert loads["session:strength"].value == 360.0 and loads["session:run"].value == 120.0
    w = by(values, "training_load_week")
    assert w.value == 480.0 and w.dq == 1.0 and w.detail["missing_feedback"] == []
    assert by(values, "training_monotony_week").value is not None
    assert engine.ENGINE_VERSION == "0.7.0"


def test_week_in_progress_has_load_but_no_monotony():
    fb = [(d(0), "strength", 5.0, 60), (d(2), "strength", 7.0, 60)]
    v = load_week(fb, [d(0), d(2)], [], MON, SUN, known_until=d(3))
    assert by(v, "training_load_week").value == 720.0
    mono = by(v, "training_monotony_week")
    assert mono.value is None and mono.detail["reason"] == "week_in_progress"
    assert by(v, "training_strain_week").value is None
    assert by(load_week(fb, [d(0), d(2)], [], MON, SUN, known_until=SUN), "training_monotony_week").value
