"""Intake and adaptive TDEE (docs/architecture.md §5.2).

adaptive_tdee: observed = mean intake − weight slope × tissue energy density, over a window; fused by
inverse variance with a prior (Mifflin-St Jeor × activity factor). A constant logging bias is absorbed:
the estimate is "maintenance as measured by the athlete's own logging".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
from scipy import stats

from .base import MetricValue
from .body import ols_slope_per_day
from .params import p, sd


@dataclass(frozen=True)
class NutritionDay:
    day: date
    kcal: float | None
    protein_g: float | None
    completeness: str


@dataclass(frozen=True)
class AthleteBasics:
    height_cm: float | None
    age_years: float | None
    sex: str | None  # "male" | "female"
    activity_factor: float | None = None


def _complete(days: list[NutritionDay], start: date, end: date) -> list[NutritionDay]:
    return [n for n in days if start <= n.day <= end and n.completeness == "complete" and n.kcal is not None]


def intake_mean(days: list[NutritionDay], end: date, window: int = 7) -> list[MetricValue]:
    start = end - timedelta(days=window - 1)
    comp = _complete(days, start, end)
    completeness = len(comp) / window
    out = []
    detail = {"complete_days": len(comp), "window_days": window}
    if completeness < p("intake_min_completeness") or len(comp) < 2:
        out.append(MetricValue(f"intake_mean_{window}d", 1, "global", start, end, None, "kcal/day", "MEASUREMENT",
                               len(comp), dq=completeness, detail=detail | {"reason": "insufficient_completeness"}))
        return out
    k = np.array([n.kcal for n in comp])
    t = stats.t.ppf(0.5 + p("rate_ci_level") / 2, len(k) - 1)
    se = k.std(ddof=1) / np.sqrt(len(k))
    out.append(MetricValue(f"intake_mean_{window}d", 1, "global", start, end, float(k.mean()), "kcal/day",
                           "MEASUREMENT", len(k), lo=float(k.mean() - t * se), hi=float(k.mean() + t * se),
                           dq=completeness, detail=detail))
    prot = [n.protein_g for n in comp if n.protein_g is not None]
    if len(prot) >= 2:
        out.append(MetricValue(f"protein_mean_{window}d", 1, "global", start, end, float(np.mean(prot)), "g/day",
                               "MEASUREMENT", len(prot), dq=len(prot) / window))
    return out


def mifflin(weight_kg: float, a: AthleteBasics) -> float | None:
    if None in (a.height_cm, a.age_years, a.sex) or a.sex not in ("male", "female"):
        return None
    return 10 * weight_kg + 6.25 * a.height_cm - 5 * a.age_years + (5 if a.sex == "male" else -161)


def adaptive_tdee(
    days: list[NutritionDay], daily_weight: dict[date, float], athlete: AthleteBasics, end: date,
    fallback_weight_kg: float | None = None,
) -> MetricValue | None:
    window = p("tdee_window_days")
    start = end - timedelta(days=window - 1)
    level = p("rate_ci_level")
    z = stats.norm.ppf(0.5 + level / 2)
    in_window = [w for d, w in daily_weight.items() if start <= d <= end]
    # only weights known on or before `end`: a later weigh-in must never inform an earlier period
    past = {d: w for d, w in daily_weight.items() if d <= end}
    ref_weight = float(np.mean(in_window)) if in_window else (past[max(past)] if past else fallback_weight_kg)
    detail: dict = {"window_days": window, "ci_level": level}
    if ref_weight is None:
        return None
    if not in_window:
        detail["prior_weight_source"] = "reported_or_last_known"

    # prior
    ree = mifflin(ref_weight, athlete)
    factor = athlete.activity_factor or p("prior_activity_factor_default")
    prior = prior_var = None
    if ree is not None:
        prior = ree * factor
        prior_var = (p("prior_relative_sd") * prior) ** 2
        detail.update(prior_kcal=prior, prior_ree=ree, activity_factor=factor,
                      activity_factor_source="athlete" if athlete.activity_factor else "default_expert_opinion")

    # observed
    comp = _complete(days, start, end)
    obs = obs_var = None
    slope = ols_slope_per_day(daily_weight, end, window)
    detail.update(complete_intake_days=len(comp), weight_days=len(in_window))
    if (len(comp) / window >= p("tdee_min_intake_completeness")
            and len(in_window) / window >= p("tdee_min_weight_days") and slope is not None):
        k = np.array([n.kcal for n in comp])
        rho, rho_sd = p("tissue_energy_density"), sd("tissue_energy_density")
        b, b_se, _ = slope
        obs = float(k.mean() - b * rho)
        obs_var = float(k.var(ddof=1) / len(k) + (rho * b_se) ** 2 + (b * rho_sd) ** 2)
        detail.update(observed_kcal=obs, observed_sd=obs_var ** 0.5, intake_mean=float(k.mean()),
                      slope_kg_per_day=b, tissue_energy_density=rho)

    if prior is None and obs is None:
        return None
    if obs is None:
        value, var, method = prior, prior_var, "prior_only"
    elif prior is None:
        value, var, method = obs, obs_var, "observed_only"
    else:
        w_obs, w_prior = 1 / obs_var, 1 / prior_var
        value = (obs * w_obs + prior * w_prior) / (w_obs + w_prior)
        var = 1 / (w_obs + w_prior)
        method = "adaptive"
        detail["weight_of_observed"] = w_obs / (w_obs + w_prior)
    detail["method"] = method
    s = var ** 0.5
    dq = min(len(comp) / window, len(in_window) / window)
    return MetricValue("adaptive_tdee", 1, "global", start, end, value, "kcal/day", "ESTIMATE",
                       len(comp) + len(in_window), lo=value - z * s, hi=value + z * s, dq=dq, detail=detail)
