"""Ingestion pipeline: validate → hard DQ → dedup → commit RAW → soft DQ (docs/architecture.md §3)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from pydantic import ValidationError

from askesis.core.ids import new_id
from askesis.core.timeutil import iso, now_utc
from askesis.model.entities import Envelope
from askesis.quality.rules import hard_checks, soft_checks
from askesis.store import repository as repo


@dataclass
class Receipt:
    batch_id: str
    inserted: list[Envelope] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (input summary, reason)
    issues: list[tuple[str, str, str]] = field(default_factory=list)  # (record id, severity, message)

    def summary(self) -> str:
        return (f"inseriti {len(self.inserted)} · duplicati {len(self.duplicates)} · "
                f"rifiutati {len(self.rejected)} · segnalazioni {len(self.issues)}")


def _issue(conn, batch_id, record_id, issue) -> None:
    conn.execute(
        """INSERT INTO dq_issue(id, record_id, ingestion_batch_id, rule, severity, message, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (new_id(), record_id, batch_id, issue.rule, issue.severity, issue.message, iso(now_utc())),
    )


def ingest(
    conn: sqlite3.Connection,
    items: list[dict],
    adapter: str,
    input_ref: str | None = None,
    source_kind: str = "manual",
) -> Receipt:
    """Ingest envelope dicts atomically (one transaction per batch)."""
    batch_id = new_id()
    receipt = Receipt(batch_id)
    with conn:
        conn.execute(
            "INSERT INTO ingestion_batch(id, adapter, input_ref, started_at) VALUES (?,?,?,?)",
            (batch_id, adapter, input_ref, iso(now_utc())),
        )
        for raw in items:
            label = f"{raw.get('entity_type')}:{raw.get('source_record_id') or raw.get('id')}"
            try:
                env = Envelope.model_validate(raw)
            except ValidationError as exc:
                reason = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
                _reject(conn, batch_id, raw, reason, receipt, label)
                continue
            hard = hard_checks(env)
            if hard:
                _reject(conn, batch_id, raw, "; ".join(i.message for i in hard), receipt, label)
                continue
            if env.supersedes_id and not repo.is_current(conn, env.supersedes_id):
                _reject(conn, batch_id, raw, f"supersedes_id non corrente: {env.supersedes_id}", receipt, label)
                continue
            repo.ensure_source(conn, env.source_id, source_kind)
            if (env.source_record_id and repo.exists_source_record(conn, env.source_id, env.source_record_id)) \
                    or repo.exists_same_content(conn, env):
                receipt.duplicates.append(label)
                continue
            repo.insert(conn, env, batch_id)
            receipt.inserted.append(env)
            for issue in soft_checks(conn, env):
                _issue(conn, batch_id, env.id, issue)
                receipt.issues.append((env.id, issue.severity, issue.message))
        conn.execute(
            """UPDATE ingestion_batch SET finished_at=?, n_inserted=?, n_duplicates=?, n_rejected=?
               WHERE id=?""",
            (iso(now_utc()), len(receipt.inserted), len(receipt.duplicates), len(receipt.rejected), batch_id),
        )
    return receipt


def _reject(conn, batch_id, raw, reason, receipt, label) -> None:
    conn.execute(
        "INSERT INTO rejected_record(id, ingestion_batch_id, raw_input, reason, recorded_at) VALUES (?,?,?,?,?)",
        (new_id(), batch_id, json.dumps(raw, default=str, ensure_ascii=False), reason, iso(now_utc())),
    )
    receipt.rejected.append((label, reason))
