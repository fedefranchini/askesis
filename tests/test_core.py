from datetime import UTC, date, datetime, time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from askesis.core import ids, timeutil, units

ZONE = "Europe/Berlin"


def test_uuid7_version_variant_and_order():
    a, b = ids.uuid7(1_700_000_000_000), ids.uuid7(1_700_000_000_001)
    assert a.version == 7 and a.variant == "specified in RFC 4122"
    assert str(a) < str(b)


def test_naive_local_to_utc_across_dst():
    # 2026-03-29: Europe/Berlin switches to CEST (+02:00) at 02:00 local
    winter = timeutil.to_utc(datetime(2026, 3, 28, 8, 0), ZONE)
    summer = timeutil.to_utc(datetime(2026, 3, 30, 8, 0), ZONE)
    assert winter.hour == 7 and summer.hour == 6


def test_nutrition_cutoff_assigns_late_meal_to_previous_day():
    late = timeutil.at_local(date(2026, 10, 5), time(1, 30), ZONE)
    assert timeutil.nutrition_date(late, ZONE, time(3, 0)) == date(2026, 10, 4)
    morning = timeutil.at_local(date(2026, 10, 5), time(8, 0), ZONE)
    assert timeutil.nutrition_date(morning, ZONE, time(3, 0)) == date(2026, 10, 5)


def test_local_date_differs_from_utc_date():
    instant = datetime(2026, 10, 4, 23, 30, tzinfo=UTC)  # 01:30 local on the 5th
    assert timeutil.local_date(instant, ZONE) == date(2026, 10, 5)


@given(st.floats(min_value=0.1, max_value=500, allow_nan=False))
def test_mass_roundtrip_lb(kg):
    assert units.mass_kg(kg * units.LB_PER_KG, "lb") == pytest.approx(kg)


@pytest.mark.parametrize(
    "text,seconds",
    [("31:40", 1900), ("1:05:00", 3900), ("45m", 2700), ("1h05m", 3900), ("90s", 90)],
)
def test_parse_duration(text, seconds):
    assert units.parse_duration_s(text) == seconds


@given(st.integers(min_value=0, max_value=10 * 3600))
def test_duration_format_roundtrip(seconds):
    assert units.parse_duration_s(units.format_duration(seconds)) == seconds


@pytest.mark.parametrize("bad", ["", "abc", "10:75", "1:61:00"])
def test_parse_duration_rejects(bad):
    with pytest.raises(units.UnitError):
        units.parse_duration_s(bad)


def test_unknown_unit_rejected():
    with pytest.raises(units.UnitError):
        units.mass_kg(10, "stone")


def test_pace():
    assert units.pace_s_per_km(5000, 1500) == 300
