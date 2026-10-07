from datetime import UTC, date, datetime

import pytest

from askesis.core.ids import new_id
from askesis.store.db import connect

ZONE = "Europe/Berlin"


@pytest.fixture(autouse=True)
def _synthetic_reports(tmp_path, monkeypatch):
    """Tests never read the real reports/ (private texts): each test gets its own empty folder."""
    monkeypatch.setenv("ASKESIS_REPORTS_DIR", str(tmp_path / "reports"))


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def weight(value, day, source="manual_cli", **kw):
    """Synthetic body-weight envelope."""
    d = date.fromisoformat(day)
    t = datetime(d.year, d.month, d.day, 6, 0, tzinfo=UTC)
    rec = dict(
        id=new_id(), entity_type="body_weight", occurred_at=t, tz=ZONE, local_date=d, recorded_at=t,
        source_id=source, payload={"value_kg": value, "fasted": True},
    )
    rec.update(kw)
    return rec
