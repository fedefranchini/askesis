import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import synthetic as syn

from askesis.core.ids import new_id
from askesis.ingestion.pipeline import ingest
from askesis.interventions import registry as reg
from askesis.plan import rules, store
from askesis.plan.model import LiftItem
from askesis.safety import rules as safety

D0 = syn.START  # Monday 2025-03-03
T0 = datetime(2025, 3, 3, 8, 0, tzinfo=UTC)


def programme(deload_weeks=()):
    sess = {
        "day": "mon",
        "name": "A",
        "lifts": [{"exercise": "bench_press", "sets": 3, "rep_range": [6, 8], "target_rir": 2}],
    }
    return {
        "microcycle": [sess, {"day": "wed", "name": "Run", "run": {"kind": "easy", "duration_min": 30}}],
        "minimal_week": [sess],
        "deload_weeks": [d.isoformat() for d in deload_weeks],
    }


def phase():
    return {
        "phase": "fat_loss",
        "exit_criteria": [{"description": "durata massima raggiunta"}],
        "max_duration_weeks": 12,
        "next_phase": "maintenance",
    }


def prereg(start=D0, eval_days=28, **kw):
    base = {
        "domain": "body_composition",
        "hypothesis": "un deficit moderato riduce il peso di tendenza",
        "reason": "fase di dimagrimento",
        "change_description": "nuovo target calorico e programma",
        "plan_changes": [
            {"kind": "phase", "name": "fase", "valid_from": start.isoformat(), "content": phase()},
            {"kind": "programme", "name": "base", "valid_from": start.isoformat(), "content": programme()},
            {
                "kind": "nutrition_target",
                "name": "target",
                "valid_from": start.isoformat(),
                "content": {
                    "energy_kcal": 1700,
                    "protein_g": 120,
                    "protein_basis": {"method": "per_kg_bw", "g_per_kg": 1.8},
                    "method": "prior_only",
                },
            },
        ],
        "expected_outcome": {
            "metric_id": "weight_ema",
            "direction": "decrease",
            "expected_range": [65.0, 67.5],
            "min_effect": 0.5,
        },
        "success_criteria": "EMA in calo nell'intervallo atteso",
        "stop_criteria": ["flag di safety T2+"],
        "min_adherence": {"weight_days": 0.7, "nutrition_complete_days": 0.7},
        "start_date": start.isoformat(),
        "evaluation_date": (start + timedelta(days=eval_days)).isoformat(),
        "evidence_claims": ["energy.rate_0_5_to_1_pct"],
        "expert_opinion": ["fattore di attività"],
    }
    base.update(kw)
    return base


# ------------------------------------------------------------------ plan versions & point-in-time
def test_plan_versions_and_point_in_time(conn):
    v1 = store.add_version(conn, "programme", "base", programme(), D0, None, T0)
    v2 = store.add_version(
        conn, "programme", "base", programme(), D0 + timedelta(days=14), None, T0 + timedelta(days=10)
    )
    conn.commit()
    assert store.active(conn, "programme", D0 + timedelta(days=12))["id"] == v1
    assert store.active(conn, "programme", D0 + timedelta(days=20))["id"] == v2
    # what we believed on day 5 about day 20 (v2 not yet recorded)
    assert store.active(conn, "programme", D0 + timedelta(days=20), as_of=T0 + timedelta(days=5))["id"] == v1
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE plan_version SET name='x'")


def test_programme_requires_minimal_week(conn):
    bad = programme()
    bad["minimal_week"] = []
    with pytest.raises(ValueError):
        store.add_version(conn, "programme", "base", bad, D0, None)


# ------------------------------------------------------------------ L1 rules
def ex(day, loads, reps, rirs):
    return rules.Exposure(day, loads, reps, rirs)


ITEM = LiftItem(exercise="bench_press", sets=3, rep_range=(6, 8), target_rir=2)


def test_double_progression_increases_after_two_topped_exposures():
    h = [ex(D0, [50] * 3, [8, 8, 8], [2, 2, 3]), ex(D0 + timedelta(days=7), [50] * 3, [8, 8, 8], [2, 3, 2])]
    rx = rules.double_progression(ITEM, h)
    assert rx.load_kg == 52.5 and rx.rep_target == 6  # catalog increment 2.5 kg, within 2–10%


def test_double_progression_holds_when_effort_too_high():
    h = [ex(D0, [50] * 3, [8, 8, 8], [2, 2, 2]), ex(D0 + timedelta(days=7), [50] * 3, [8, 8, 8], [1, 1, 0])]
    rx = rules.double_progression(ITEM, h)
    assert rx.load_kg == 50 and rx.rep_target == 8


def test_double_progression_adds_reps_and_handles_missing_rir():
    assert rules.double_progression(ITEM, [ex(D0, [50] * 3, [7, 6, 6], [2, 2, 2])]).rep_target == 8
    rx = rules.double_progression(ITEM, [ex(D0, [50] * 3, [8, 8, 8], [None, None, None])])
    assert rx.load_kg == 50 and "RIR mancante" in rx.reason
    assert rules.double_progression(ITEM, []).load_kg is None  # calibration


def test_next_session_applies_planned_deload_and_records_execution(conn):
    store.add_version(conn, "programme", "base", programme(deload_weeks=[D0 + timedelta(days=7)]), D0, None)
    conn.commit()
    normal = rules.next_session(conn, D0)
    deload = rules.next_session(conn, D0 + timedelta(days=7))
    assert normal["sessions"][0]["lifts"][0]["sets"] == 3
    assert deload["deload"] and deload["sessions"][0]["lifts"][0]["sets"] == 2
    assert deload["sessions"][0]["lifts"][0]["target_rir"] == 4
    assert conn.execute("SELECT COUNT(*) FROM rule_execution").fetchone()[0] == 2
    assert rules.next_session(conn, D0 + timedelta(days=1))["status"] == "rest_day"


def test_scheduled_period_switches_to_minimal_week(conn):
    store.add_version(conn, "programme", "base", programme(), D0, None)
    store.add_version(
        conn,
        "scheduled_period",
        "esami",
        {
            "label": "periodo a carico ridotto",
            "start": "2025-03-10",
            "end": "2025-03-23",
            "modifiers": {"use_minimal_week": True, "volume_factor": 0.5},
        },
        D0,
        None,
    )
    conn.commit()
    out = rules.next_session(conn, D0 + timedelta(days=7))
    assert out["minimal_week"] and out["sessions"][0]["lifts"][0]["sets"] == 2
    assert rules.next_session(conn, D0 + timedelta(days=9))["status"] == "rest_day"  # Wed run not in minimal week


# ------------------------------------------------------------------ safety
def health(day, **payload):
    t = datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC)
    return dict(
        id=new_id(),
        entity_type="health_event",
        occurred_at=t,
        tz=syn.ZONE,
        local_date=day,
        recorded_at=t,
        source_id="synthetic",
        payload=payload,
    )


def test_safety_rapid_weight_loss(conn):
    ingest(conn, syn.athlete() + syn.linear_weights(28, slope=-0.15), "s")  # ≈ -1.6 %BW/week
    flags = safety.evaluate(conn, D0 + timedelta(days=27))
    f = [x for x in flags if x.rule_id.startswith("safety.rapid_weight_loss")]
    assert f and f[0].tier == "T2"


def test_safety_moderate_loss_not_flagged(conn):
    ingest(conn, syn.athlete() + syn.linear_weights(28, slope=-0.05), "s")  # ≈ -0.5 %BW/week
    assert not [x for x in safety.evaluate(conn, D0 + timedelta(days=27)) if "rapid" in x.rule_id]


def test_safety_first_diet_week_excluded(conn):
    ingest(conn, syn.athlete() + syn.linear_weights(28, slope=-0.15), "s")
    store.add_version(conn, "phase", "fase", phase(), D0 + timedelta(days=20), None)
    conn.commit()
    assert not [x for x in safety.evaluate(conn, D0 + timedelta(days=27)) if "rapid" in x.rule_id]


def test_safety_pain(conn):
    recs = syn.strength_session(D0)
    recs[1]["payload"]["pain"] = 5
    ingest(conn, recs, "s")
    f = [x for x in safety.evaluate(conn, D0) if x.rule_id.startswith("safety.pain")]
    assert f and f[0].tier == "T1"


def test_safety_cardiovascular_is_T3_and_blocks_everything(conn):
    ingest(conn, [health(D0, kind="symptom", description="test", red_flags=["chest_pain"])], "s")
    f = safety.evaluate(conn, D0)
    assert any(x.tier == "T3" for x in f)
    with pytest.raises(safety.SafetyBlock):
        safety.gate(conn, "measurement_protocol")


def test_safety_eating_disorder_signals_T2(conn):
    ingest(conn, [health(D0, kind="symptom", signals=["compensatory_exercise"])], "s")
    f = [x for x in safety.evaluate(conn, D0) if "eating" in x.rule_id]
    assert f and f[0].tier == "T2" and "sospendere target calorici restrittivi" in f[0].actions


def test_safety_low_intake_proxy(conn):
    ingest(conn, syn.athlete() + syn.linear_weights(7) + syn.constant_intake(7, kcal=1000), "s")
    assert [x for x in safety.evaluate(conn, D0 + timedelta(days=6)) if "low_energy" in x.rule_id]


def test_safety_is_idempotent_and_resolvable(conn):
    ingest(conn, [health(D0, kind="symptom", red_flags=["syncope"])], "s")
    safety.evaluate(conn, D0)
    safety.evaluate(conn, D0)
    flags = safety.open_flags(conn)
    assert len(flags) == 1
    safety.resolve(conn, flags[0]["id"], "valutazione medica: nulla di rilevante")
    assert safety.open_flags(conn) == []


def test_gate_blocks_stress_increase_with_T1(conn):
    recs = syn.strength_session(D0)
    recs[1]["payload"]["pain"] = 5
    ingest(conn, recs, "s")
    safety.evaluate(conn, D0)
    with pytest.raises(safety.SafetyBlock):
        safety.gate(conn, "training_volume_increase")
    safety.gate(conn, "deload")  # not stress-increasing: allowed


# ------------------------------------------------------------------ interventions
def seeded(conn, days=28, slope=-0.05, intake_days=28):
    ingest(conn, syn.athlete() + syn.linear_weights(days, slope=slope) + syn.constant_intake(intake_days), "s")


def test_lifecycle_and_frozen_prereg(conn):
    seeded(conn)
    iid = reg.propose(conn, "Fase dimagrimento", "phase_start_fat_loss", prereg(), at=T0)
    with pytest.raises(ValueError):
        reg.approve(conn, iid, "ok va bene", "motivo", "moderate", at=T0)  # needs explicit "approvo"
    versions = reg.approve(conn, iid, "Approvo", "deficit moderato", "moderate", at=T0)
    assert len(versions) == 3 and reg.status(conn, iid) == "activated"
    assert store.active(conn, "nutrition_target", D0)["intervention_id"] == iid
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE intervention SET prereg='{}'")
    reg.amend(conn, iid, "aggiunto controllo intermedio", at=T0 + timedelta(days=7))
    assert reg.status(conn, iid) == "amended"


def test_one_active_intervention_per_domain(conn):
    seeded(conn)
    a = reg.propose(conn, "A", "phase_start_fat_loss", prereg(), at=T0)
    reg.approve(conn, a, "approvo", "r", "moderate", at=T0)
    b = reg.propose(conn, "B", "energy_deficit_increase", prereg(plan_changes=[]), at=T0)
    with pytest.raises(ValueError, match="già un intervento attivo"):
        reg.approve(conn, b, "approvo", "r", "moderate", at=T0)


def test_safety_gate_blocks_approval(conn):
    seeded(conn)
    ingest(conn, [health(D0, kind="symptom", red_flags=["chest_pain"])], "s")
    safety.evaluate(conn, D0)
    iid = reg.propose(conn, "Fase", "phase_start_fat_loss", prereg(), at=T0)
    with pytest.raises(safety.SafetyBlock):
        reg.approve(conn, iid, "approvo", "r", "moderate", at=T0)


def test_rejection_is_recorded(conn):
    seeded(conn)
    iid = reg.propose(conn, "Fase", "phase_start_fat_loss", prereg(), at=T0)
    reg.reject(conn, iid, "no, preferisco aspettare", "rimandato", at=T0)
    assert reg.status(conn, iid) == "rejected"
    assert conn.execute("SELECT status FROM decision").fetchone()[0] == "rejected"


def test_evaluation_effective(conn):
    seeded(conn, days=29)
    iid = reg.propose(conn, "Fase", "phase_start_fat_loss", prereg(start=D0, eval_days=28), at=T0)
    reg.approve(conn, iid, "approvo", "r", "moderate", at=T0)
    res = reg.evaluate(conn, iid, D0 + timedelta(days=28), at=T0 + timedelta(days=28))
    assert res["conclusion"] == "effective", res
    assert reg.status(conn, iid) == "concluded"
    assert conn.execute("SELECT COUNT(*) FROM response_profile").fetchone()[0] == 1


def test_evaluation_low_adherence_is_inconclusive(conn):
    seeded(conn, days=29, intake_days=5)
    iid = reg.propose(conn, "Fase", "phase_start_fat_loss", prereg(), at=T0)
    reg.approve(conn, iid, "approvo", "r", "moderate", at=T0)
    res = reg.evaluate(conn, iid, D0 + timedelta(days=28), at=T0 + timedelta(days=28))
    assert res["conclusion"] == "inconclusive_low_adherence"


def test_evaluation_flags_confounders(conn):
    seeded(conn, days=29)
    ctx = health(D0 + timedelta(days=10), kind="illness", description="raffreddore")
    ingest(conn, [ctx], "s")
    iid = reg.propose(conn, "Fase", "phase_start_fat_loss", prereg(), at=T0)
    reg.approve(conn, iid, "approvo", "r", "moderate", at=T0)
    res = reg.evaluate(conn, iid, D0 + timedelta(days=28), at=T0 + timedelta(days=28))
    assert res["conclusion"].endswith("_confounded") and res["confounders"]


def test_why_answers_when_why_and_what_happened(conn):
    seeded(conn, days=29)
    iid = reg.propose(conn, "Fase dimagrimento", "phase_start_fat_loss", prereg(), at=T0)
    reg.approve(conn, iid, "Approvo", "deficit moderato con prior a incertezza ampia", "moderate", at=T0)
    reg.evaluate(conn, iid, D0 + timedelta(days=14), at=T0 + timedelta(days=14))
    reg.evaluate(conn, iid, D0 + timedelta(days=28), at=T0 + timedelta(days=28))
    text = reg.render_why(reg.why(conn, 1))
    for needle in (
        "Quando:",
        "Perché",
        "Approvo",
        "baseline",
        "valutazione",
        "energy.rate_0_5_to_1_pct",
        "Opinione esperta",
        "concluded",
    ):
        assert needle in text, needle


def test_no_prescription_while_T2_or_T3_flag_open(conn):
    store.add_version(conn, "programme", "base", programme(), D0, None)
    conn.commit()
    ingest(conn, [health(D0, kind="symptom", red_flags=["syncope"])], "s")
    safety.evaluate(conn, D0)
    out = rules.next_session(conn, D0)
    assert out["status"] == "blocked_by_safety" and out["flags"][0]["tier"] == "T3"
    assert conn.execute("SELECT COUNT(*) FROM rule_execution").fetchone()[0] == 0
    safety.resolve(conn, safety.open_flags(conn)[0]["id"], "valutazione medica: ok")
    assert rules.next_session(conn, D0)["status"] == "session"
