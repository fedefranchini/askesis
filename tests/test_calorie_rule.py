"""Calorie-adjustment rule engine (L2): proposals only, applied with "approvo" (synthetic data)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import synthetic as syn
from test_f3 import prereg

from askesis.ingestion.pipeline import ingest
from askesis.interventions import registry as reg
from askesis.plan import calorie_rule as cr
from askesis.plan import store
from askesis.store.db import connect

START = syn.START
DAY21 = START + timedelta(days=20)
T0 = datetime(2025, 3, 3, 7, tzinfo=UTC)
TARGET = 1700  # synthetic target in test_f3.prereg


def world(tmp_path, slope_kg_day: float, days: int = 21, name: str = "c", kcal: float = TARGET, extra=()):
    conn = connect(tmp_path / f"{name}.db")
    ingest(conn, syn.athlete() + syn.linear_weights(days, START, 68.0, slope_kg_day)
           + syn.constant_intake(days, kcal=kcal) + list(extra), "s")
    iid = reg.propose(conn, "Fase", "phase_start_fat_loss", prereg(), at=T0)
    reg.approve(conn, iid, "approvo", "r", "moderate", at=T0)
    return conn


@pytest.mark.parametrize("slope,status,change", [(-0.02, "propose", -150), (-0.15, "propose", 150),
                                                 (-0.07, "neutral", None)])
def test_decision_follows_the_band(tmp_path, slope, status, change):
    out = cr.evaluate(world(tmp_path, slope), DAY21)
    assert out["status"] == status, out
    assert out.get("change_kcal") == change


def test_waits_for_week_3_and_checks_preconditions(tmp_path):
    conn = world(tmp_path, -0.02)
    assert cr.evaluate(conn, START + timedelta(days=10))["status"] == "waiting"
    sparse = world(tmp_path, -0.02, name="sparse", kcal=TARGET * 1.3)  # intake far from target
    out = cr.evaluate(sparse, DAY21)
    assert out["status"] == "preconditions_not_met" and any("aderenza" in r for r in out["reasons"])


def test_open_safety_flag_blocks_reductions_only(tmp_path):
    for slope, expected in ((-0.02, "blocked_by_safety"), (-0.15, "propose")):
        conn = world(tmp_path, slope, name=f"f{slope}")
        conn.execute("INSERT INTO safety_flag(id, rule_id, tier, local_date, fingerprint, message, signals, actions, "
                     "opened_at) VALUES ('x', 'safety.test@1', 'T1', ?, 'fp', 'm', '{}', '[]', ?)",
                     (DAY21.isoformat(), T0.isoformat()))
        conn.commit()
        assert cr.evaluate(conn, DAY21)["status"] == expected


def test_min_days_between_proposals(tmp_path):
    conn = world(tmp_path, -0.02, days=28)
    assert cr.evaluate(conn, DAY21)["status"] == "propose"
    assert cr.evaluate(conn, DAY21 + timedelta(days=7))["status"] == "too_soon"


def test_later_data_does_not_change_the_decision(tmp_path):
    later = syn.linear_weights(10, DAY21 + timedelta(days=1), 50.0, -1.0)
    a = cr.evaluate(world(tmp_path, -0.02, name="a"), DAY21, record=False)
    b = cr.evaluate(world(tmp_path, -0.02, name="b", extra=later), DAY21, record=False)
    assert a == b


def test_apply_needs_approvo_and_creates_a_linked_target_version(tmp_path):
    conn = world(tmp_path, -0.02)
    out = cr.evaluate(conn, DAY21)
    nxt = DAY21 + timedelta(days=1)
    with pytest.raises(ValueError, match="approvo"):
        cr.apply(conn, out["execution_id"], "ok", nxt)
    cr.apply(conn, out["execution_id"][-8:], "approvo", nxt)
    assert store.content(store.active(conn, "nutrition_target", nxt))["energy_kcal"] == TARGET - 150
    assert store.content(store.active(conn, "nutrition_target", DAY21))["energy_kcal"] == TARGET  # past unchanged
    with pytest.raises(ValueError, match="già applicata"):
        cr.apply(conn, out["execution_id"], "approvo", nxt)
    cr.apply(conn, out["execution_id"], "approvo", nxt + timedelta(days=1), undo=True)
    assert store.content(store.active(conn, "nutrition_target", nxt + timedelta(days=1)))["energy_kcal"] == TARGET
    events = [json.loads(r["payload"]) for r in conn.execute(
        "SELECT payload FROM intervention_event WHERE event = 'amended' ORDER BY rowid")]
    assert [e["undo"] for e in events] == [False, True]


def test_rule_never_drops_below_resting_energy(tmp_path):
    conn = world(tmp_path, -0.02)
    rule = cr.load_rule()
    rule["limits"]["max_change_kcal_per_application"] = 2000
    rule["decision"]["if_slower_than_band"]["change_kcal"] = -1500
    out = cr.evaluate(conn, DAY21, rule=rule)
    assert out["status"] == "propose" and out["proposed_kcal"] >= out["floor_kcal"]


def test_rule_file_is_l2_and_proposes():
    rule = cr.load_rule()
    assert rule["level"] == "L2" and rule["on_trigger"]["action"] == "propose"
