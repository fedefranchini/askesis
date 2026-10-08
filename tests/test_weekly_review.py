"""Weekly review with traffic light: gate by phase, classification of every area, actions, validator, no look-ahead."""

from datetime import timedelta

import pytest
import synthetic as syn
from test_f3 import T0, prereg

from askesis.analytics import engine, review
from askesis.analytics import weekly_review as wr
from askesis.analytics.base import MetricValue
from askesis.analytics.params import p
from askesis.cli.main import app
from askesis.ingestion.pipeline import ingest
from askesis.interventions import registry as reg
from askesis.plan import store
from askesis.store.db import connect
from askesis.validation import textcheck

PHASE_START = syn.START + timedelta(days=7)  # week 2 of the synthetic data
WS1, WS2, WS3 = (syn.START + timedelta(days=7 * i) for i in range(3))
VELOCITY = {"metric_id": "weight_rate_pct_14d", "direction": "decrease", "expected_range": [-5.0, -3.0],
            "min_effect": 3.0}


def _phase(conn, expected=None):
    pr = prereg(start=PHASE_START)
    if expected is not None:
        pr["expected_outcome"] = expected
    iid = reg.propose(conn, "Fase", "phase_start_fat_loss", pr, at=T0)
    reg.approve(conn, iid, "approvo", "r", "moderate", at=T0)
    return iid


@pytest.fixture
def db(conn):
    recs = (syn.athlete() + syn.linear_weights(35) + syn.constant_intake(35) + syn.strength_session(syn.START)
            + syn.strength_session(syn.START + timedelta(days=7)))
    assert not ingest(conn, recs, "synthetic").rejected
    return conn


def mv(metric, value, lo=None, hi=None, **detail):
    return MetricValue(metric, 1, "global", syn.START, syn.START + timedelta(days=6), value, "u", "MEASUREMENT", 5,
                       lo=lo, hi=hi, detail=detail)


def _render(conn, ws, issues=None):
    _, vals, _ = engine.run(conn, ws - timedelta(days=28), ws + timedelta(days=6))
    ctx = wr.context(conn, ws)
    return vals, ctx, (wr.render(vals, ws, issues, ctx) if ctx else None)


# ------------------------------------------------------------------ gate
def test_context_is_none_before_the_phase_and_from_its_first_week(db):
    assert wr.context(db, WS1) is None  # no phase at all
    iid = _phase(db)
    assert wr.context(db, WS1) is None  # week starts before the phase
    ctx = wr.context(db, WS2)
    assert ctx["phase_start"] == PHASE_START and ctx["intervention_number"] == reg.get(db, iid)["number"]
    assert ctx["expected"]["metric_id"] == "weight_ema" and wr.context(db, WS3) is not None


def test_phase_without_intervention_is_not_a_gate(db):
    store.add_version(db, "phase", "x", {"phase": "fat_loss", "exit_criteria": [{"description": "d"}],
                                         "max_duration_weeks": 12, "next_phase": "maintenance"}, WS1, None)
    assert wr.context(db, WS2) is None


# ------------------------------------------------------------------ classification
def test_safety_and_data():
    assert wr.classify_safety([("safety T2", "m"), ("warn", "x")])[0] == wr.ROSSO
    assert wr.classify_safety([("warn", "x")])[0] == wr.VERDE and wr.classify_safety(None)[0] == wr.VERDE
    assert [wr.classify_data(g)[0] for g in (["A", "B"], ["A", "C"], ["B", "D"], [])] == [
        wr.VERDE, wr.GIALLO, wr.ROSSO, wr.NV]


def test_energy_and_protein():
    assert wr.classify_energy(None)[0] == wr.classify_energy(mv("e", None))[0] == wr.NV
    assert wr.classify_energy(mv("e", 50, -20, 120)) == (wr.VERDE, "within")
    assert wr.classify_energy(mv("e", 200, 100, 300)) == (wr.GIALLO, "above")
    assert wr.classify_energy(mv("e", -200, -300, -100)) == (wr.GIALLO, "below")
    assert wr.classify_protein(None)[0] == wr.classify_protein(mv("p", None))[0] == wr.NV
    assert wr.classify_protein(mv("p", -5, -15, -1))[0] == wr.GIALLO
    assert wr.classify_protein(mv("p", -5, -15, 5))[0] == wr.VERDE
    assert wr.classify_protein(mv("p", 20, 10, 30))[0] == wr.VERDE  # above target is never flagged


def test_sessions():
    lim = p("review_sessions_missed_yellow")
    assert wr.classify_sessions(None)[0] == wr.NV
    assert wr.classify_sessions(mv("s", 3, planned=3))[0] == wr.VERDE
    assert wr.classify_sessions(mv("s", 4, planned=3))[0] == wr.VERDE
    assert wr.classify_sessions(mv("s", 3 - lim, planned=3))[0] == wr.GIALLO
    assert wr.classify_sessions(mv("s", 3 - lim - 1, planned=3))[0] == wr.ROSSO


def test_velocity_every_status_and_window():
    exp = VELOCITY
    ok = dict(window_start=PHASE_START, phase_start=PHASE_START)
    assert wr.classify_velocity(mv("v", -4.0, -4.5, -3.5), exp, **ok) == (wr.VERDE, "inside")
    assert wr.classify_velocity(mv("v", -5.5, -6.0, -4.5), exp, **ok) == (wr.GIALLO, "overlap_below")
    assert wr.classify_velocity(mv("v", -2.5, -3.5, -1.5), exp, **ok) == (wr.GIALLO, "overlap_above")
    assert wr.classify_velocity(mv("v", -7.0, -8.0, -6.0), exp, **ok) == (wr.ROSSO, "outside_below")
    assert wr.classify_velocity(mv("v", 4.0, 3.0, 5.0), exp, **ok) == (wr.ROSSO, "outside_above")
    assert wr.classify_velocity(None, exp, **ok) == (wr.NV, "missing")
    assert wr.classify_velocity(mv("v", -4.0), exp, **ok) == (wr.NV, "missing")  # no CI
    assert wr.classify_velocity(mv("v", -4.0, -4.5, -3.5), exp, syn.START, PHASE_START) == (wr.NV, "pre_phase")
    other = exp | {"metric_id": "weight_ema"}
    assert wr.classify_velocity(mv("v", -4.0, -4.5, -3.5), other, **ok) == (wr.NV, "no_range")
    assert wr.classify_velocity(mv("v", -4.0, -4.5, -3.5), None, **ok) == (wr.NV, "no_range")


def test_sleep():
    assert wr.classify_sleep(None)[0] == wr.classify_sleep(mv("s", None))[0] == wr.NV
    assert wr.classify_sleep(mv("s", p("sleep_recommended_min_h")))[0] == wr.VERDE
    assert wr.classify_sleep(mv("s", p("sleep_recommended_min_h") - 0.1))[0] == wr.GIALLO


# ------------------------------------------------------------------ actions
def _area(name, status, action="fai"):
    return wr.Area(name, status, "r", "c", "d", "t", "i", action)


def test_three_actions_ranked_with_mantieni_filler():
    ar = [_area("Safety", wr.VERDE, "a0"), _area("Dati", wr.GIALLO, "a1"), _area("Energia", wr.ROSSO, "a2"),
          _area("Proteine", wr.GIALLO, "a3"), _area("Sedute", wr.VERDE, "a4"), _area("Sonno", wr.NV, "a5"),
          _area("Carico", wr.INFO, "a6")]
    acts = wr.top_actions(ar)
    assert [a.split(": ", 1)[1] for a in acts] == ["a2", "a1", "a3"]  # rosso, then giallo by area order
    assert acts[0].startswith("**Energia** (rosso)")
    acts = wr.top_actions([_area("Dati", wr.GIALLO, "a1"), *ar[:1], ar[4], ar[5], ar[6]])
    assert acts == ["**Dati** (giallo): a1", "Mantieni: a4"]  # filler: green areas in order, never Safety
    assert len(wr.top_actions([_area("Safety", wr.VERDE), _area("Dati", wr.VERDE)])) == 1  # fewer eligible areas


def test_strength_every_status():
    k = p("review_strength_red_weeks")
    up, flat, down = mv("c", 2.0, 0.5, 3.5), mv("c", 0.5, -1.0, 2.0), mv("c", -3.0, -4.5, -1.5)
    assert wr.classify_strength([]) == (wr.NV, "missing")
    assert wr.classify_strength([[mv("c", 1.0)]]) == (wr.NV, "missing")  # noise not estimable
    assert wr.classify_strength([[up], [flat]]) == (wr.VERDE, "ok")
    assert wr.classify_strength([[down, up], [flat]]) == (wr.GIALLO, "down")
    assert wr.classify_strength([[down] * k, [up]]) == (wr.ROSSO, "down_long")
    assert wr.classify_strength([[down] * (k - 1) + [None]])[0] == wr.GIALLO  # streak broken by a missing week


# ------------------------------------------------------------------ render
def test_full_render_passes_the_validator_and_shows_status_words(db):
    _phase(db, VELOCITY)
    vals, ctx, md = _render(db, WS3)
    assert md.startswith("# Review settimanale") and "## Semaforo" in md and "## Tre azioni prioritarie" in md
    assert f"Fase: {ctx['phase_name']}, settimana 2" in md
    for area in ("Safety", "Dati", "Energia", "Proteine", "Sedute", "Velocità del peso", "Forza", "Sonno",
                 "Carico (Foster)"):
        assert f"{area} —" in md
    assert "**Criterio:**" in md and "**Tendenza:**" in md and "**Azione:**" in md
    assert "🟡 giallo" in md  # constant intake above the 1700 kcal target
    assert "ℹ️ informativo" in md
    assert len([ln for ln in md.splitlines() if ln[:2] in ("1.", "2.", "3.")]) == 3
    res = textcheck.validate(md, db)
    assert res.ok, [i.render() for i in res.issues]


def test_velocity_is_not_evaluable_in_the_first_phase_week(db):
    _phase(db, VELOCITY)
    _, _, md = _render(db, WS2)
    assert "include giorni prima della fase" in md and "Velocità del peso | ⚪ non valutabile" in md
    assert textcheck.validate(md, db).ok
    _, _, md3 = _render(db, WS3)
    assert "Velocità del peso | 🟢 verde" in md3 or "Velocità del peso | 🟡" in md3 or "Velocità del peso | 🔴" in md3


def test_safety_flag_makes_the_area_red_and_first_action(db):
    _phase(db, VELOCITY)
    vals, ctx, _ = _render(db, WS3)
    md = wr.render(vals, WS3, [("safety T2", "Valutare i sintomi")], ctx)
    assert "Safety | 🔴 rosso" in md and "- ⚠ Valutare i sintomi" in md
    assert "1. **Safety** (rosso): segui le azioni del flag (vedi docs/agent/safety.md)" in md


def test_actions_never_change_the_plan():
    vals = [mv("weight_rate_pct_14d", -7.0, -8.0, -6.0)]
    ctx = {"phase_start": syn.START, "phase_name": "f", "intervention_number": 1, "expected": VELOCITY}
    a = wr._velocity(wr._Series(vals, syn.START + timedelta(days=6)), ctx | {"phase_start": syn.START - timedelta(30)})
    assert a.status == wr.ROSSO and "nessun aumento del deficit" in a.action and "«approvo»" in a.action


def test_old_format_is_untouched_before_the_phase(db):
    _phase(db, VELOCITY)
    _, vals, _ = engine.run(db, WS1 - timedelta(days=28), WS1 + timedelta(days=6))
    assert wr.context(db, WS1) is None
    assert review.render(vals, WS1, []).startswith("# Review settimanale") and "## 1. Fondamentali" in \
        review.render(vals, WS1, [])


# ------------------------------------------------------------------ no look-ahead
def test_render_ignores_later_data(tmp_path):
    base = syn.athlete() + syn.linear_weights(28) + syn.constant_intake(28)
    later = syn.linear_weights(14, syn.START + timedelta(days=28), 130.0, 1.0)
    out = []
    for name, recs in (("a", base), ("b", base + later)):
        c = connect(tmp_path / f"{name}.db")
        ingest(c, recs, "s")
        _phase(c, VELOCITY)
        _, _, md = _render(c, WS3 + timedelta(days=7))
        out.append(md)
    assert out[0] == out[1] and "Fase:" in out[0]


# ------------------------------------------------------------------ CLI
@pytest.fixture
def cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("ASKESIS_REPORTS_DIR", str(tmp_path / "reports"))
    runner = CliRunner()
    return lambda *a: runner.invoke(app, list(a), catch_exceptions=False), tmp_path / "cli.db"


def test_cli_review_uses_the_new_format_only_inside_a_phase(cli):
    run, path = cli
    conn = connect(path)
    ingest(conn, syn.athlete() + syn.linear_weights(35) + syn.constant_intake(35), "s")
    _phase(conn, VELOCITY)
    conn.close()
    old = run("review", "--stdout", "--date", str(WS1))
    new = run("review", "--stdout", "--date", str(WS3))
    assert old.exit_code == 0 and "## 1. Fondamentali" in old.output and "## Semaforo" not in old.output
    conn = connect(path)
    _, vals, _ = engine.run(conn, WS1 - timedelta(days=28), WS1 + timedelta(days=6))
    assert review.render(vals, WS1, []) in old.output  # byte-identical to the previous format
    conn.close()
    saved = run("review", "--date", str(WS3))
    assert "validata e salvata" in saved.output
    assert new.exit_code == 0 and "## Semaforo" in new.output and "## 1. Fondamentali" not in new.output
