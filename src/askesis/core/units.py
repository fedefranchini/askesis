"""Canonical units and explicit conversions (docs/architecture.md §3).

Canonical: mass kg · distance m · circumference/height cm · duration s · energy kcal · HR bpm · HRV ms.
Pace is never stored: it is derived from distance and duration.
"""

from __future__ import annotations

import re

LB_PER_KG = 2.2046226218
CM_PER_IN = 2.54
KJ_PER_KCAL = 4.184

_MASS = {"kg": 1.0, "g": 0.001, "lb": 1 / LB_PER_KG}
_LENGTH_CM = {"cm": 1.0, "mm": 0.1, "m": 100.0, "in": CM_PER_IN}
_DISTANCE_M = {"m": 1.0, "km": 1000.0, "mi": 1609.344}
_ENERGY_KCAL = {"kcal": 1.0, "kj": 1 / KJ_PER_KCAL}


class UnitError(ValueError):
    pass


def _convert(value: float, unit: str, table: dict[str, float], kind: str) -> float:
    try:
        return value * table[unit.lower()]
    except KeyError as exc:
        raise UnitError(f"unità di {kind} non supportata: {unit!r}") from exc


def mass_kg(value: float, unit: str = "kg") -> float:
    return _convert(value, unit, _MASS, "massa")


def length_cm(value: float, unit: str = "cm") -> float:
    return _convert(value, unit, _LENGTH_CM, "lunghezza")


def distance_m(value: float, unit: str = "m") -> float:
    return _convert(value, unit, _DISTANCE_M, "distanza")


def energy_kcal(value: float, unit: str = "kcal") -> float:
    return _convert(value, unit, _ENERGY_KCAL, "energia")


_DURATION = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")


def parse_duration_s(value: str) -> int:
    """Parse 'mm:ss' or 'h:mm:ss' (also '45m', '1h05m', '90s') into seconds."""
    v = value.strip().lower()
    m = _DURATION.match(v)
    if m:
        h = int(m.group(1) or 0)
        mi, s = int(m.group(2)), int(m.group(3))
        if s >= 60 or (m.group(1) and mi >= 60):
            raise UnitError(f"durata non valida: {value!r}")
        return h * 3600 + mi * 60 + s
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m(?:in)?)?(?:(\d+)s)?", v)
    if m and any(m.groups()):
        h, mi, s = (int(g or 0) for g in m.groups())
        return h * 3600 + mi * 60 + s
    raise UnitError(f"durata non valida: {value!r}")


def format_duration(seconds: int) -> str:
    h, rem = divmod(int(round(seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def pace_s_per_km(distance_m_: float, duration_s: float) -> float:
    if distance_m_ <= 0:
        raise UnitError("distanza nulla: passo non definito")
    return duration_s / (distance_m_ / 1000.0)
