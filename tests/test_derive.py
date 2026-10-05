"""Values derived at activation: declared at proposal, computed from data up to the cut-off, frozen at activation."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import synthetic as syn
from test_f3 import prereg

from askesis.ingestion.pipeline import ingest
from askesis.interventions import registry as reg
from askesis.plan import store
from askesis.store.db import connect
from askesis.validation import textcheck as tc

UNTIL = syn.START + timedelta(days=20)  # Sunday
START = UNTIL + timedelta(days=1)  # Monday
T0 = datetime(2025, 3, 20, 8, 0, tzinfo=UTC)  # proposal and approval before the cut-off


def spec() -> dict:
    return {
        "data_until": UNTIL.isoformat(),
        "values": [
            {"name": "start_weight", "method": "metric", "args": {"metric": "weight_ma7"}, "unit": "kg",
             "description": "media 7 giorni"},
            {"name": "start_tdee", "method": "metric", "args": {"metric": "adaptive_tdee"}, "unit": "kcal/die",
             "description": "TDEE"},
            {"name": "energy", "method": "energy_target", "unit": "kcal/die", "description": "target",
             "args": {"tdee": "start_tdee", "weight": "start_weight", "rate_pct_per_week": 0.6, "round_to": 50}},
            {"name": "protein", "method": "protein_target", "unit": "g/die", "description": "proteine",
             "args": {"weight": "start_weight", "g_per_kg": 1.6, "round_to": 5}},
        ],
    }


def derived_prereg(**kw) -> dict:
    target = {"energy_kcal": "$derived:energy", "protein_g": "$derived:protein",
              "protein_basis": {"method": "per_kg_bw", "g_per_kg": 1.6}, "method": "prior_only"}
    return prereg(
        start=START,
        plan_changes=[{"kind": "nutrition_target", "name": "target", "valid_from": START.isoformat(),
                       "content": target}],
        derived=spec(),
        **kw,
    )


def world(tmp_path, name: str, later: list[dict] | None = None) -> sqlite3.Connection:
    conn = connect(tmp_path / f"{name}.db")
    ingest(conn, syn.athlete() + syn.linear_weights(21) + syn.constant_intake(21) + (later or []), "s")
    return conn


def approved(conn) -> str:
    iid = reg.propose(conn, "Fase", "phase_start_fat_loss", derived_prereg(), at=T0)
    assert reg.approve(conn, iid, "approvo", "r", "moderate", at=T0) == []
    return iid


def test_approval_before_cutoff_waits_for_activation(tmp_path):
    conn = world(tmp_path, "a")
    iid = approved(conn)
    assert reg.status(conn, iid) == "approved"
    assert store.active(conn, "nutrition_target", START) is None
    with pytest.raises(ValueError, match="attivazione possibile"):
        reg.activate(conn, iid, UNTIL)


def test_activation_freezes_values_into_the_plan(tmp_path):
    conn = world(tmp_path, "a")
    iid = approved(conn)
    res = reg.activate(conn, iid, START, at=datetime(2025, 3, 24, 7, tzinfo=UTC))
    v = res["values"]
    assert res["data_until"] == UNTIL.isoformat()
    assert v["energy"]["value"] % 50 == 0 and v["protein"]["value"] % 5 == 0
    deficit = 0.006 * v["start_weight"]["value"] * 7000 / 7
    assert v["energy"]["value"] == pytest.approx(round((v["start_tdee"]["value"] - deficit) / 50) * 50)
    content = store.content(store.active(conn, "nutrition_target", START))
    assert content["energy_kcal"] == v["energy"]["value"] and content["protein_g"] == v["protein"]["value"]
    act = reg.activation(conn, iid)
    assert act["derived"] == json.loads(json.dumps(res)) and len(act["derived_hash"]) == 64
    assert act["baseline"]["period_end"] <= UNTIL.isoformat()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):  # frozen by the database
        conn.execute("UPDATE intervention_event SET payload = '{}' WHERE event = 'activated'")


def test_frozen_values_ignore_data_after_cutoff(tmp_path):
    later = syn.linear_weights(5, UNTIL + timedelta(days=1), 40.0, 0.0)  # very different, known before activation
    out = []
    for name, extra in (("a", []), ("b", later)):
        conn = world(tmp_path, name, extra)
        iid = approved(conn)
        res = reg.activate(conn, iid, START + timedelta(days=5), at=datetime(2025, 3, 30, tzinfo=UTC))
        out.append({k: v["value"] for k, v in res["values"].items()})
    assert out[0] == out[1]


def test_undeclared_placeholder_is_rejected(tmp_path):
    conn = world(tmp_path, "a")
    bad = derived_prereg()
    bad["plan_changes"][0]["content"]["energy_kcal"] = "$derived:unknown"
    with pytest.raises(ValueError, match="non dichiarati"):
        reg.propose(conn, "Fase", "phase_start_fat_loss", bad, at=T0)


# ------------------------------------------------------------------ sources of structured numbers
SOURCES = {
    "plan_changes.target.content.protein_basis.g_per_kg": "within:claim:protein.general_range:1.4-2.0",
    "derived.protein.args.g_per_kg": "within:claim:protein.general_range:1.4-2.0",
    "derived.energy.args.rate_pct_per_week": "within:claim:energy.rate_0_5_to_1_pct:0.5-1",
    "derived": "engineering_choice: arrotondamenti per praticità di logging",
    "expected_outcome": "engineering_choice: intervallo sintetico di test",
    "min_adherence": "engineering_choice: soglie sintetiche di test",
}


def test_structured_numbers_need_a_supporting_source():
    assert tc.check_prereg(derived_prereg(value_sources=SOURCES)).ok
    missing = tc.check_prereg(derived_prereg(value_sources={}))
    assert any("senza fonte" in i.problem and "g_per_kg" in i.segment for i in missing.issues)


def test_value_outside_the_claim_range_is_blocked():
    pr = derived_prereg(value_sources=SOURCES)
    pr["derived"]["values"][3]["args"]["g_per_kg"] = 2.4  # the historical error, now as a structured value
    issues = tc.check_prereg(pr).issues
    assert any("fuori dall'intervallo" in i.problem for i in issues)


def test_invented_range_and_unlabelled_choices_are_blocked():
    assert tc.check_source(1.7, "within:claim:protein.general_range:1.6-2.4")  # not the claim's range
    assert tc.check_source(1.7, "expert_opinion: x")  # a label needs a reason
    assert tc.check_source(1.0, "param:safety_rapid_loss_pct_per_week") is None
    assert tc.check_source(1.2, "param:safety_rapid_loss_pct_per_week")
    assert tc.check_source(150, "rule:calorie_adjustment@2") is None


def test_wildcards_give_each_kind_of_number_its_own_source():
    pr = prereg(value_sources={
        "plan_changes": "engineering_choice: valori sintetici di test",
        "plan_changes.*.content.microcycle.*.lifts.*.rep_range": "within:claim:rt.progression_acsm:6-12",
        "expected_outcome": "engineering_choice: intervallo sintetico di test",
        "min_adherence": "param:intake_min_completeness",
    })
    assert tc.check_prereg(pr).ok
    pr["plan_changes"][1]["content"]["microcycle"][0]["lifts"][0]["rep_range"] = [12, 20]
    assert any("rep_range.1" in i.segment for i in tc.check_prereg(pr).issues)


def test_preview_fallback_is_never_used_at_activation(tmp_path):
    conn = connect(tmp_path / "few.db")
    ingest(conn, syn.athlete() + syn.linear_weights(2, UNTIL - timedelta(days=1)), "s")  # 2 weigh-ins: no 7-day mean
    from askesis.interventions import derive

    s = derive.DerivedSpec.model_validate(spec())
    s.values[0].args["preview_fallback"] = "weight_daily"
    prev = derive.compute(conn, s, preview=True)
    assert "anteprima" in prev["values"]["start_weight"]["source"]
    with pytest.raises(derive.DerivationError, match="non calcolabile"):
        derive.compute(conn, s)


def test_renderings_show_provisional_values_and_keep_health_out_of_the_note(tmp_path):
    from askesis.interventions import derive, present

    conn = world(tmp_path, "a")
    data = derived_prereg(stop_criteria=["sintomi cardiovascolari: stop"])
    data["plan_changes"].append({"kind": "programme", "name": "p", "valid_from": START.isoformat(),
                                 "content": {**prereg()["plan_changes"][1]["content"],
                                             "constraints": {"excluded_exercises": ["deadlift"]}}})
    prev = derive.compute(conn, derive.DerivedSpec.model_validate(data["derived"]), preview=True)
    md = present.render_proposal("Fase", data, prev)
    assert "PROVVISORI" in md and "(provvisorio)" in md and "cardiovascolari" in md
    note = "\n".join(present.plan_note_lines("Fase", data, prev))
    assert "provvisorio" in note and "Panca piana" in note
    assert all(w not in note.lower() for w in ("cardiovascolari", "stop", "deadlift", "stacco da terra", "kg di peso"))
    frozen = "\n".join(present.plan_note_lines("Fase", data, prev | {"frozen": True}, provisional=False))
    assert "provvisorio" not in frozen


def start_spec(fallback: dict | None = None) -> dict:
    s = spec()
    s["values"].insert(2, {"name": "intake", "method": "metric", "unit": "kcal/die", "description": "intake abituale",
                           "args": {"metric": "intake_mean_7d", **({"fallback": fallback} if fallback else {})}})
    s["values"].append({"name": "start", "method": "energy_start", "unit": "kcal/die", "description": "partenza",
                        "args": {"formula_target": "energy", "intake": "intake", "tdee": "start_tdee",
                                 "step_kcal": 200, "round_to": 50}})
    return s


def test_energy_start_is_the_lower_of_formula_and_intake_minus_step(tmp_path):
    from askesis.interventions import derive

    conn = world(tmp_path, "a")
    res = derive.compute(conn, derive.DerivedSpec.model_validate(start_spec()))["values"]
    cands = res["start"]["candidates"]
    assert cands["intake_minus_step"] == pytest.approx(res["intake"]["value"] - 200)
    assert res["start"]["value"] == pytest.approx(round(min(cands.values()) / 50) * 50)


def test_declared_fallback_is_used_when_intake_is_not_computable(tmp_path):
    from askesis.interventions import derive

    conn = connect(tmp_path / "noint.db")
    ingest(conn, syn.athlete() + syn.linear_weights(21), "s")  # weights only, no food log
    with pytest.raises(derive.DerivationError):
        derive.compute(conn, derive.DerivedSpec.model_validate(start_spec()))
    s = derive.DerivedSpec.model_validate(start_spec({"value": 2100, "source": "record:abcd1234"}))
    res = derive.compute(conn, s)["values"]
    assert res["intake"]["value"] == 2100 and "ripiego dichiarato" in res["intake"]["source"]


def test_record_source_must_contain_the_value(tmp_path):
    conn = world(tmp_path, "a")
    rid = conn.execute("SELECT id FROM raw_record WHERE entity_type = 'nutrition_day' LIMIT 1").fetchone()["id"]
    assert tc.check_source(syn.KCAL, f"record:{rid[-8:]}", conn) is None
    assert tc.check_source(syn.KCAL + 300, f"record:{rid[-8:]}", conn)


def test_session_length_estimate_follows_the_parameters():
    from askesis.analytics.params import p
    from askesis.interventions import present

    one = {"lifts": [{"exercise": "lateral_raise", "sets": 2, "rep_range": [8, 12], "target_rir": 1}]}
    expected = (2 * p("session_set_duration_s") + p("session_rest_isolation_s") + p("session_transition_s")) / 60
    assert present.session_minutes(one) == pytest.approx(expected)
    bench = {"exercise": "bench_press", "sets": 1, "rep_range": [6, 8], "target_rir": 2}
    warm = p("session_warmup_sets")
    third = present.session_minutes({"lifts": [bench] * 3}) - present.session_minutes({"lifts": [bench] * 2})
    assert third * 60 == pytest.approx(p("session_set_duration_s") + warm["other_compounds"] * p("session_warmup_set_s")
                                       + p("session_transition_s"))
