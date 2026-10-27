"""Plan content schemas (docs/architecture.md §4.8). Prescriptions, not observations."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: list[str] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def last_set(lift: dict) -> str:
    """Suffix for the RIR of a prescription whose last working set has its own target (", ultima a cedimento")."""
    v = lift.get("last_set_rir")
    if v is None:
        return ""
    return ", ultima a cedimento" if v == 0 else f", ultima RIR {v:g}"


def planned_weekday(on: date, periods: list[dict]) -> str:
    """Microcycle day whose sessions fall on `on`: a scheduled period with `day_offset: n` runs the week n days late
    (on `on` comes the session planned for `on - n`); one with `day_map` names, for some weekdays, the microcycle day
    to do instead (e.g. {"wed": "thu", "thu": "fri", "fri": "rest"}; "rest" = nothing planned that day)."""
    own = WEEKDAYS[on.weekday()]
    for pp in periods:
        mapped = pp.get("modifiers", {}).get("day_map", {}).get(own)
        if mapped:
            return mapped
    offset = sum(int(pp.get("modifiers", {}).get("day_offset", 0)) for pp in periods)
    return WEEKDAYS[(on - timedelta(days=offset)).weekday()]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LiftItem(_M):
    exercise: str  # catalog id (preferred) or free text
    sets: int = Field(ge=1, le=10)
    rep_range: tuple[int, int]
    target_rir: float = Field(ge=0, le=5)
    last_set_rir: float | None = Field(default=None, ge=0, le=5)  # last working set (e.g. 0 = to failure)
    note: str | None = None

    @model_validator(mode="after")
    def _range(self) -> LiftItem:
        if not 1 <= self.rep_range[0] <= self.rep_range[1] <= 30:
            raise ValueError("rep_range non valido")
        return self


class RunItem(_M):
    kind: str  # easy | long | intervals | test | …
    duration_min: float | None = None
    distance_km: float | None = None
    intensity: str = "talk_test"  # how intensity is controlled
    note: str | None = None


class PlannedSession(_M):
    day: Weekday
    name: str
    lifts: list[LiftItem] = []
    run: RunItem | None = None


class PainGate(_M):
    """Hold load progression on exercises of some movement patterns after pain in a body region is logged.

    Below the safety threshold (safety layer) pain does not stop the session, but it must not be loaded further.
    Regions are matched case-insensitively as substrings of the logged region or exercise."""

    patterns: list[str] = Field(min_length=1)  # catalog patterns, e.g. knee_dominant
    regions: list[str] = Field(min_length=1)  # logged region keywords
    max_score: float = Field(ge=0, le=10)  # progression allowed only if every logged score is ≤ this
    lookback_days: int = Field(ge=1, le=28)


class Programme(_M):
    microcycle: list[PlannedSession]
    minimal_week: list[PlannedSession] = Field(min_length=1)  # fallback for difficult weeks
    progression_rule: str = "double_progression@1"
    deload_weeks: list[date] = []  # Monday of each pre-planned deload week (L1)
    pain_gate: PainGate | None = None
    constraints: dict = {}


class NutritionTarget(_M):
    energy_kcal: float
    energy_kcal_interval: tuple[float, float] | None = None
    protein_g: float
    protein_basis: dict  # e.g. {"method": "per_kg_ffm_estimate", "g_per_kg": 2.3, "ffm_kg": …, "uncertainty": …}
    steps_target: int | None = None
    method: str  # prior_only | adaptive_tdee
    claims: list[str] = []


class ExitCriterion(_M):
    description: str
    metric_id: str | None = None
    operator: Literal["<=", ">=", "=="] | None = None
    value: float | None = None


class Phase(_M):
    phase: str  # fat_loss | recomposition | maintenance | muscle_gain | …
    exit_criteria: list[ExitCriterion] = Field(min_length=1)
    max_duration_weeks: int = Field(ge=1, le=52)
    next_phase: str
    notes: str | None = None


class ScheduledPeriod(_M):
    """A pre-planned period with modified load (e.g. exam sessions), decided in advance."""

    label: str
    start: date
    end: date
    modifiers: dict  # e.g. {"energy": "maintenance", "volume_factor": 0.6, "use_minimal_week": true, "day_offset": 1}


SCHEMAS: dict[str, type[BaseModel]] = {
    "programme": Programme,
    "nutrition_target": NutritionTarget,
    "phase": Phase,
    "scheduled_period": ScheduledPeriod,
}
