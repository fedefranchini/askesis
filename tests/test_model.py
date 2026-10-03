from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from askesis.model.entities import Envelope
from askesis.model.schemas import export

NOW = datetime(2026, 1, 10, 7, 0, tzinfo=UTC)


def env(**kw):
    base = dict(
        id="r1",
        entity_type="body_weight",
        occurred_at=NOW,
        tz="Europe/Berlin",
        local_date=date(2026, 1, 10),
        recorded_at=NOW,
        source_id="manual_cli",
        payload={"value_kg": 75.0, "fasted": True},
    )
    base.update(kw)
    return Envelope(**base)


def test_valid_weight():
    assert env().payload == {"value_kg": 75.0, "fasted": True}


def test_unknown_entity_rejected():
    with pytest.raises(ValidationError):
        env(entity_type="mystery")


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError):
        env(occurred_at=datetime(2026, 1, 10, 7, 0))


def test_payload_extra_field_rejected_for_strict_entity():
    with pytest.raises(ValidationError):
        env(payload={"value_kg": 75.0, "weight": 1})


def test_interval_requires_start():
    with pytest.raises(ValidationError):
        env(entity_type="running_session", payload={"distance_m": 5000, "elapsed_s": 1800})


def test_nutrition_requires_energy_unless_not_logged():
    with pytest.raises(ValidationError):
        env(entity_type="nutrition_day", payload={"completeness": "complete"})
    ok = env(entity_type="nutrition_day", payload={"completeness": "not_logged"})
    assert ok.payload == {"completeness": "not_logged"}


def test_flexible_entity_keeps_extra_fields():
    e = env(entity_type="athlete_attribute", payload={"key": "k", "value": {"a": 1}, "extra": True})
    assert e.payload["extra"] is True


def test_schema_export(tmp_path):
    files = export(tmp_path)
    assert (tmp_path / "envelope.schema.json") in files
    assert len(files) > 10
