"""Session comment: per-session e1RM vs the previous session against the noise, records, one point of attention."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import synthetic as syn

from askesis import coach
from askesis.analytics import engine, session_comment
from askesis.analytics.params import p
from askesis.analytics.session import session_metrics
from askesis.analytics.training import SetRow
from askesis.core.ids import new_id
from askesis.ingestion.pipeline import ingest
from askesis.store.db import connect
from askesis.validation import textcheck

MON = syn.START


def row(day, load, reps, rir=2, ex="bench_press", kind="external", st="working"):
    return SetRow(day, f"s{day}", ex, ex, st, load, kind, reps, rir, None)


def sessions(loads_reps: list[tuple[float, int]], ex="bench_press"):
    return [row(MON + timedelta(weeks=i), load, reps, ex=ex) for i, (load, reps) in enumerate(loads_reps)]


def by(values, metric, day=None):
    return {m.subject: m for m in values if m.metric_id == metric and (day is None or m.period_start == day)}


def test_first_session_is_the_baseline_not_a_record():
    v = session_metrics(sessions([(50, 8)]), MON, MON)
    assert set(by(v, "e1rm_session")) == {"exercise:bench_press"}
    assert not by(v, "e1rm_session_change") and not by(v, "strength_record")


def test_change_against_the_noise_of_past_sessions():
    hist = [(50, 8), (50, 8), (50, 9), (50, 8), (50, 9), (52.5, 9)]
    v = session_metrics(sessions(hist), MON, MON + timedelta(weeks=10))
    last = MON + timedelta(weeks=5)
    ch = by(v, "e1rm_session_change", last)["exercise:bench_press"]
    assert ch.detail["noise_points"] == 4 and "minimal_difference_kg" in ch.detail
    early = by(v, "e1rm_session_change", MON + timedelta(weeks=2))["exercise:bench_press"]
    assert early.detail["reason"] == "noise_not_estimable"  # fewer than noise_min_points past changes
    assert p("noise_min_points") == 4
    recs = by(v, "strength_record", last)
    load = recs["exercise:bench_press:load"]
    assert load.value == 52.5 and load.detail["previous"] == 50
    assert "exercise:bench_press:e1rm" in recs
    assert by(v, "strength_record", MON + timedelta(weeks=2))["exercise:bench_press:reps@50"].value == 9


def test_no_look_ahead():
    hist = [(50, 8), (50, 8), (50, 9), (50, 8), (50, 9), (52.5, 9), (55, 6)]
    day = MON + timedelta(weeks=5)
    full = [m for m in session_metrics(sessions(hist), day, day)]
    cut = [m for m in session_metrics(sessions(hist[:6]), day, day)]
    assert [(m.metric_id, m.subject, m.value, m.lo, m.hi) for m in full] == \
           [(m.metric_id, m.subject, m.value, m.lo, m.hi) for m in cut]


def test_bodyweight_exercises_use_reps():
    s = [row(MON, 0, 6, ex="pull_up", kind="bodyweight"), row(MON + timedelta(weeks=1), 0, 8, ex="pull_up",
                                                               kind="bodyweight")]
    v = session_metrics(s, MON, MON + timedelta(weeks=1))
    assert by(v, "best_reps_session", MON + timedelta(weeks=1))["exercise:pull_up"].value == 8
    assert by(v, "strength_record", MON + timedelta(weeks=1))["exercise:pull_up:reps@0"].value == 9 - 1


def test_too_many_reps_for_an_e1rm_compares_reps_at_the_same_load(tmp_path):
    c = connect(tmp_path / "r.db")
    recs = syn.athlete() + _session(MON, [("leg_extension", 30, 12, 2)])
    recs += _session(MON + timedelta(weeks=1), [("leg_extension", 30, 13, 2)])
    ingest(c, recs, "synthetic")
    md = comment(c, MON + timedelta(weeks=1))
    assert "**Leg extension**: più ripetizioni. 13 ripetizioni con 30 kg contro 12 del 3/3" in md
    assert "13 ripetizioni con 30 kg, mai così tante a questo carico (prima 12)" in md
    assert textcheck.validate(md, c).ok
    assert "nessuna seduta precedente allo stesso carico" in comment(c, MON)


def test_lower_bound_e1rm_is_not_compared_as_a_change():
    s = [row(MON, 50, 8), row(MON + timedelta(weeks=1), 55, 6, rir=None)]
    v = session_metrics(s, MON, MON + timedelta(weeks=1))
    assert by(v, "e1rm_session_change")["exercise:bench_press"].detail["lower_bound"] is True
    assert "exercise:bench_press:e1rm" not in by(v, "strength_record")  # a lower bound never sets an e1RM record


# ------------------------------------------------------------------ rendered comment, validated
def _session(day: date, sets: list[tuple[str, float, int, float | None]], pain: float | None = None) -> list[dict]:
    t = datetime(day.year, day.month, day.day, 17, 0, tzinfo=UTC)
    sess = syn._env("training_session", day, {"session_kind": "strength"}, start_at=t)
    out = [sess]
    for i, (ex, load, reps, rir) in enumerate(sets, 1):
        pl = {"session_id": sess["id"], "exercise_raw": ex, "exercise_id": ex, "sequence": i, "set_type": "working",
              "load_kg": load, "reps": reps, "load_kind": "external"}
        if rir is not None:
            pl["rir"] = rir
        if pain is not None and i == 1:
            pl |= {"pain": pain, "pain_region": "spalla"}
        rec = syn._env("set_record", day, pl, start_at=t)
        rec["id"] = new_id()
        out.append(rec)
    return out


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "s.db")
    recs = syn.athlete()
    for w, (load, reps) in enumerate([(60, 8), (60, 8), (60, 9), (60, 8), (60, 9)]):
        recs += _session(MON + timedelta(weeks=w), [("bench_press", load, reps, 2), ("bench_press", load, reps - 1, 2),
                                                    ("back_squat", 80, 6, 2)])
    ingest(c, recs, "synthetic")
    return c


def comment(c, day):
    _, values, _ = engine.run(c, day, day)
    return session_comment.render(c, values, day)


def test_comment_cites_metrics_and_passes_the_validator(conn):
    day = MON + timedelta(weeks=5)
    ingest(conn, _session(day, [("bench_press", 62.5, 9, 2), ("bench_press", 62.5, 8, 1), ("back_squat", 80, 6, 2)]),
           "synthetic")
    md = comment(conn, day)
    assert "**Panca piana**" in md and "`e1rm_session_change@1`" in md
    assert "carico più alto di sempre, 62,5 kg (prima 60 kg)" in md
    assert "**Squat**: stabile" in md  # same set as the previous sessions: inside the noise
    assert md.index("Panca piana") < md.index("Squat")  # exercise order as recorded
    assert "## Punto di attenzione\n\n- nessuno" in md
    res = textcheck.validate(md, conn)
    assert res.ok, [i.render() for i in res.issues]


def test_first_session_and_pain_attention(tmp_path):
    c = connect(tmp_path / "f.db")
    ingest(c, syn.athlete() + _session(MON, [("bench_press", 40, 10, 2)], pain=p("safety_pain_threshold")),
           "synthetic")
    md = comment(c, MON)
    assert "prima seduta registrata" in md and "Nessun record" in md
    assert "dolore registrato durante Panca piana [record:" in md
    assert textcheck.validate(md, c).ok
    assert comment(c, MON + timedelta(days=1)) is None  # no sets that day: no comment


def test_coach_lists_session_comments(tmp_path):
    (tmp_path / "seduta-2025-03-03.md").write_text("# Commento seduta — 2025-03-03\n\n- Nessun record.\n")
    [doc] = coach.documents(tmp_path, None)
    assert doc.kind == "seduta" and doc.label == "Commento seduta 2025-03-03" and doc.validated
