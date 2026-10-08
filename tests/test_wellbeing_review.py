"""The questionnaire in the weekly review: what is missing before the thresholds, the convergence area, the lagged
links as validated hypotheses. Synthetic data only."""

from datetime import timedelta

import pytest
import synthetic as syn
from test_weekly_review import VELOCITY, WS3, _phase, _render

from askesis.ingestion.pipeline import ingest
from askesis.validation import textcheck

AREA = "Benessere (questionario)"
CALM = {"moment": "morning", "sleep_quality_1_10": 7, "fatigue_1_10": 3, "soreness_1_10": 2, "stress_1_10": 3,
        "mood_1_10": 7, "hunger_1_10": 5, "motivation_1_10": 7}


@pytest.fixture
def db(conn):
    recs = (syn.athlete() + syn.linear_weights(35) + syn.constant_intake(35) + syn.strength_session(syn.START)
            + syn.strength_session(syn.START + timedelta(days=7)))
    assert not ingest(conn, recs, "synthetic").rejected
    _phase(conn, VELOCITY)
    return conn


def morning(day, **change):
    return syn._env("subjective_checkin", day, CALM | change)


def checked(db, md):
    res = textcheck.validate(md, db)
    assert res.ok, [i.render() for i in res.issues]
    return md


def test_no_answers_says_so(db):
    _, _, md = _render(db, WS3)
    checked(db, md)
    assert f"| {AREA} | ⚪ non valutabile | nessuna risposta |" in md


def test_baseline_in_progress_says_how_many_are_missing(db):
    start = WS3 - timedelta(days=3)
    assert not ingest(db, [morning(start + timedelta(days=i)) for i in range(10)], "synthetic").rejected
    _, _, md = _render(db, WS3)
    checked(db, md)
    assert f"| {AREA} | ⚪ non valutabile | baseline in costruzione |" in md
    assert "ne mancano" in md
    assert "Collegamenti nel tempo" in md and "Non ancora calcolati" in md and "mancano 7" in md


def test_convergence_turns_the_area_yellow_without_changing_the_plan(db):
    first = WS3 - timedelta(days=20)
    recs = []
    for i in range(20):
        d = first + timedelta(days=i)
        recs.append(morning(d, sleep_quality_1_10=7 - i % 2, mood_1_10=7 - i % 2))
        recs.append(syn._env("resting_hr_daily", d, {"bpm": 50 + i % 2}))
    for i in range(3):
        d = WS3 + timedelta(days=i)
        recs.append(morning(d, sleep_quality_1_10=2, fatigue_1_10=9, mood_1_10=2))
        recs.append(syn._env("resting_hr_daily", d, {"bpm": 70}))
    assert not ingest(db, recs, "synthetic").rejected
    _, _, md = _render(db, WS3)
    checked(db, md)
    assert f"| {AREA} | 🟡 giallo | segnali concordi |" in md
    assert "FC a riposo più alta" in md and "proposta da approvare" in md


def test_links_appear_as_hypotheses_with_interval_after_the_thresholds(db):
    first = WS3 - timedelta(days=80)
    recs = []
    for i in range(87):
        d = first + timedelta(days=i)
        s = 3 + (i * 7) % 6
        recs.append(morning(d, sleep_quality_1_10=s, stress_1_10=9 - s))
        if i % 2 == 0:
            recs.append(syn._env("subjective_checkin", d, {"moment": "post_session", "session_kind": "run",
                                                          "session_quality_1_10": min(10, s + i % 4 // 2)}))
    assert not ingest(db, recs, "synthetic").rejected
    _, _, md = _render(db, WS3)
    checked(db, md)
    section = md[md.index("## Collegamenti nel tempo"):]
    assert "sonno percepito (mattina) → qualità della seduta dello stesso giorno: rho" in section
    assert "IC 90%" in section and "ipotesi di collegamento positivo" in section
    assert "non dice quale delle due cose causa l'altra" in section
