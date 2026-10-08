"""Analysis of the daily questionnaire (A2b) and lagged links (A2c). Pure functions over the engine inputs.

Everything is judged against the athlete's own recent answers, never against population norms:
- personal baseline: the answers of the previous `wellbeing_baseline_days` days, EXCLUDING the day itself (no
  look-ahead, and today's answer never shifts its own baseline); nothing is judged with fewer than
  `wellbeing_min_responses` answers in the baseline;
- z-score = (answer − baseline mean) / baseline SD, oriented with the item's declared direction, and a minimal
  significant change = max(`wellbeing_z_threshold` × SD, `wellbeing_min_change_points`): the scales are integers, so a
  change smaller than one point is never a change;
- wellbeing index "ispirato a Hooper": sum of sleep (reversed), fatigue, stress and soreness, the four items of the
  Hooper index, on our 1–10 scales (4–40, higher = worse). It is NOT the Hooper index (original scales differ, and its
  exact form is not in the abstracts read): an engineering choice, labelled as such;
- convergence: a day is concordant when at least `convergence_min_subjective` items are worse beyond the minimal
  change AND at least `convergence_min_objective` objective signal is worse (resting HR above its baseline, Watch sleep
  below its baseline, a strength session or an easy-run efficiency down beyond noise in the last
  `convergence_objective_lookback_days` days); the signal is on after `convergence_min_days` consecutive concordant
  days. A day without answers breaks the streak;
- lagged links (A2c): a fixed, declared list of pairs (no fishing among all combinations), Spearman correlation with a
  confidence interval, only after `links_min_weeks` weeks of questionnaire and `links_min_pairs` pairs. Always
  presented as a hypothesis, never as a cause.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import NormalDist

import numpy as np

from askesis import checkin

from .base import MetricValue
from .params import p

# items that enter the subjective signals (hunger is "neutral": more hunger is neither good nor bad by itself)
ORIENTED = tuple(i for i in checkin.MORNING if i.direction != "neutral")
INDEX_ITEMS = ("sleep_quality_1_10", "fatigue_1_10", "stress_1_10", "soreness_1_10")  # the Hooper four
INDEX_LABEL = "indice di benessere ispirato a Hooper"


@dataclass(frozen=True)
class Link:
    key: str
    x_label: str
    y_label: str
    lag_days: int  # y is read `lag_days` after x


LINKS: tuple[Link, ...] = (
    Link("sleep_q__session_quality", "sonno percepito (mattina)", "qualità della seduta dello stesso giorno", 0),
    Link("sleep_h__session_quality", "ore di sonno (orologio)", "qualità della seduta dello stesso giorno", 0),
    Link("stress__session_quality", "stress (mattina)", "qualità della seduta dello stesso giorno", 0),
    Link("session_rpe__soreness", "fatica della seduta (CR-10)", "indolenzimento del mattino dopo", 1),
    Link("session_load__fatigue", "carico della seduta (Foster)", "stanchezza del mattino dopo", 1),
)


# ------------------------------------------------------------------ baseline
def baseline(series: dict[date, float], day: date) -> tuple[int, float | None, float | None]:
    """(n, mean, sample SD) of the values in the `wellbeing_baseline_days` days before `day` (the day excluded)."""
    start = day - timedelta(days=int(p("wellbeing_baseline_days")))
    xs = [v for d, v in series.items() if start <= d < day]
    if not xs:
        return 0, None, None
    sd = float(np.std(xs, ddof=1)) if len(xs) > 1 else None
    return len(xs), float(np.mean(xs)), sd


def judge(x: float, n: int, mean: float | None, sd: float | None, direction: str,
          floor: float | None = None) -> dict:
    """Status of one answer against its baseline: worse | better | within | few_responses.

    `direction`: higher_better | higher_worse | neutral (neutral: "higher" | "lower" instead of better/worse)."""
    need = int(p("wellbeing_min_responses"))
    if n < need or mean is None:
        return {"status": "few_responses", "n": n, "needed": need}
    floor = p("wellbeing_min_change_points") if floor is None else floor
    mcs = max(p("wellbeing_z_threshold") * (sd or 0.0), floor)
    diff = x - mean
    z = diff / sd if sd else None
    if abs(diff) < mcs:
        status = "within"
    elif direction == "neutral":
        status = "higher" if diff > 0 else "lower"
    else:
        status = "worse" if (diff > 0) == (direction == "higher_worse") else "better"
    return {"status": status, "n": n, "mean": mean, "sd": sd, "z": z, "diff": diff, "mcs": mcs, "x": x}


def morning_series(morning: list[tuple[date, dict]]) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = {i.field: {} for i in checkin.MORNING}
    for d, pl in morning:
        for f in out:
            if pl.get(f) is not None:
                out[f][d] = float(pl[f])
    return out


def index_series(series: dict[str, dict[date, float]]) -> dict[date, float]:
    """Hooper-inspired sum (4–40, higher = worse) on days with all four items answered."""
    days = set.intersection(*(set(series[f]) for f in INDEX_ITEMS))
    return {d: (11 - series["sleep_quality_1_10"][d]) + series["fatigue_1_10"][d] + series["stress_1_10"][d]
            + series["soreness_1_10"][d] for d in sorted(days)}


def readiness(morning: list[tuple[date, dict]], day: date) -> dict:
    """How many morning answers the baseline of `day` holds and how many are still missing (for the 'cosa manca')."""
    start = day - timedelta(days=int(p("wellbeing_baseline_days")))
    n = sum(1 for d, pl in morning if start <= d < day and any(pl.get(i.field) is not None for i in checkin.MORNING))
    need = int(p("wellbeing_min_responses"))
    return {"n": n, "needed": need, "missing": max(0, need - n), "ready": n >= need,
            "window_days": int(p("wellbeing_baseline_days"))}


# ------------------------------------------------------------------ objective signals
def objective_signals(day: date, rhr: dict[date, float], sleep_h: dict[date, float],
                      changes: list[MetricValue]) -> list[str]:
    """Objective signals worse than usual on `day` (only data up to `day`)."""
    out = []
    if day in rhr:
        n, mean, sd = baseline(rhr, day)
        if judge(rhr[day], n, mean, sd, "higher_worse", floor=0.0)["status"] == "worse" and sd:
            out.append("rhr")
    if day in sleep_h:
        n, mean, sd = baseline(sleep_h, day)
        if judge(sleep_h[day], n, mean, sd, "higher_better", floor=0.0)["status"] == "worse" and sd:
            out.append("sleep_watch")
    lookback = day - timedelta(days=int(p("convergence_objective_lookback_days")) - 1)
    for metric, name in (("e1rm_session_change", "strength"), ("run_efficiency_change", "run_efficiency")):
        recent = [m for m in changes if m.metric_id == metric and lookback <= m.period_end <= day]
        if any(m.hi is not None and m.hi < 0 for m in recent):
            out.append(name)
    return out


# ------------------------------------------------------------------ daily metrics
def daily(morning: list[tuple[date, dict]], rhr: dict[date, float], sleep_s: list[tuple[date, float]],
          changes: list[MetricValue], start: date, end: date) -> list[MetricValue]:
    """Per answered day in [start, end]: item z-scores, the Hooper-inspired index, the convergence streak."""
    series = morning_series([(d, pl) for d, pl in morning if d <= end])
    idx = index_series(series)
    sleep_h = {d: s / 3600 for d, s in sleep_s if d <= end}
    rhr = {d: v for d, v in rhr.items() if d <= end}
    answered = sorted({d for s in series.values() for d in s})
    out: list[MetricValue] = []
    streak, prev_day = 0, None
    for d in answered:
        worse = []
        for item in checkin.MORNING:
            if d not in series[item.field]:
                continue
            n, mean, sd = baseline(series[item.field], d)
            j = judge(series[item.field][d], n, mean, sd, item.direction)
            if j["status"] == "worse":
                worse.append(item.key)
            if start <= d <= end:
                out.append(MetricValue("wellbeing_item_z", 1, f"item:{item.key}", d, d, j.get("z"), "z",
                                       "INFERENCE", n, detail=j | {"direction": item.direction}))
        if d in idx and start <= d <= end:
            n, mean, sd = baseline(idx, d)
            j = judge(idx[d], n, mean, sd, "higher_worse")
            out.append(MetricValue("wellbeing_index", 1, "global", d, d, idx[d], "punti", "INFERENCE", n,
                                   detail=j | {"label": INDEX_LABEL, "items": list(INDEX_ITEMS)}))
        ready = readiness(morning, d)["ready"]
        objective = objective_signals(d, rhr, sleep_h, changes) if ready else []
        concordant = ready and len(worse) >= p("convergence_min_subjective") and \
            len(objective) >= p("convergence_min_objective")
        consecutive = prev_day is not None and (d - prev_day).days == 1
        streak = (streak + 1 if consecutive else 1) if concordant else 0
        prev_day = d
        if start <= d <= end and ready:
            out.append(MetricValue("wellbeing_convergence", 1, "global", d, d, float(streak), "giorni", "INFERENCE",
                                   1, detail={"subjective": worse, "objective": objective,
                                              "active": streak >= p("convergence_min_days")}))
    return out


# ------------------------------------------------------------------ RED-S pattern (used by the safety layer)
def reds_pattern(morning: list[tuple[date, dict]], on: date) -> dict | None:
    """Days in the last `reds_pattern_window_days` (ending on `on`) with high hunger, high fatigue and low mood.

    Each condition is met by the answer's anchor (hunger and fatigue at least the "molta"/"stanco" anchors, mood at
    most "giù") or by a change beyond the minimal change against the personal baseline. Returns None until the
    baseline holds enough answers. Whether the athlete is in a deficit is decided by the caller."""
    if not readiness(morning, on)["ready"]:
        return None
    series = morning_series([(d, pl) for d, pl in morning if d <= on])
    start = on - timedelta(days=int(p("reds_pattern_window_days")) - 1)
    hits = []
    for d in sorted(x for x in series["hunger_1_10"] if start <= x <= on):
        if d not in series["fatigue_1_10"] or d not in series["mood_1_10"]:
            continue
        h, f, m = series["hunger_1_10"][d], series["fatigue_1_10"][d], series["mood_1_10"][d]

        def beyond(field: str, x: float, direction: str, want: str, _d: date = d) -> bool:
            return judge(x, *baseline(series[field], _d), direction)["status"] == want

        if ((h >= p("reds_hunger_min") or beyond("hunger_1_10", h, "neutral", "higher"))
                and (f >= p("reds_fatigue_min") or beyond("fatigue_1_10", f, "higher_worse", "worse"))
                and (m <= p("reds_mood_max") or beyond("mood_1_10", m, "higher_better", "worse"))):
            hits.append(d)
    return {"days": hits, "window_days": int(p("reds_pattern_window_days")),
            "min_days": int(p("reds_pattern_min_days")), "active": len(hits) >= p("reds_pattern_min_days")}


# ------------------------------------------------------------------ lagged links (A2c)
def _ranks(xs: list[float]) -> np.ndarray:
    """Average ranks (ties share the mean rank)."""
    a = np.asarray(xs, dtype=float)
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a))
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[order[j + 1]] == a[order[i]]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman_ci(xs: list[float], ys: list[float], level: float) -> tuple[float, float, float] | None:
    """Spearman rho with a Fisher-z interval using the variance 1.06/(n−3) (Fieller, Hartley & Pearson 1957)."""
    n = len(xs)
    if n < 4:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    if np.std(rx) == 0 or np.std(ry) == 0:
        return None
    rho = float(np.corrcoef(rx, ry)[0, 1])
    rho_c = min(max(rho, -0.999999), 0.999999)
    zc = NormalDist().inv_cdf(0.5 + level / 2)
    se = math.sqrt(1.06 / (n - 3))
    z = math.atanh(rho_c)
    return rho, math.tanh(z - zc * se), math.tanh(z + zc * se)


def link_pairs(link: Link, morning: list[tuple[date, dict]], sleep_s: list[tuple[date, float]],
               feedback: list[tuple[date, str, float | None, int | None]], quality: dict[date, float],
               end: date) -> list[tuple[float, float]]:
    """(x, y) pairs for a link with both days ≤ end."""
    series = morning_series([(d, pl) for d, pl in morning if d <= end])
    rpe: dict[date, float] = {}
    load: dict[date, float] = {}
    for d, _kind, r, minutes in feedback:
        if r is not None:
            rpe[d] = max(rpe.get(d, 0.0), r)
            if minutes is not None:
                load[d] = load.get(d, 0.0) + r * minutes
    x = {"sleep_q__session_quality": series["sleep_quality_1_10"],
         "sleep_h__session_quality": {d: s / 3600 for d, s in sleep_s},
         "stress__session_quality": series["stress_1_10"],
         "session_rpe__soreness": rpe, "session_load__fatigue": load}[link.key]
    y = {"sleep_q__session_quality": quality, "sleep_h__session_quality": quality,
         "stress__session_quality": quality, "session_rpe__soreness": series["soreness_1_10"],
         "session_load__fatigue": series["fatigue_1_10"]}[link.key]
    out = []
    for d in sorted(x):
        dy = d + timedelta(days=link.lag_days)
        if dy <= end and dy in y:
            out.append((float(x[d]), float(y[dy])))
    return out


def links(morning: list[tuple[date, dict]], sleep_s: list[tuple[date, float]],
          feedback: list[tuple[date, str, float | None, int | None]], quality: dict[date, float],
          end: date) -> list[MetricValue]:
    """One `lagged_link` per declared pair, as of `end`; value None (with what is missing) until the thresholds."""
    answered = [d for d, pl in morning if d <= end and any(pl.get(i.field) is not None for i in checkin.ITEMS)]
    answered += [d for d in quality if d <= end]
    first = min(answered, default=None)
    weeks = 0 if first is None else ((end - first).days + 1) // 7
    need_w, need_n, level = int(p("links_min_weeks")), int(p("links_min_pairs")), float(p("rate_ci_level"))
    out = []
    for link in LINKS:
        pairs = link_pairs(link, morning, sleep_s, feedback, quality, end)
        detail = {"x": link.x_label, "y": link.y_label, "lag_days": link.lag_days, "weeks": weeks,
                  "weeks_needed": need_w, "pairs_needed": need_n, "tests": len(LINKS), "ci_level": level,
                  "method": "spearman_fisher_z_fieller"}
        res = None
        if weeks >= need_w and len(pairs) >= need_n:
            res = spearman_ci([a for a, _ in pairs], [b for _, b in pairs], level)
            if res is None:
                detail["reason"] = "no_variation"
        else:
            detail["reason"] = "thresholds"
        start = first or end
        out.append(MetricValue("lagged_link", 1, f"link:{link.key}", start, end, None if res is None else res[0],
                               "rho", "INFERENCE", len(pairs), lo=None if res is None else res[1],
                               hi=None if res is None else res[2], detail=detail))
    return out
