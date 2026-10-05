"""Body-weight and circumference metrics (docs/architecture.md §5.2). Pure functions, no I/O."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
from scipy import stats

from .base import MetricValue
from .params import p

# ------------------------------------------------------------------ daily weight


def daily_weights(weighins: list[tuple[date, str, float, bool | None]]) -> dict[date, float]:
    """weighins: (local_date, occurred_at_iso, kg, fasted). Rule: first fasted, otherwise first of the day."""
    by_day: dict[date, list] = {}
    for d, when, kg, fasted in weighins:
        by_day.setdefault(d, []).append((0 if fasted else 1, when, kg))
    return {d: sorted(v)[0][2] for d, v in sorted(by_day.items())}


def weight_daily(daily: dict[date, float]) -> list[MetricValue]:
    return [MetricValue("weight_daily", 1, "global", d, d, kg, "kg", "MEASUREMENT", 1) for d, kg in daily.items()]


def weight_ma7(daily: dict[date, float], end: date) -> MetricValue | None:
    start = end - timedelta(days=6)
    vals = [v for d, v in daily.items() if start <= d <= end]
    if len(vals) < p("weight_ma7_min_points"):
        return None
    return MetricValue("weight_ma7", 1, "global", start, end, float(np.mean(vals)), "kg", "ESTIMATE", len(vals),
                       dq=len(vals) / 7)


def weight_ema_series(daily: dict[date, float]) -> dict[date, float]:
    """Gap-aware EMA: alpha_eff = 1 - (1 - alpha)^Δdays."""
    alpha = p("weight_ema_alpha")
    out, ema, last = {}, None, None
    for d, w in sorted(daily.items()):
        if ema is None:
            ema = w
        else:
            a = 1 - (1 - alpha) ** (d - last).days
            ema = ema + a * (w - ema)
        out[d], last = ema, d
    return out


def weight_ema(daily: dict[date, float], end: date) -> MetricValue | None:
    series = {d: v for d, v in weight_ema_series(daily).items() if d <= end}
    if not series:
        return None
    last_day = max(series)
    return MetricValue("weight_ema", 1, "global", min(series), end, series[last_day], "kg", "ESTIMATE",
                       len(series), detail={"last_weighin": last_day.isoformat()})


def _window(daily: dict[date, float], end: date, days: int) -> tuple[np.ndarray, np.ndarray]:
    start = end - timedelta(days=days - 1)
    pts = sorted((d, v) for d, v in daily.items() if start <= d <= end)
    x = np.array([(d - start).days for d, _ in pts], dtype=float)
    y = np.array([v for _, v in pts], dtype=float)
    return x, y


def weight_rate(daily: dict[date, float], end: date, days: int) -> list[MetricValue]:
    """Theil–Sen slope (robust) with CI, in kg/week and %BW/week. Also returns the OLS slope for reference."""
    x, y = _window(daily, end, days)
    if len(x) < p("weight_rate_min_points")[str(days)] or np.ptp(x) == 0:
        return []
    level = p("rate_ci_level")
    ts = stats.theilslopes(y, x, alpha=level)
    ols = stats.linregress(x, y)
    mean_w = float(np.mean(y))
    start = end - timedelta(days=days - 1)
    dq = len(x) / days
    kg = MetricValue(f"weight_rate_{days}d", 1, "global", start, end, ts.slope * 7, "kg/week", "ESTIMATE", len(x),
                     lo=ts.low_slope * 7, hi=ts.high_slope * 7, dq=dq,
                     detail={"method": "theil_sen", "ols_kg_per_week": ols.slope * 7, "ols_se": ols.stderr * 7,
                             "ci_level": level})
    pct = MetricValue(f"weight_rate_pct_{days}d", 1, "global", start, end, ts.slope * 7 / mean_w * 100,
                      "%BW/week", "ESTIMATE", len(x), lo=ts.low_slope * 7 / mean_w * 100,
                      hi=ts.high_slope * 7 / mean_w * 100, dq=dq, detail={"reference_weight_kg": mean_w})
    return [kg, pct]


def ols_slope_per_day(daily: dict[date, float], end: date, days: int) -> tuple[float, float, int] | None:
    """(slope kg/day, standard error, n) by OLS, used for energy-balance propagation."""
    x, y = _window(daily, end, days)
    if len(x) < 3 or np.ptp(x) == 0:
        return None
    r = stats.linregress(x, y)
    return float(r.slope), float(r.stderr), len(x)


# ------------------------------------------------------------------ circumference


def waist_sessions(measurements: list[tuple[date, list[float]]]) -> list[MetricValue]:
    """Median of repeated readings per session; spread is reported as within-session range."""
    out = []
    for d, readings in sorted(measurements):
        out.append(MetricValue("waist_session", 1, "global", d, d, float(np.median(readings)), "cm", "MEASUREMENT",
                               len(readings), lo=min(readings), hi=max(readings)))
    return out


def weight_noise_sd(daily: dict[date, float], end: date, days: int = 28) -> tuple[float, int] | None:
    """Day-to-day scale noise: SD of daily weights around the EMA trend in the last `days` (data ≤ end only).
    None when there are fewer points than the 28-day rate needs (weight_rate_min_points)."""
    past = {d: w for d, w in daily.items() if d <= end}
    ema = weight_ema_series(past)
    res = [w - ema[d] for d, w in past.items() if end - timedelta(days=days - 1) <= d]
    if len(res) < p("weight_rate_min_points")[str(days)]:
        return None
    return float(np.std(res, ddof=1)), len(res)
