"""Rest between working sets, as written on the gym sheet and the Plan page.

Ranges come from versioned parameters with an evidence basis (rest_compound_range_s, rest_isolation_range_s). An
exercise missing from the catalog gets the longer, multi-joint range.
"""

from __future__ import annotations

from askesis.analytics.params import p
from askesis.reference import find_exercise

CLAIMS = ("rt.rest_strength", "rt.rest_hypertrophy")


def rest_range(exercise: str) -> tuple[int, int]:
    ref = find_exercise(exercise)
    lo, hi = p("rest_isolation_range_s") if ref is not None and not ref.get("compound", True) \
        else p("rest_compound_range_s")
    return int(lo), int(hi)


def _min(s: int) -> str:
    return f"{s / 60:g}".replace(".", ",")


def rest_label(exercise: str) -> str:
    lo, hi = rest_range(exercise)
    return f"recupero {_min(lo)}–{_min(hi)} min"
