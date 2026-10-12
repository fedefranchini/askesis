"""Illness as a declared confounder: no look-ahead on episodes, planned sessions excused, wellbeing baselines clean,
session comment and weekly review say so and still validate. Synthetic data only."""

from datetime import UTC, date, datetime, timedelta

import pytest
import synthetic as syn
from test_adherence import plans as adh_plans
from test_session import MON as S_MON
from test_session import _session
from test_weekly_review import VELOCITY, WS3, _phase, _render
from test_wellbeing import calm

from askesis.analytics import adherence, engine, illness, session_comment, wellbeing
from askesis.core.ids import new_id
from askesis.ingestion.pipeline import ingest
from askesis.store.db import connect
from askesis.validation import textcheck

D = date(2025, 3, 3)  # a Monday


def ill_record(start: date, recorded: datetime, supersedes: str | None = None, **payload) -> dict:
    rec = dict(id=new_id(), entity_type="health_event", occurred_at=recorded, tz=syn.ZONE, local_date=start,
               recorded_at=recorded, source_id="synthetic", payload={"kind": "illness", **payload})
    if supersedes:
        rec["supersedes_id"] = supersedes
    return rec


def at(y, m, d, h=9):
    return datetime(y, m, d, h, 0, tzinfo=UTC)


def checked(conn, md):
    res = textcheck.validate(md, conn)
    assert res.ok, [i.render() for i in res.issues]
    return md


# ------------------------------------------------------------------ (a) no look-ahead
def test_episode_recorded_after_the_cutoff_is_invisible(conn):
    ingest(conn, syn.athlete() + syn.linear_weights(14), "synthetic")
    ingest(conn, [ill_record(D + timedelta(days=3), at(2025, 6, 1), description="x")], "synthetic")
    assert engine.load_inputs(conn, at(2025, 5, 1)).illness == []
    eps = engine.load_inputs(conn, at(2025, 6, 2)).illness
    assert [(e.start, e.end) for e in eps] == [(D + timedelta(days=3), None)]


def test_resolution_recorded_later_does_not_change_an_earlier_run(conn):
    ingest(conn, syn.athlete() + syn.linear_weights(14), "synthetic")
    first = ill_record(D + timedelta(days=3), at(2025, 6, 1), description="x")
    ingest(conn, [first], "synthetic")
    cutoff = at(2025, 6, 5)
    _, _, before = engine.run(conn, D, D + timedelta(days=13), cutoff=cutoff)
    fp = engine.load_inputs(conn, cutoff).fingerprint
    end = D + timedelta(days=4)
    assert not ingest(conn, [ill_record(first["local_date"], at(2025, 6, 10), first["id"], end=end.isoformat())],
                      "synthetic").rejected
    _, _, after = engine.run(conn, D, D + timedelta(days=13), cutoff=cutoff)
    assert after == before and engine.load_inputs(conn, cutoff).fingerprint == fp
    assert engine.load_inputs(conn, cutoff).illness[0].end is None  # still open as it was known then
    assert engine.load_inputs(conn, at(2025, 6, 11)).illness[0].end == end  # visible from its own recorded_at


# ------------------------------------------------------------------ (b) sessions excused
def test_planned_sessions_on_ill_days_are_excused_not_missed():
    ws, we = D, D + timedelta(days=6)
    plain = adherence.sessions_vs_plan([], adh_plans(), ws, we)
    assert plain.detail == {"planned": 3}  # no illness: identical to before
    ill = adherence.sessions_vs_plan([], adh_plans(), ws, we, ill_days={D, D + timedelta(days=2)})
    assert ill.detail == {"planned": 1, "excused_ill": 2} and ill.value == 0


def test_engine_excuses_planned_sessions_of_declared_episode(conn):
    ingest(conn, syn.athlete() + syn.linear_weights(14), "synthetic")
    ingest(conn, [ill_record(D, at(2025, 3, 3), end=(D + timedelta(days=1)).isoformat())], "synthetic")
    inp = engine.load_inputs(conn)
    inp.plans = adh_plans()
    vals = engine.compute(inp, D, D + timedelta(days=6))
    sv = next(m for m in vals if m.metric_id == "sessions_vs_plan_week")
    assert sv.detail["excused_ill"] == 1 and sv.detail["planned"] == 2
    wk = next(m for m in vals if m.metric_id == "illness_days_week")
    assert wk.value == 2 and wk.detail["days"] == [D.isoformat(), (D + timedelta(days=1)).isoformat()]


# ------------------------------------------------------------------ (c) wellbeing baseline
def test_ill_days_do_not_shift_the_baseline_or_the_links():
    morning = calm(20)
    ill_days = {morning[5][0], morning[6][0], morning[7][0]}
    extreme = [(d, pl | {"sleep_quality_1_10": 1, "fatigue_1_10": 10, "stress_1_10": 10, "soreness_1_10": 10})
               if d in ill_days else (d, pl) for d, pl in morning]
    last = morning[-1][0]

    def idx_last(m, ill):
        v = [x for x in wellbeing.daily(m, {}, [], [], last, last, ill=ill) if x.metric_id == "wellbeing_index"]
        return v[0]

    clean = idx_last(morning, set())
    polluted = idx_last(extreme, set())
    shielded = idx_last(extreme, ill_days)
    assert polluted.detail["mean"] != clean.detail["mean"]  # without the flag the baseline is distorted
    assert shielded.detail["mean"] != polluted.detail["mean"]
    assert shielded.detail["n"] == 16
    # same baseline as the answers on the remaining days only
    only = [(d, pl) for d, pl in morning if d not in ill_days]
    assert shielded.detail["mean"] == pytest.approx(idx_last(only, set()).detail["mean"])
    # an ill day carries the flag, a healthy one does not
    day = sorted(ill_days)[0]
    vals = wellbeing.daily(extreme, {}, [], [], day, last, ill=ill_days)
    assert next(v for v in vals if v.metric_id == "wellbeing_index" and v.period_start == day).detail["ill"] is True
    assert "ill" not in next(v for v in vals if v.metric_id == "wellbeing_index" and v.period_start == last).detail


def test_link_pairs_touching_an_ill_day_are_dropped():
    morning = calm(10)
    quality = {d: 5.0 + i % 3 for i, (d, _) in enumerate(morning)}
    link = wellbeing.LINKS[0]
    full = wellbeing.link_pairs(link, morning, [], [], quality, morning[-1][0])
    cut = wellbeing.link_pairs(link, morning, [], [], quality, morning[-1][0], {morning[2][0], morning[3][0]})
    assert len(full) == 10 and len(cut) == 8


# ------------------------------------------------------------------ (d) session comment
@pytest.fixture
def sconn(tmp_path):
    c = connect(tmp_path / "s.db")
    recs = syn.athlete()
    for w, (load, reps) in enumerate([(60, 8), (60, 8), (60, 9), (60, 8), (60, 9)]):
        recs += _session(S_MON + timedelta(weeks=w), [("bench_press", load, reps, 2), ("back_squat", 80, 6, 2)])
    ingest(c, recs, "synthetic")
    return c


def _comment(c, day):
    _, values, _ = engine.run(c, day, day)
    return session_comment.render(c, values, day)


def test_session_comment_notes_a_recent_illness_and_validates(sconn):
    day = S_MON + timedelta(weeks=5)
    ingest(sconn, _session(day, [("bench_press", 62.5, 9, 2), ("back_squat", 80, 6, 2)]), "synthetic")
    assert session_comment.ILLNESS_NOTE not in _comment(sconn, day)
    end = day - timedelta(days=2)  # ended two days before: within the 3 days after
    ingest(sconn, [ill_record(end - timedelta(days=2), at(2025, 4, 1), end=end.isoformat())], "synthetic")
    md = _comment(sconn, day)
    assert session_comment.ILLNESS_NOTE in md and "meno affidabile" in md
    checked(sconn, md)


def test_session_comment_no_note_when_the_illness_ended_long_before(sconn):
    day = S_MON + timedelta(weeks=5)
    ingest(sconn, _session(day, [("bench_press", 62.5, 9, 2)]), "synthetic")
    end = day - timedelta(days=6)
    ingest(sconn, [ill_record(end - timedelta(days=1), at(2025, 4, 1), end=end.isoformat())], "synthetic")
    assert session_comment.ILLNESS_NOTE not in _comment(sconn, day)


# ------------------------------------------------------------------ (e) weekly review
def test_weekly_review_mentions_the_illness_once_and_validates(conn):
    recs = (syn.athlete() + syn.linear_weights(35) + syn.constant_intake(35) + syn.strength_session(syn.START)
            + syn.strength_session(syn.START + timedelta(days=7)))
    assert not ingest(conn, recs, "synthetic").rejected
    _phase(conn, VELOCITY)
    _, _, plain = _render(conn, WS3)
    assert "malattia dichiarata" not in plain
    start = WS3 + timedelta(days=1)
    ingest(conn, [ill_record(start, at(2025, 3, 20), end=(WS3 + timedelta(days=2)).isoformat())], "synthetic")
    _, _, md = _render(conn, WS3)
    checked(conn, md)
    assert md.count("malattia dichiarata") == 1
    stop = start + timedelta(days=1)
    assert f"dal {start.day}/{start.month} al {stop.day}/{stop.month}" in md
    assert "non contano come saltate" in md and "meno affidabili" in md and "`illness_days_week@1`" in md


def test_episode_helpers_match_the_engine_input():
    e = illness.from_record("r", "2025-03-03", {"kind": "illness", "end": "2025-03-04"})
    assert illness.ill_days([e], date(2025, 3, 31)) == {D, D + timedelta(days=1)}


def test_resting_hr_and_watch_sleep_baselines_skip_ill_days():
    """A fever week with a high resting heart rate and short nights must not raise the usual values: after the
    illness, an ordinary day is judged against the healthy days only."""
    from askesis.analytics import wellbeing

    start = date(2099, 1, 1)
    healthy = {start + timedelta(days=i): 300.0 + (i % 3) for i in range(30)}  # synthetic, far from real values
    ill = {start + timedelta(days=30 + i) for i in range(7)}
    rhr = healthy | {d: 390.0 for d in ill}
    sleep = {d: 20.0 + (d.toordinal() % 2) for d in healthy} | {d: 2.0 for d in ill}
    day = start + timedelta(days=37)
    rhr[day], sleep[day] = 330.0, 14.0  # clearly worse than the healthy days
    with_ill = wellbeing.objective_signals(day, rhr, sleep, [], frozenset(ill))
    assert "rhr" in with_ill and "sleep_watch" in with_ill
    healthy_only = {d: v for d, v in rhr.items() if d not in ill}
    n, mean, sd = wellbeing.baseline(healthy_only, day)
    assert mean is not None and mean < 302  # the ill week did not move the usual value
