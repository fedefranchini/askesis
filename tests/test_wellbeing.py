"""Questionnaire analysis (A2b) and lagged links (A2c): baseline without look-ahead, minimal change, Hooper-inspired
index, convergence, the non-judgemental T1 pattern, links only after the thresholds. Synthetic data only."""

from datetime import date, timedelta

import pytest

from askesis.analytics import engine, wellbeing
from askesis.analytics.base import MetricValue
from askesis.analytics.params import p
from askesis.safety import rules

D0 = date(2025, 1, 6)
CALM = {"sleep_quality_1_10": 7, "fatigue_1_10": 3, "soreness_1_10": 2, "stress_1_10": 3, "mood_1_10": 7,
        "hunger_1_10": 5, "motivation_1_10": 7, "moment": "morning"}


def days(n: int, start: date = D0) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def calm(n: int, start: date = D0, wobble: bool = True) -> list[tuple[date, dict]]:
    """Stable answers with a small alternating wobble (SD > 0)."""
    out = []
    for i, d in enumerate(days(n, start)):
        pl = dict(CALM)
        if wobble and i % 2:
            pl |= {"sleep_quality_1_10": 6, "fatigue_1_10": 4, "mood_1_10": 6}
        out.append((d, pl))
    return out


def by(values, metric, subject=None):
    return [m for m in values if m.metric_id == metric and (subject is None or m.subject == subject)]


# ------------------------------------------------------------------ baseline and judgement
def test_baseline_excludes_the_day_itself():
    s = {D0 + timedelta(days=i): 5.0 for i in range(20)}
    s[D0 + timedelta(days=20)] = 10.0
    n, mean, _ = wellbeing.baseline(s, D0 + timedelta(days=20))
    assert n == 20 and mean == 5.0  # today's answer never shifts its own baseline


def test_baseline_window_is_28_days():
    s = {D0 + timedelta(days=i): float(i) for i in range(40)}
    n, _, _ = wellbeing.baseline(s, D0 + timedelta(days=40))
    assert n == p("wellbeing_baseline_days")


def test_no_judgement_with_few_responses():
    j = wellbeing.judge(9, 13, 5.0, 1.0, "higher_worse")
    assert j == {"status": "few_responses", "n": 13, "needed": 14}


def test_minimal_change_never_below_one_point():
    j = wellbeing.judge(5.5, 20, 5.0, 0.0, "higher_worse")
    assert j["status"] == "within" and j["mcs"] == 1 and j["z"] is None
    assert wellbeing.judge(6, 20, 5.0, 0.0, "higher_worse")["status"] == "worse"


def test_direction_orients_the_status():
    assert wellbeing.judge(3, 20, 7.0, 1.0, "higher_better")["status"] == "worse"
    assert wellbeing.judge(9, 20, 5.0, 1.0, "higher_worse")["status"] == "worse"
    assert wellbeing.judge(9, 20, 5.0, 1.0, "neutral")["status"] == "higher"


def test_readiness_says_what_is_missing():
    r = wellbeing.readiness(calm(10), D0 + timedelta(days=10))
    assert r == {"n": 10, "needed": 14, "missing": 4, "ready": False, "window_days": 28}
    assert wellbeing.readiness(calm(14), D0 + timedelta(days=14))["ready"]


# ------------------------------------------------------------------ index
def test_hooper_inspired_index_is_labelled_and_summed():
    vals = wellbeing.daily(calm(20), {}, [], [], D0, D0 + timedelta(days=19))
    idx = by(vals, "wellbeing_index")
    first = idx[0]
    assert first.value == (11 - 7) + 3 + 3 + 2
    assert first.detail["label"] == "indice di benessere ispirato a Hooper"
    assert first.detail["status"] == "few_responses"
    assert idx[-1].detail["status"] in ("within", "worse", "better")


def test_index_needs_all_four_items():
    m = [(D0, {"sleep_quality_1_10": 7, "fatigue_1_10": 3, "moment": "morning"})]
    assert not by(wellbeing.daily(m, {}, [], [], D0, D0), "wellbeing_index")


# ------------------------------------------------------------------ convergence
def _bad(d: date) -> tuple[date, dict]:
    return d, CALM | {"sleep_quality_1_10": 2, "fatigue_1_10": 9, "mood_1_10": 2}


def _converging(bad_days: int, gap: bool = False):
    base = calm(20)
    start = D0 + timedelta(days=20)
    bad = [_bad(start + timedelta(days=i + (1 if gap and i >= 1 else 0))) for i in range(bad_days)]
    rhr = {d: 50.0 + (i % 2) for i, d in enumerate(days(20))}
    for d, _ in bad:
        rhr[d] = 70.0
    return base + bad, rhr, bad[-1][0]


def test_convergence_on_after_three_concordant_days():
    morning, rhr, last = _converging(3)
    conv = by(wellbeing.daily(morning, rhr, [], [], D0, last), "wellbeing_convergence")
    final = conv[-1]
    assert final.period_end == last and final.value == 3 and final.detail["active"]
    assert "rhr" in final.detail["objective"] and len(final.detail["subjective"]) >= 2


def test_convergence_not_on_after_two_days():
    morning, rhr, last = _converging(2)
    final = by(wellbeing.daily(morning, rhr, [], [], D0, last), "wellbeing_convergence")[-1]
    assert final.value == 2 and not final.detail["active"]


def test_convergence_needs_an_objective_signal():
    morning, _rhr, last = _converging(4)
    final = by(wellbeing.daily(morning, {}, [], [], D0, last), "wellbeing_convergence")[-1]
    assert final.value == 0 and not final.detail["active"]


def test_a_day_without_answers_breaks_the_streak():
    morning, rhr, last = _converging(3, gap=True)
    final = by(wellbeing.daily(morning, rhr, [], [], D0, last), "wellbeing_convergence")[-1]
    assert final.value == 2 and not final.detail["active"]


def test_strength_drop_beyond_noise_counts_as_objective():
    morning, _rhr, last = _converging(3)
    drop = MetricValue("e1rm_session_change", 1, "exercise:x", last - timedelta(days=3), last - timedelta(days=3),
                       -30.0, "kg", "ESTIMATE", 6, lo=-40.0, hi=-20.0)
    final = by(wellbeing.daily(morning, {}, [], [drop], D0, last), "wellbeing_convergence")[-1]
    assert final.detail["active"] and final.detail["objective"] == ["strength"]


def test_no_convergence_metric_before_the_baseline_is_ready():
    morning, rhr, last = _converging(3)
    vals = wellbeing.daily(morning[10:], rhr, [], [], D0, last)
    assert not by(vals, "wellbeing_convergence")


# ------------------------------------------------------------------ RED-S pattern (T1)
FAT_LOSS = [(D0 + timedelta(days=14), "2025-01-01T00:00:00Z", "phase", {"phase": "fat_loss"})]


def _pattern(n_days: int) -> list[tuple[date, dict]]:
    start = D0 + timedelta(days=20)
    return calm(20) + [(start + timedelta(days=i), CALM | {"hunger_1_10": 8, "fatigue_1_10": 8, "mood_1_10": 3})
                       for i in range(n_days)]


def _inputs(morning, plans) -> engine.Inputs:
    return engine.Inputs(morning=morning, plans=plans)


def test_reds_pattern_flags_t1_in_deficit():
    morning = _pattern(4)
    on = morning[-1][0]
    flags = rules._reds_pattern(_inputs(morning, FAT_LOSS), on)
    assert len(flags) == 1 and flags[0].tier == "T1" and flags[0].rule_id == "safety.reds_pattern@1"
    msg = flags[0].message
    assert "non è una diagnosi né un giudizio" in msg
    assert "non aumentare il deficit" in flags[0].actions


def test_reds_pattern_needs_persistence():
    morning = _pattern(3)
    assert not rules._reds_pattern(_inputs(morning, FAT_LOSS), morning[-1][0])


def test_reds_pattern_only_in_deficit():
    morning = _pattern(5)
    on = morning[-1][0]
    assert not rules._reds_pattern(_inputs(morning, []), on)
    maintenance = [(D0, "2025-01-01T00:00:00Z", "phase", {"phase": "maintenance"})]
    assert not rules._reds_pattern(_inputs(morning, maintenance), on)
    period = FAT_LOSS + [(D0, "2025-01-01T00:00:00Z", "scheduled_period",
                          {"start": D0.isoformat(), "end": (on + timedelta(days=5)).isoformat(),
                           "modifiers": {"energy": "maintenance"}})]
    assert not rules._reds_pattern(_inputs(morning, period), on)


def test_reds_pattern_silent_before_enough_answers():
    morning = _pattern(5)[12:]  # fewer than 14 answers before the pattern
    assert not rules._reds_pattern(_inputs(morning, FAT_LOSS), morning[-1][0])


def test_reds_pattern_ignores_future_answers():
    morning = _pattern(5)
    on = morning[-3][0]  # only 3 pattern days up to `on`
    assert not rules._reds_pattern(_inputs(morning, FAT_LOSS), on)


# ------------------------------------------------------------------ lagged links (A2c)
def _link_data(n_days: int):
    morning, quality = [], {}
    for i, d in enumerate(days(n_days)):
        s = 3 + (i * 7) % 6  # 3..8, deterministic spread
        morning.append((d, {"moment": "morning", "sleep_quality_1_10": s, "stress_1_10": 9 - s}))
        if i % 2 == 0:
            quality[d] = float(s + (1 if i % 4 == 0 else 0))
    return morning, quality


def test_links_wait_for_weeks_and_pairs():
    morning, quality = _link_data(50)
    vals = wellbeing.links(morning, [], [], quality, D0 + timedelta(days=49))
    assert len(vals) == len(wellbeing.LINKS)
    m = next(v for v in vals if v.subject == "link:sleep_q__session_quality")
    assert m.value is None and m.detail["reason"] == "thresholds"
    assert m.detail["weeks"] == 7 and m.detail["weeks_needed"] == 8 and m.detail["pairs_needed"] == 40
    assert m.n_obs == 25


def test_links_after_thresholds_are_hypotheses_with_interval():
    morning, quality = _link_data(84)
    vals = wellbeing.links(morning, [], [], quality, D0 + timedelta(days=83))
    m = next(v for v in vals if v.subject == "link:sleep_q__session_quality")
    assert m.n_obs == 42 and m.value is not None and m.lo < m.value < m.hi
    assert m.value > 0.5  # quality follows sleep in the synthetic data
    stress = next(v for v in vals if v.subject == "link:stress__session_quality")
    assert stress.value < 0
    assert m.detail["tests"] == 5 and m.detail["ci_level"] == p("rate_ci_level")


def test_links_ignore_data_after_end():
    morning, quality = _link_data(84)
    end = D0 + timedelta(days=60)
    full = wellbeing.links(morning, [], [], quality, end)
    cut = wellbeing.links([x for x in morning if x[0] <= end], [], [], {d: v for d, v in quality.items() if d <= end},
                          end)
    assert [(m.value, m.lo, m.hi, m.n_obs) for m in full] == [(m.value, m.lo, m.hi, m.n_obs) for m in cut]


def test_next_day_link_uses_the_following_morning():
    fb = [(D0 + timedelta(days=i), "strength", float(i % 9), 60) for i in range(0, 70, 2)]
    morning = [(D0 + timedelta(days=i), {"moment": "morning", "soreness_1_10": 1 + (i - 1) % 9})
               for i in range(1, 71, 2)]
    pairs = wellbeing.link_pairs(wellbeing.LINKS[3], morning, [], fb, {}, D0 + timedelta(days=70))
    assert len(pairs) == 35 and all(b == a + 1 for a, b in pairs)  # soreness the day after = rpe + 1


def test_spearman_handles_ties():
    rho, lo, hi = wellbeing.spearman_ci([1, 1, 2, 3, 3, 4] * 5, [2, 2, 3, 4, 4, 5] * 5, 0.9)
    assert rho == pytest.approx(1.0) and hi <= 1.0
    assert wellbeing.spearman_ci([1] * 10, list(range(10)), 0.9) is None


# ------------------------------------------------------------------ engine, no look-ahead
def _engine_inputs(n: int) -> engine.Inputs:
    morning, rhr, _ = _converging(3)
    inp = engine.Inputs(morning=[x for x in morning if x[0] < D0 + timedelta(days=n)],
                        rhr={d: v for d, v in rhr.items() if d < D0 + timedelta(days=n)})
    inp.dates = sorted({d for d, _ in inp.morning})
    return inp


def test_engine_values_up_to_a_day_do_not_depend_on_later_answers():
    end = D0 + timedelta(days=21)
    early = [m for m in engine.compute(_engine_inputs(22), D0, end)
             if m.metric_id.startswith("wellbeing") and m.period_end <= end]
    late = [m for m in engine.compute(_engine_inputs(23), D0, end)
            if m.metric_id.startswith("wellbeing") and m.period_end <= end]
    assert [(m.metric_id, m.subject, m.period_end, m.value, m.detail) for m in early] == \
        [(m.metric_id, m.subject, m.period_end, m.value, m.detail) for m in late]
    assert by(early, "wellbeing_item_z") and by(early, "wellbeing_convergence")
