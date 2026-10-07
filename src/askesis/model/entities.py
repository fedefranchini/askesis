"""Canonical record model (docs/architecture.md §4) — MVP subset, schema 0.1.

Every RAW record is an Envelope (provenance + bitemporal time) carrying an entity-specific payload.

Time conventions by entity kind:
- instant:  `occurred_at` is when it happened (e.g. a weigh-in);
- interval: `start_at` (+ optional `end_at`), e.g. a training or running session;
- daily:    the authoritative key is `local_date`; `occurred_at` is set to local noon of that date.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "0.1"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Open(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------- body
class BodyWeight(Strict):
    value_kg: float
    fasted: bool | None = None
    post_void: bool | None = None
    clothing: str | None = None
    method: str | None = None


class BodyMeasurement(Strict):
    site: str  # e.g. waist_navel, hip, chest, arm_left …
    readings_cm: list[float] = Field(min_length=1)
    protocol_id: str | None = None


# ---------------------------------------------------------------- nutrition
class Completeness(StrEnum):
    complete = "complete"
    partial = "partial"
    estimated = "estimated"
    not_logged = "not_logged"


class NutritionDay(Strict):
    energy_kcal: float | None = None
    protein_g: float | None = None
    carbohydrate_g: float | None = None
    fat_g: float | None = None
    fiber_g: float | None = None
    alcohol_g: float | None = None
    completeness: Completeness
    logging_method: str | None = None
    free_meal_estimate_kcal: float | None = None  # part of energy_kcal estimated by the athlete (meal not in the app)
    free_meal_estimate_protein_g: float | None = None

    @model_validator(mode="after")
    def _values_required_unless_not_logged(self) -> NutritionDay:
        if self.completeness != Completeness.not_logged and self.energy_kcal is None:
            raise ValueError("energy_kcal obbligatorio salvo completeness=not_logged")
        return self


# ---------------------------------------------------------------- strength
class SetType(StrEnum):
    warmup = "warmup"
    working = "working"
    top = "top"
    backoff = "backoff"
    drop = "drop"
    amrap = "amrap"
    myo = "myo"
    cluster = "cluster"


class TrainingSession(Strict):
    session_kind: str = "strength"
    session_rpe: float | None = Field(default=None, ge=0, le=10)
    location: str | None = None
    planned_session_id: str | None = None


class SetRecord(Strict):
    session_id: str
    exercise_raw: str
    exercise_id: str | None = None
    sequence: int = Field(ge=1)
    set_type: SetType = SetType.working
    load_kg: float
    load_kind: Literal["external", "bodyweight", "bodyweight_plus", "assisted"] = "external"
    reps: int
    reps_target: int | None = None
    rpe: float | None = None
    rir: float | None = None
    to_failure: bool | None = None
    rom: str | None = None
    side: str | None = None
    pain: float | None = None
    pain_region: str | None = None
    completed: bool = True


# ---------------------------------------------------------------- running
class RunningSession(Strict):
    distance_m: float
    elapsed_s: int
    moving_s: int | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    avg_cadence: int | None = None
    elev_gain_m: float | None = None
    environment: str | None = None
    run_type: str | None = None
    session_rpe: float | None = Field(default=None, ge=0, le=10)
    stop_reason: str | None = None


# ---------------------------------------------------------------- daily signals
class DailyActivity(Strict):
    steps: int


class SleepSession(Strict):
    asleep_s: int | None = None
    in_bed_s: int | None = None


class RestingHrDaily(Strict):
    bpm: int
    method: str | None = None


class SubjectiveCheckin(Strict):
    fatigue_1_5: int | None = Field(default=None, ge=1, le=5)
    soreness_1_5: int | None = Field(default=None, ge=1, le=5)
    stress_1_5: int | None = Field(default=None, ge=1, le=5)
    mood_1_5: int | None = Field(default=None, ge=1, le=5)
    sleep_quality_1_5: int | None = Field(default=None, ge=1, le=5)
    readiness_1_10: int | None = Field(default=None, ge=1, le=10)
    illness: bool | None = None
    pain: list[dict[str, Any]] | None = None  # [{"region": str, "score_0_10": float}]
    # Daily questionnaire (askesis.checkin): every scale measures "how much" of its item; direction is in ITEMS.
    moment: Literal["morning", "post_session"] | None = None
    sleep_quality_1_10: int | None = Field(default=None, ge=1, le=10)
    fatigue_1_10: int | None = Field(default=None, ge=1, le=10)
    soreness_1_10: int | None = Field(default=None, ge=1, le=10)
    stress_1_10: int | None = Field(default=None, ge=1, le=10)
    mood_1_10: int | None = Field(default=None, ge=1, le=10)
    hunger_1_10: int | None = Field(default=None, ge=1, le=10)
    motivation_1_10: int | None = Field(default=None, ge=1, le=10)
    session_kind: Literal["strength", "run"] | None = None
    session_rpe_cr10: int | None = Field(default=None, ge=0, le=10)
    session_quality_1_10: int | None = Field(default=None, ge=1, le=10)
    session_minutes: int | None = Field(default=None, ge=1, le=600)

    @model_validator(mode="after")
    def _moment_fields(self):
        morning = ("sleep_quality_1_10", "fatigue_1_10", "soreness_1_10", "stress_1_10", "mood_1_10", "hunger_1_10",
                   "motivation_1_10")
        post = ("session_kind", "session_rpe_cr10", "session_quality_1_10", "session_minutes")
        has = lambda names: any(getattr(self, n) is not None for n in names)  # noqa: E731
        if has(morning) and self.moment != "morning":
            raise ValueError("le voci del mattino richiedono moment = morning")
        if has(post) and self.moment != "post_session":
            raise ValueError("le voci della seduta richiedono moment = post_session")
        if self.moment == "post_session" and self.session_kind is None:
            raise ValueError("check-in dopo la seduta senza tipo di seduta")
        return self


class DailyContext(Strict):
    """Generic, user-defined context variable. Keys and aliases live in private configuration."""

    key: str
    value: float | int | str | bool


# ---------------------------------------------------------------- athlete & context (flexible)
class AthleteAttribute(Open):
    key: str
    value: Any
    valid_from: date | None = None


class Goal(Open):
    domain: str
    priority_rank: int
    description: str
    status: str = "active"
    valid_from: date | None = None


class HealthEvent(Open):
    kind: Literal["injury", "illness", "symptom"]
    body_region: str | None = None
    description: str | None = None
    status: str | None = None


class ContextEvent(Open):
    kind: str
    description: str | None = None
    start: date | None = None
    end: date | None = None
    approximate: bool | None = None


class ExercisePreference(Open):
    exercise_raw: str
    status: Literal["YES", "SUB", "NO"]
    reason: str | None = None


class TestResult(Open):
    test_kind: str


class ReportedSummary(Open):
    """A summary reported by the athlete (e.g. an app's weekly report). Not a measurement."""

    source: str


class DecisionLog(Open):
    type: str
    status: str
    athlete_response_verbatim: str | None = None
    scope: str | None = None


# ---------------------------------------------------------------- registry
EntityKind = Literal["instant", "interval", "daily"]

ENTITIES: dict[str, tuple[type[BaseModel], EntityKind]] = {
    "body_weight": (BodyWeight, "instant"),
    "body_measurement": (BodyMeasurement, "instant"),
    "nutrition_day": (NutritionDay, "daily"),
    "training_session": (TrainingSession, "interval"),
    "set_record": (SetRecord, "interval"),
    "running_session": (RunningSession, "interval"),
    "daily_activity": (DailyActivity, "daily"),
    "sleep_session": (SleepSession, "interval"),
    "resting_hr_daily": (RestingHrDaily, "daily"),
    "subjective_checkin": (SubjectiveCheckin, "daily"),
    "daily_context": (DailyContext, "daily"),
    "athlete_attribute": (AthleteAttribute, "instant"),
    "goal": (Goal, "instant"),
    "health_event": (HealthEvent, "instant"),
    "context_event": (ContextEvent, "instant"),
    "athlete_exercise_preference": (ExercisePreference, "instant"),
    "test_result": (TestResult, "instant"),
    "reported_summary": (ReportedSummary, "instant"),
    "decision_log": (DecisionLog, "instant"),
}

ENTRY_METHODS = ("manual", "imported", "synced", "device_estimated")


class Envelope(Strict):
    id: str
    entity_type: str
    schema_version: str = SCHEMA_VERSION
    occurred_at: datetime | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    tz: str
    local_date: date
    recorded_at: datetime
    source_id: str
    source_record_id: str | None = None
    device_id: str | None = None
    entry_method: Literal["manual", "imported", "synced", "device_estimated"] = "manual"
    supersedes_id: str | None = None
    missing_reason: str | None = None
    payload: dict[str, Any]
    original_values: dict[str, Any] | None = None
    notes: str | None = None

    @field_validator("entity_type")
    @classmethod
    def _known_entity(cls, v: str) -> str:
        if v not in ENTITIES:
            raise ValueError(f"entity_type sconosciuto: {v!r}")
        return v

    @field_validator("occurred_at", "start_at", "end_at", "recorded_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("i timestamp devono avere fuso/offset")
        return v

    @model_validator(mode="after")
    def _validate_time_and_payload(self) -> Envelope:
        model, kind = ENTITIES[self.entity_type]
        if kind == "interval" and self.start_at is None:
            raise ValueError(f"{self.entity_type}: start_at obbligatorio")
        if kind != "interval" and self.occurred_at is None:
            raise ValueError(f"{self.entity_type}: occurred_at obbligatorio")
        if self.start_at and self.end_at and self.end_at < self.start_at:
            raise ValueError("end_at precedente a start_at")
        self.payload = model.model_validate(self.payload).model_dump(mode="json", exclude_none=True)
        return self


def payload_model(entity_type: str) -> type[BaseModel]:
    return ENTITIES[entity_type][0]
