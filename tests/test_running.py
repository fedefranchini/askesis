"""Aerobic running efficiency: comparable easy runs, EF, change vs noise, pace at equal HR, no look-ahead."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
import synthetic as syn
from test_weekly_review import PHASE_START, WS3, _phase, _render  # noqa: F401

from askesis.analytics import engine, running, trends
from askesis.analytics.params import p
from askesis.analytics.training import RunRow
from askesis.ingestion.pipeline import ingest
from askesis.plan import store
from askesis.store.db import connect
from askesis.validation import textcheck

MON = syn.START  # a Monday
FAR = date(2030, 1, 1)


def run(day, hr=105, dist=6000.0, secs=1800, **kw):
    kw.setdefault("run_type", "easy")
    return RunRow(day, dist, secs, hr, **kw)


def eff(runs, plans=(), start=MON, end=FAR):
    return running.efficiency(list(runs), list(plans), start, end)


def of(vals, metric, subject="env:outdoor"):
    return [m for m in vals if m.metric_id == metric and m.subject == subject]


def series(n, step=1, base=6000.0, hr=105):
    """n easy runs, EF slowly drifting (distance changes by a few metres)."""
    return [run(MON + timedelta(days=step * i), hr=hr, dist=base + 40 * ((i * 7) % 5)) for i in range(n)]


MIN = {"day": "wed", "name": "m", "lifts": [{"exercise": "bench_press", "sets": 3, "rep_range": [6, 8],
                                             "target_rir": 2}]}


def prog(kind="easy", day="mon"):
    micro = [{"day": day, "name": "x", "run": {"kind": kind}}]
    return (MON, "2025-01-01T00:00:00Z", "programme", {"microcycle": micro})


# ------------------------------------------------------------------ comparability
def test_declared_easy_is_comparable_and_other_types_never():
    assert len(of(eff([run(MON)]), "run_efficiency")) == 1
    for t in ("long", "interval", "tempo", "race"):
        assert not eff([run(MON, run_type=t)], [prog()])  # even when the plan says easy that day


def test_plan_easy_when_type_is_missing():
    m = of(eff([run(MON, run_type=None)], [prog()]), "run_efficiency")
    assert len(m) == 1 and m[0].detail["easy_source"] == "plan"
    assert of(eff([run(MON)]), "run_efficiency")[0].detail["easy_source"] == "declared"
    assert not eff([run(MON, run_type=None)])  # no programme
    assert not eff([run(MON, run_type=None)], [prog(day="tue")])  # other weekday
    assert not eff([run(MON, run_type=None)], [prog(kind="long")])  # not an easy run in the plan
    assert not eff([run(MON, run_type=None)], [(MON + timedelta(days=1), "x", "programme", prog()[3])])  # not yet valid


def test_needs_heart_rate_and_minimum_duration():
    assert not eff([run(MON, hr=None)])
    short = int(p("run_eff_min_minutes") * 60)
    assert eff([run(MON, secs=short)]) and not eff([run(MON, secs=short - 1)])
    assert eff([run(MON, secs=short + 600, moving_s=short)])  # moving time wins over elapsed
    assert not eff([run(MON, secs=short + 600, moving_s=short - 60)])


def test_elevation_limit_and_unknown_allowed():
    lim = p("run_eff_max_elev_m_per_km")
    assert not eff([run(MON, elev_gain_m=lim * 6.0 + 1)])
    ok = of(eff([run(MON, elev_gain_m=lim * 6.0)]), "run_efficiency")[0]
    assert ok.detail["elev_known"] is True
    assert of(eff([run(MON)]), "run_efficiency")[0].detail["elev_known"] is False


def test_environments_are_separated():
    runs = [run(MON), run(MON + timedelta(days=1), environment="treadmill"),
            run(MON + timedelta(days=2), environment="outdoor")]
    v = eff(runs)
    assert len(of(v, "run_efficiency", "env:outdoor")) == 2 and len(of(v, "run_efficiency", "env:treadmill")) == 1


# ------------------------------------------------------------------ values
def test_efficiency_value_and_detail():
    m = of(eff([run(MON, hr=105, dist=6000, secs=1800, moving_s=1500)]), "run_efficiency")[0]
    assert m.value == pytest.approx(240 / 105) and m.unit == "m/beat" and m.n_obs == 1
    assert m.detail["speed_m_min"] == 240 and m.detail["avg_hr"] == 105 and m.detail["pace_s_per_km"] == 250
    assert m.period_start == m.period_end == MON and m.epistemic == "ESTIMATE"


def test_only_runs_inside_the_period_are_emitted_but_history_is_used():
    runs = series(8)
    v = eff(runs, start=runs[-1].day, end=runs[-1].day)
    assert len(of(v, "run_efficiency")) == 1 and len(of(v, "run_efficiency_change")) == 1


def test_no_change_with_fewer_than_two_windows():
    k = p("run_eff_window")
    v = eff(series(2 * k - 1))
    assert len(of(v, "run_efficiency")) == 2 * k - 1
    assert not of(v, "run_efficiency_change") and not of(v, "run_pace_at_ref_hr")


def test_change_with_interval_and_pace_at_reference_hr():
    k, mn = p("run_eff_window"), p("run_eff_min_noise_runs")
    runs = series(k + mn + k)  # baseline of k+mn runs before the last window
    ef = [r.distance_m / (r.elapsed_s / 60) / r.avg_hr for r in runs]
    ch = of(eff(runs), "run_efficiency_change")[-1]
    diff = np.mean(ef[-k:]) - np.mean(ef[-2 * k:-k])
    sd = np.std(ef[:-k], ddof=1)
    md = p("minimal_difference_z") * math.sqrt(2) * sd / math.sqrt(k)
    assert ch.value == pytest.approx(diff) and ch.lo == pytest.approx(diff - md) and ch.hi == pytest.approx(diff + md)
    assert ch.detail["sd"] == pytest.approx(sd) and ch.detail["minimal_difference"] == pytest.approx(md)
    assert ch.detail["runs_used"] == len(runs) and ch.detail["window"] == k and ch.detail["ref_hr"] == 105
    pace = of(eff(runs), "run_pace_at_ref_hr")[-1]
    assert pace.value == pytest.approx(60000 / (np.mean(ef[-k:]) * 105)) and pace.unit == "s/km"
    assert pace.detail["previous_s_km"] == pytest.approx(60000 / (np.mean(ef[-2 * k:-k]) * 105))
    assert ch.detail["pace_at_ref_now_s_km"] == pytest.approx(pace.value)


def test_reference_hr_is_the_median_of_comparable_runs():
    runs = [run(MON + timedelta(days=i), hr=h) for i, h in enumerate([100, 110, 180, 105, 110, 100])]
    pace = of(eff(runs), "run_pace_at_ref_hr")[-1]
    assert pace.detail["ref_hr"] == 107.5


def test_noise_not_estimable_with_few_baseline_runs():
    k, mn = p("run_eff_window"), p("run_eff_min_noise_runs")
    ch = of(eff(series(2 * k)), "run_efficiency_change")[-1]
    assert k < mn and ch.value is not None and ch.lo is None and ch.hi is None
    assert ch.detail["reason"] == "noise_not_estimable"
    assert running.beyond_noise(ch) is None


def test_faster_at_equal_hr_is_better():
    k, mn = p("run_eff_window"), p("run_eff_min_noise_runs")
    old = [run(MON + timedelta(days=i), dist=6000 + 20 * (i % 3)) for i in range(k + mn + k)]
    new = [run(MON + timedelta(days=100 + i), dist=7000 + 20 * i) for i in range(k)]
    ch = of(eff(old + new), "run_efficiency_change")[-1]
    assert ch.lo > 0 and running.beyond_noise(ch) == "better"
    slow = [run(MON + timedelta(days=100 + i), dist=5000 + 20 * i) for i in range(k)]
    assert running.beyond_noise(of(eff(old + slow), "run_efficiency_change")[-1]) == "worse"


def test_mmss():
    assert running.mmss(331.4) == "5:31" and running.mmss(299.7) == "5:00"


# ------------------------------------------------------------------ no look-ahead
def test_a_later_run_never_changes_earlier_metrics():
    runs = series(12)
    base = eff(runs[:9])
    more = eff(runs)
    assert [m for m in more if m.period_end <= runs[8].day] == base
    shifted = [*runs[:9], run(runs[8].day + timedelta(days=30), dist=9999.0)]
    assert [m for m in eff(shifted) if m.period_end <= runs[8].day] == base


# ------------------------------------------------------------------ engine, trends, review
def _run_rec(day, dist, hr=105, secs=1800, run_type="easy", **extra):
    t = datetime(day.year, day.month, day.day, 7, 0, tzinfo=UTC)
    pl = {"distance_m": dist, "elapsed_s": secs, "avg_hr": hr, "run_type": run_type} | extra
    pl = {k: v for k, v in pl.items() if v is not None}
    return syn._env("running_session", day, pl, start_at=t, end_at=t + timedelta(seconds=secs))


def _runs(n, start=MON, step=2):
    return [_run_rec(start + timedelta(days=step * i), 6000 + 40 * ((i * 7) % 5)) for i in range(n)]


def test_engine_integration(tmp_path):
    c = connect(tmp_path / "r.db")
    recs = syn.athlete() + _runs(9) + [_run_rec(MON + timedelta(days=1), 6000, run_type="long"),
                                       _run_rec(MON + timedelta(days=3), 6000, run_type=None, environment="treadmill")]
    ingest(c, recs, "synthetic")
    _, values, _ = engine.run(c, MON, MON + timedelta(days=30))
    assert len(of(values, "run_efficiency")) == 9 and not of(values, "run_efficiency", "env:treadmill")
    assert of(values, "run_efficiency_change") and of(values, "run_pace_at_ref_hr")
    inp = engine.load_inputs(c)
    assert {r.run_type for r in inp.runs} == {"easy", "long", None}
    assert any(r.environment == "treadmill" for r in inp.runs)


def test_engine_uses_the_programme_for_untyped_runs(tmp_path):
    c = connect(tmp_path / "r.db")
    ingest(c, syn.athlete() + [_run_rec(MON, 6000, run_type=None)], "synthetic")
    _, values, _ = engine.run(c, MON, MON)
    assert not of(values, "run_efficiency")
    store.add_version(c, "programme", "p", prog()[3] | {"minimal_week": [MIN]}, MON, None)
    _, values, _ = engine.run(c, MON, MON)
    assert of(values, "run_efficiency")[0].detail["easy_source"] == "plan"


def _fact(c, day):
    r = next(x for x in trends.questions(c, day, 91) if x["key"] == "recovery")
    return r, [f for f in r["facts"] if f.startswith("efficienza aerobica")]


def test_trends_fact(tmp_path):
    c = connect(tmp_path / "t.db")
    ingest(c, syn.athlete() + syn.linear_weights(3), "synthetic")
    assert _fact(c, MON + timedelta(days=60))[1] == []  # no comparable run: nothing
    ingest(c, _runs(4), "synthetic")
    r, f = _fact(c, MON + timedelta(days=60))
    assert len(f) == 1 and "rumore non ancora stimabile (4 corse facili confrontabili, ne servono 7)" in f[0]
    assert "run_efficiency_change@1" in r["refs"]
    ingest(c, _runs(12), "synthetic")  # same ids? new records on the same days: still the same series shape
    r, f = _fact(c, MON + timedelta(days=60))
    k = int(p("run_eff_window"))
    assert f and "FC 105" in f[0] and f"rispetto alle {k} precedenti" in f[0] and "/km" in f[0]


def test_trends_fact_has_no_look_ahead(tmp_path):
    c = connect(tmp_path / "t.db")
    ingest(c, syn.athlete() + _runs(9), "synthetic")
    day = MON + timedelta(days=10)  # only the first 6 runs are in the past
    before = _fact(c, day)
    ingest(c, [_run_rec(MON + timedelta(days=40), 9000)], "synthetic")
    assert _fact(c, day) == before


def test_weekly_review_efficiency_line_passes_the_validator(conn):
    recs = (syn.athlete() + syn.linear_weights(35) + syn.constant_intake(35) + syn.strength_session(syn.START)
            + syn.strength_session(syn.START + timedelta(days=7)) + _runs(11))
    assert not ingest(conn, recs, "synthetic").rejected
    _phase(conn)
    _, ctx, md = _render(conn, WS3)
    line = next(x for x in md.splitlines() if x.startswith("- Efficienza aerobica"))
    assert "FC 105 bpm" in line and "/km" in line and "run_pace_at_ref_hr@1" in line
    assert "run_efficiency_change@1" in line
    res = textcheck.validate(md, conn)
    assert res.ok, [i.render() for i in res.issues]


def test_weekly_review_without_comparable_runs(conn):
    recs = (syn.athlete() + syn.linear_weights(35) + syn.constant_intake(35) + syn.strength_session(syn.START)
            + syn.strength_session(syn.START + timedelta(days=7)))
    assert not ingest(conn, recs, "synthetic").rejected
    _phase(conn)
    _, ctx, md = _render(conn, WS3)
    assert "- Efficienza aerobica: nessuna corsa facile confrontabile" in md
    assert textcheck.validate(md, conn).ok
