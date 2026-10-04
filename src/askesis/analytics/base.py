"""Common types for derived metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class MetricValue:
    metric_id: str
    version: int
    subject: str
    period_start: date
    period_end: date
    value: float | None
    unit: str
    epistemic: str  # MEASUREMENT | ESTIMATE | INFERENCE | FACT
    n_obs: int
    lo: float | None = None
    hi: float | None = None
    dq: float | None = None
    detail: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def ref(self) -> str:
        return f"{self.metric_id}@{self.version}"

    def key(self) -> tuple:
        return (self.metric_id, self.subject, self.period_start, self.period_end)


def r6(x: float | None) -> float | None:
    return None if x is None else round(float(x), 6)
