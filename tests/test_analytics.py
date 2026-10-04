from datetime import UTC, date, datetime, timedelta

import pytest
import synthetic as syn
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from askesis.analytics import body, energy, engine, review, training
from askesis.analytics.params import PARAMS_FILE, all_params, p
from askesis.config import ROOT
from askesis.ingestion.pipeline import ingest

WEEK4_END = syn.START + timedelta(days=27)  # Sunday of the 4th week


@pytest.fixture
def db(conn):
    recs = (syn.athlete() + syn.linear_weights(28) + syn.constant_intake(28) + syn.strength_session(syn.START)
            + syn.strength_session(syn.START + timedelta(days=3))
            + [syn.run(syn.START + timedelta(days=1), 5.0, 1800, 150), syn.run(syn.START + timedelta(days=5), 7.0,
                                                                             2700, 145)])
    r = ingest(conn, recs, "synthetic")
    assert not r.rejected, r.rejected
    return conn


def values(conn, end=WEEK4_END):
    inp = engine.load_inputs(conn)
    return engine.compute(inp, syn.START, end)


def get(vals, metric, subject="global", end=WEEK4_END):
    return next(m for m in vals if m.metric_id == metric and m.subject == subject and m.period_end == end)


# ------------------------------------------------------------------ parameters
def test_every_parameter_has_basis_and_valid_claims():
    claims = {c["id"] for c in yaml.safe_load((ROOT / "knowledge/evidence/claims.yaml").read_text())["claims"]}
    for key, prm in all_params().items():
        assert prm["basis"] in {"evidence", "expert_opinion", "engineering_choice"}, key
        assert set(prm.get("claims", [])) <= claims, key
    assert PARAMS_FILE.exists()


# ------------------------------------------------------------------ golden: body
def test_rate_exact_on_linear_trend(db):
    vals = values(db)
    m = get(vals, "weight_rate_14d")
    assert m.value == pytest.approx(syn.SLOPE * 7)
    pct = get(vals, "weight_rate_pct_28d")
    mean_w = syn.W0 + syn.SLOPE * 13.5
    assert pct.value == pytest.approx(syn.SLOPE * 7 / mean_w * 100)


def test_ema_lags_a_falling_trend(db):
    ema = get(values(db), "weight_ema")
    last = syn.W0 + syn.SLOPE * 27
    assert last < ema.value < syn.W0


@settings(max_examples=60)
@given(st.lists(st.floats(min_value=40, max_value=150, allow_nan=False), min_size=1, max_size=40))
def test_ema_bounded_by_inputs(ws):
    daily = {syn.START + timedelta(days=2 * i): w for i, w in enumerate(ws)}
    series = body.weight_ema_series(daily)
    assert min(ws) - 1e-9 <= min(series.values()) and max(series.values()) <= max(ws) + 1e-9


def test_theil_sen_robust_to_single_outlier():
    daily = {syn.START + timedelta(days=i): 70 - 0.1 * i for i in range(14)}
    daily[syn.START + timedelta(days=6)] += 4.0  # one bad weigh-in
    m = body.weight_rate(daily, syn.START + timedelta(days=13), 14)[0]
    assert m.value == pytest.approx(-0.7, abs=0.05)
    assert abs(m.detail["ols_kg_per_week"] - (-0.7)) > abs(m.value - (-0.7))


def test_rate_needs_minimum_points():
    daily = {syn.START + timedelta(days=i): 70.0 for i in range(5)}
    assert body.weight_rate(daily, syn.START + timedelta(days=13), 14) == []


def test_daily_weight_prefers_fasted():
    d = syn.START
    daily = body.daily_weights([(d, "2025-03-03T05:00:00+00:00", 70.4, False),
                                (d, "2025-03-03T06:00:00+00:00", 70.0, True)])
    assert daily[d] == 70.0


# ------------------------------------------------------------------ golden: energy
def test_adaptive_tdee_matches_analytic_solution(db):
    t = get(values(db), "adaptive_tdee")
    rho, rho_sd = p("tissue_energy_density"), all_params()["tissue_energy_density"]["sd"]
    observed = syn.KCAL - syn.SLOPE * rho
    obs_var = (syn.SLOPE * rho_sd) ** 2  # constant intake and noise-free weights
    mean_w = syn.W0 + syn.SLOPE * 13.5
    prior = (10 * mean_w + 6.25 * syn.HEIGHT - 5 * syn.AGE - 161) * p("prior_activity_factor_default")
    prior_var = (p("prior_relative_sd") * prior) ** 2
    expected = (observed / obs_var + prior / prior_var) / (1 / obs_var + 1 / prior_var)
    assert t.detail["method"] == "adaptive"
    assert t.detail["observed_kcal"] == pytest.approx(observed)
    assert t.value == pytest.approx(expected)
    assert t.lo < t.value < t.hi


def test_tdee_falls_back_to_prior_with_little_data(conn):
    ingest(conn, syn.athlete() + syn.linear_weights(5), "synthetic")
    inp = engine.load_inputs(conn)
    t = energy.adaptive_tdee(inp.nutrition, body.daily_weights(inp.weighins), inp.athlete,
                             syn.START + timedelta(days=6))
    assert t.detail["method"] == "prior_only"
    assert (t.hi - t.lo) > 500  # honest, wide uncertainty


def test_intake_insufficient_completeness_reports_none():
    days = [energy.NutritionDay(syn.START + timedelta(days=i), 2000, 100, "complete") for i in range(3)]
    m = energy.intake_mean(days, syn.START + timedelta(days=6))[0]
    assert m.value is None and m.detail["reason"] == "insufficient_completeness"


def test_intake_mean_and_protein(db):
    vals = values(db)
    assert get(vals, "intake_mean_7d").value == pytest.approx(syn.KCAL)
    assert get(vals, "protein_mean_7d").value == pytest.approx(syn.PROT)


# ------------------------------------------------------------------ golden: training
def test_hard_sets_volume_and_e1rm(db):
    end = syn.START + timedelta(days=6)
    vals = values(db, end)
    hard = get(vals, "hard_sets", end=end)
    assert hard.value == 8  # per session: 3 bench (RIR ≤ 4) + 1 squat (RIR 3)
    assert hard.detail["proximity_unknown_sets"] == 2  # bench set without RIR, ×2 sessions
    assert get(vals, "volume_per_muscle_week", "muscle:chest", end).value == 6.0
    assert get(vals, "volume_per_muscle_week", "muscle:triceps", end).value == 3.0  # 6 × 0.5
    e1 = get(vals, "e1rm_best_week", "exercise:bench_press", end)
    assert e1.value == pytest.approx(50 * (1 + 10 / 30))  # 8 reps + RIR 2
    assert not e1.detail["lower_bound"]
    assert get(vals, "sessions_strength", end=end).value == 2


def test_e1rm_without_rir_is_lower_bound():
    s = training.SetRow(syn.START, "s", "bench_press", "panca", "working", 50, "external", 6, None, None)
    val, lower = training.e1rm(s)
    assert lower and val == pytest.approx(50 * (1 + 6 / 30))


def test_running_week(db):
    end = syn.START + timedelta(days=6)
    vals = values(db, end)
    assert get(vals, "run_volume_km", end=end).value == pytest.approx(12.0)
    assert get(vals, "run_count", end=end).value == 2
    hr = get(vals, "run_avg_hr", end=end).value
    assert hr == pytest.approx((150 * 1800 + 145 * 2700) / 4500)


def test_dq_grades(db):
    vals = values(db, syn.START + timedelta(days=6))
    overall = get(vals, "dq_score", "domain:overall", syn.START + timedelta(days=6))
    assert overall.value == 1.0 and overall.detail["grade"] == "A"


# ------------------------------------------------------------------ rebuild & reproducibility
def test_rebuild_is_reproducible(db):
    cutoff = datetime(2030, 1, 1, tzinfo=UTC)
    _, n1, d1, ok1 = engine.rebuild(db, cutoff)
    _, n2, d2, ok2 = engine.rebuild(db, cutoff)
    assert ok1 and ok2 and n1 == n2 > 0 and d1 == d2
    assert db.execute("SELECT COUNT(*) FROM metric_run").fetchone()[0] == 1


def test_cutoff_changes_what_is_known(db):
    early = datetime(2025, 3, 10, 0, 0, tzinfo=UTC)
    late = datetime(2030, 1, 1, tzinfo=UTC)
    _, v_early, _ = engine.run(db, syn.START, WEEK4_END, cutoff=early)
    _, v_late, _ = engine.run(db, syn.START, WEEK4_END, cutoff=late)
    assert engine.digest(v_early) != engine.digest(v_late)
    assert sum(m.metric_id == "weight_daily" for m in v_early) == 7


# ------------------------------------------------------------------ review
def test_review_cites_every_number(db):
    ws = syn.START + timedelta(days=21)
    md = review.render(values(db), ws)
    assert "# Review settimanale" in md
    for line in md.splitlines():
        if line.startswith("- ") and "**" in line and "Pesate" not in line:
            assert "@1`" in line, line
    assert "adattivo" in md and "IC" in md


def test_review_without_data_is_honest(conn):
    md = review.render([], syn.START)
    assert "dati insufficienti" in md and "Nessuna sessione" in md


def test_engine_never_uses_wall_clock(db, monkeypatch):
    import askesis.analytics.engine as eng

    cutoff = datetime(2030, 1, 1, tzinfo=UTC)
    d1 = eng.digest(eng.compute(eng.load_inputs(db, cutoff), syn.START, WEEK4_END))
    monkeypatch.setattr(eng, "now_utc", lambda: datetime(1999, 1, 1, tzinfo=UTC))
    d2 = eng.digest(eng.compute(eng.load_inputs(db, cutoff), syn.START, WEEK4_END))
    assert d1 == d2
    assert date(2025, 3, 3).weekday() == 0
