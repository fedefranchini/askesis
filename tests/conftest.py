from datetime import UTC, date, datetime

import pytest

from askesis.core.ids import new_id
from askesis.store.db import connect

ZONE = "Europe/Berlin"


@pytest.fixture(autouse=True)
def _synthetic_reports(tmp_path, monkeypatch):
    """Tests never read the real reports/ (private texts): each test gets its own empty folder."""
    monkeypatch.setenv("ASKESIS_REPORTS_DIR", str(tmp_path / "reports"))


@pytest.fixture(autouse=True)
def _never_the_real_configuration_or_database(tmp_path, monkeypatch):
    """Tests never read the private configuration (it points to the hosted database with the real data) and can
    never open a hosted database: a test that needs one replaces `remote.connect` with a local libSQL file."""
    from askesis.store import remote, state

    monkeypatch.setenv("ASKESIS_CONFIG", str(tmp_path / "no-private-config.toml"))
    for var in ("ASKESIS_DB_URL", "ASKESIS_DB_TOKEN", "ASKESIS_DB_TOKEN_SOURCE", "ASKESIS_CONFIG_TOML",
                "ASKESIS_WEB_ORIGIN"):
        monkeypatch.delenv(var, raising=False)

    def refuse(url, token):
        raise RuntimeError(f"test: connessione a un database online rifiutata ({url})")

    monkeypatch.setattr(remote, "connect", refuse)
    yield
    state.use_database(None)


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
