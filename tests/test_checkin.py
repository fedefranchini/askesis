"""Daily questionnaire: anchored scales, dictation, merge into one record per day and moment, response quality."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
import synthetic as syn
from typer.testing import CliRunner

from askesis import checkin, services
from askesis.config import load
from askesis.ingestion import manual
from askesis.ingestion.pipeline import ingest
from askesis.model.entities import SubjectiveCheckin
from askesis.parsers.text import ParseError, parse_day
from askesis.store.db import connect

DAY = syn.START + timedelta(days=7)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "c.db"))
    cfg = load()
    conn = connect(cfg.db_path)
    ingest(conn, syn.athlete(), "synthetic")
    return cfg, conn


def log(cfg, conn, line, day=DAY):
    return services.save(conn, services.prepare(conn, services.build_day(line, cfg, day)), "test")


def current(conn, day=DAY):
    return [json.loads(r["payload"]) for r in conn.execute(
        "SELECT payload FROM v_current WHERE entity_type = 'subjective_checkin' AND local_date = ?",
        (day.isoformat(),))]


def test_items_are_anchored_and_have_a_declared_direction():
    assert [i.key for i in checkin.MORNING] == ["sonno", "stanchezza", "indolenzimento", "stress", "umore", "fame",
                                                "voglia"]
    assert [i.key for i in checkin.POST] == ["fatica", "qualita"]
    for it in checkin.ITEMS:
        assert it.lo in it.anchors and it.hi in it.anchors and len(it.anchors) >= 5, it.key
        assert it.direction in ("higher_better", "higher_worse", "neutral")
        assert it.field in SubjectiveCheckin.model_fields
        assert len(it.descriptions()) == it.hi - it.lo + 1
    by = checkin.BY_KEY
    assert (by["fatica"].lo, by["fatica"].hi) == (0, 10)  # CR-10
    assert all((i.lo, i.hi) == (1, 10) for i in checkin.ITEMS if i.key != "fatica")
    # every scale measures "how much": the index direction follows the item
    assert {k: by[k].direction for k in ("sonno", "umore", "voglia", "qualita")} == dict.fromkeys(
        ("sonno", "umore", "voglia", "qualita"), "higher_better")
    assert {k: by[k].direction for k in ("stanchezza", "indolenzimento", "stress")} == dict.fromkeys(
        ("stanchezza", "indolenzimento", "stress"), "higher_worse")
    assert by["sonno"].describe(1).startswith("Pessimo") and by["sonno"].describe(10).startswith("Ottimo")
    assert by["sonno"].describe(6) == "tra «Discreto» e «Buono»"


@pytest.mark.parametrize("line,expected", [
    ("checkin sonno 7 stanchezza 4 umore 6", {"sleep_quality_1_10": 7, "fatigue_1_10": 4, "mood_1_10": 6}),
    ("check-in Stanco 3 DOMS 2 motivazione 9", {"fatigue_1_10": 3, "soreness_1_10": 2, "motivation_1_10": 9}),
])
def test_morning_dictation(line, expected):
    [it] = parse_day(line)
    assert it.kind == "checkin" and it.data == expected


def test_session_dictation():
    [it] = parse_day("seduta corsa rpe 5 qualità 8 42min")
    assert it.data == {"session_kind": "run", "session_rpe_cr10": 5, "session_quality_1_10": 8, "session_minutes": 42}
    assert parse_day("seduta pesi fatica 0")[0].data["session_rpe_cr10"] == 0  # CR-10 starts at 0


@pytest.mark.parametrize("line", ["checkin sonno 0", "checkin sonno 11", "checkin sonno 7,5", "checkin sonno",
                                  "checkin fatica 5", "checkin sonno 7 sonno 6", "checkin", "seduta fatica 5",
                                  "seduta pesi umore 5", "seduta pesi", "seduta pesi fatica 11",
                                  "seduta pesi fatica 5 0min"])
def test_invalid_answers_are_refused_not_guessed(line):
    with pytest.raises(ParseError):
        parse_day(line)


def test_model_keeps_moments_apart():
    with pytest.raises(ValueError):
        SubjectiveCheckin(sleep_quality_1_10=7)  # a morning item without moment
    with pytest.raises(ValueError):
        SubjectiveCheckin(moment="morning", session_rpe_cr10=5)
    with pytest.raises(ValueError):
        SubjectiveCheckin(moment="post_session", session_rpe_cr10=5)  # session kind missing
    assert SubjectiveCheckin(pain=[{"region": "x", "score_0_10": 2}]).moment is None  # old pain records still valid


def test_answers_merge_into_one_current_record_per_day_and_moment(ctx):
    cfg, conn = ctx
    saved = log(cfg, conn, "checkin sonno 7 stanchezza 4")
    assert saved.lines == [f"Check-in {DAY.day}/{DAY.month}: sonno 7 (buono) · stanchezza 4 "
                           "(tra «leggera» e «normale»)"]
    log(cfg, conn, "checkin umore 6 stanchezza 5")  # a later, partial answer: merged, previous items kept
    [morning] = current(conn)
    assert morning == {"moment": "morning", "sleep_quality_1_10": 7, "fatigue_1_10": 5, "mood_1_10": 6}
    assert conn.execute("SELECT COUNT(*) FROM raw_record WHERE entity_type = 'subjective_checkin'").fetchone()[0] == 2
    log(cfg, conn, "seduta pesi fatica 7 qualità 8 70min")
    log(cfg, conn, "seduta corsa fatica 4")
    log(cfg, conn, "seduta pesi qualità 6")
    answers = checkin.today_answers(conn, DAY)
    assert answers["strength"] == {"moment": "post_session", "session_kind": "strength", "session_rpe_cr10": 7,
                                   "session_quality_1_10": 6, "session_minutes": 70}
    assert answers["run"]["session_rpe_cr10"] == 4 and answers["morning"]["mood_1_10"] == 6
    log(cfg, conn, "dolore spalla 2/10")  # pain check-ins stay separate records
    assert len(current(conn)) == 4
    assert checkin.today_answers(conn, DAY + timedelta(days=1)) == {"morning": {}, "strength": {}, "run": {}}


def test_response_quality_counts_days_and_points_out_identical_runs(ctx):
    cfg, conn = ctx
    for i in range(6):
        log(cfg, conn, "checkin sonno 6 stanchezza 5 umore 6", DAY + timedelta(days=i))
    q = checkin.response_quality(conn, DAY + timedelta(days=5))
    assert q["answered"] == 6 and q["last7"] == 6 and q["identical_streak"] == 6 and q["identical_warning"]
    log(cfg, conn, "checkin sonno 8 stanchezza 3 umore 7", DAY + timedelta(days=6))
    q = checkin.response_quality(conn, DAY + timedelta(days=6))
    assert q["identical_streak"] == 0 and not q["identical_warning"]
    # no look-ahead: answers after the day are ignored
    assert checkin.response_quality(conn, DAY + timedelta(days=2))["answered"] == 3
    # one-item answers repeated are not pointed out (too little to call automatic)
    for i in range(10, 16):
        log(cfg, conn, "checkin sonno 6", DAY + timedelta(days=i))
    assert not checkin.response_quality(conn, DAY + timedelta(days=15))["identical_warning"]


def test_cli_checkin_and_scales(ctx, monkeypatch):
    from askesis.cli.main import app

    cfg, conn = ctx
    r = CliRunner().invoke(app, ["log", "checkin", "sonno 8 fame 6", "--date", DAY.isoformat()])
    assert r.exit_code == 0, r.output
    assert "Check-in" in r.output and "fame 6 (tra «normale, gestibile» e «molta, ci pensavo spesso»)" in r.output
    assert current(conn)[0]["hunger_1_10"] == 6
    r = CliRunner().invoke(app, ["checkin-scales"])
    assert r.exit_code == 0 and "Come hai dormito stanotte?" in r.output and "10 Massimale" in r.output
    assert CliRunner().invoke(app, ["log", "checkin", "sonno 12"]).exit_code != 0


def test_builder_writes_daily_records(ctx):
    cfg, _ = ctx
    [rec] = manual.build(parse_day("checkin sonno 7")[0], cfg, DAY, syn._env("x", DAY, {})["recorded_at"])
    assert rec["entity_type"] == "subjective_checkin" and rec["local_date"] == DAY
    assert rec["payload"] == {"moment": "morning", "sleep_quality_1_10": 7}
