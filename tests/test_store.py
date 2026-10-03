import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from conftest import weight

from askesis.core.ids import new_id
from askesis.ingestion.pipeline import ingest
from askesis.store import repository as repo
from askesis.store.db import migrate


def test_migrations_idempotent(conn):
    assert migrate(conn) == []


def test_insert_and_current(conn):
    r = ingest(conn, [weight(75.2, "2026-01-10")], "test")
    assert len(r.inserted) == 1
    assert [row["value_kg"] for row in conn.execute("SELECT * FROM v_body_weight")] == [75.2]


def test_raw_is_append_only(conn):
    rec = weight(75.2, "2026-01-10")
    ingest(conn, [rec], "test")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE raw_record SET notes='x' WHERE id=?", (rec["id"],))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM raw_record WHERE id=?", (rec["id"],))


def test_supersede_keeps_history(conn):
    old = weight(75.2, "2026-01-10")
    ingest(conn, [old], "test")
    new = weight(75.8, "2026-01-10", supersedes_id=old["id"])
    ingest(conn, [new], "test")
    values = [row["value_kg"] for row in conn.execute("SELECT * FROM v_body_weight")]
    assert values == [75.8]
    assert repo.get(conn, old["id"]) is not None  # still stored


def test_cannot_supersede_twice(conn):
    old = weight(75.2, "2026-01-10")
    ingest(conn, [old], "test")
    ingest(conn, [weight(75.8, "2026-01-10", supersedes_id=old["id"])], "test")
    r = ingest(conn, [weight(75.9, "2026-01-10", supersedes_id=old["id"])], "test")
    assert r.rejected and "non corrente" in r.rejected[0][1]


def test_retraction(conn):
    rec = weight(75.2, "2026-01-10")
    ingest(conn, [rec], "test")
    repo.retract(conn, rec["id"], "inserito per errore")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM v_body_weight").fetchone()[0] == 0
    with pytest.raises(ValueError):
        repo.retract(conn, rec["id"], "di nuovo")


def test_ingest_is_idempotent_by_source_record_id(conn):
    rec = weight(75.2, "2026-01-10", source_record_id="ext-1")
    ingest(conn, [rec], "test")
    again = dict(rec, id=new_id())
    r = ingest(conn, [again], "test")
    assert r.duplicates and not r.inserted


def test_ingest_is_idempotent_by_content(conn):
    rec = weight(75.2, "2026-01-10")
    ingest(conn, [rec], "test")
    r = ingest(conn, [dict(rec, id=new_id())], "test")
    assert r.duplicates and not r.inserted


def test_hard_dq_rejects_and_keeps_audit(conn):
    r = ingest(conn, [weight(750, "2026-01-10")], "test")
    assert r.rejected and not r.inserted
    assert conn.execute("SELECT COUNT(*) FROM rejected_record").fetchone()[0] == 1


def test_invalid_schema_rejected_not_silently_dropped(conn):
    bad = weight(75, "2026-01-10")
    bad["payload"] = {"value_kg": "settantacinque"}
    r = ingest(conn, [bad], "test")
    assert r.rejected
    assert conn.execute("SELECT COUNT(*) FROM rejected_record").fetchone()[0] == 1


def test_soft_dq_weight_jump(conn):
    ingest(conn, [weight(75.0, "2026-01-10")], "test")
    r = ingest(conn, [weight(78.0, "2026-01-11")], "test")
    assert r.inserted and any("variazione" in m for _, _, m in r.issues)


def test_as_of_reconstructs_past_knowledge(conn):
    t0 = datetime(2026, 1, 10, 6, 0, tzinfo=UTC)
    old = weight(75.2, "2026-01-10", recorded_at=t0)
    ingest(conn, [old], "test")
    new = weight(75.8, "2026-01-10", supersedes_id=old["id"], recorded_at=t0 + timedelta(days=2))
    ingest(conn, [new], "test")
    before = repo.current(conn, "body_weight", as_of=t0 + timedelta(days=1))
    assert [r["id"] for r in before] == [old["id"]]
    after = repo.current(conn, "body_weight")
    assert [r["id"] for r in after] == [new["id"]]


def test_batch_is_atomic_on_error(conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk")

    monkeypatch.setattr(repo, "insert", boom)
    with pytest.raises(RuntimeError):
        ingest(conn, [weight(75.2, "2026-01-10")], "test")
    assert conn.execute("SELECT COUNT(*) FROM ingestion_batch").fetchone()[0] == 0
