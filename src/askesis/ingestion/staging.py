"""Adapter: staging NDJSON (envelope v0, written before the CLI existed) → canonical envelopes."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from askesis.core.ids import new_id

RENAMED = {"nutrition_summary_reported": "reported_summary"}


def _common(rec: dict) -> dict:
    return dict(
        id=rec["id"],
        source_record_id=rec["id"],
        schema_version="0.1",
        tz=rec["tz"],
        local_date=rec["local_date"],
        recorded_at=rec["recorded_at"],
        source_id=rec.get("source", "manual_chat"),
        entry_method=rec.get("entry_method", "manual"),
        supersedes_id=rec.get("supersedes_id"),
        missing_reason=rec.get("missing_reason"),
        notes=rec.get("notes") or None,
    )


def convert(rec: dict) -> list[dict]:
    et = rec["entity_type"]
    if et == "decision":
        local = datetime.fromisoformat(rec["recorded_at"]).astimezone(ZoneInfo(rec["tz"])).date().isoformat()
        payload = {k: rec[k] for k in ("type", "status", "athlete_response_verbatim", "scope") if k in rec}
        out = _common({**rec, "local_date": local})
        out.update(entity_type="decision_log", occurred_at=rec["recorded_at"], payload=payload)
        return [out]
    payload = dict(rec.get("payload", {}))
    out = _common(rec)
    out["entity_type"] = RENAMED.get(et, et)
    if et == "training_session":
        sets = payload.pop("sets", [])
        payload.pop("session_id", None)
        out["start_at"] = payload.pop("start_at", rec.get("occurred_at"))
        if payload.get("end_at"):
            out["end_at"] = payload.pop("end_at")
        records = [out | {"payload": payload}]
        for i, s in enumerate(sets, start=1):
            sp = {"session_id": out["id"], "sequence": i, "set_type": s.get("set_type", "working"), **s}
            sid = f"{rec['id']}#set{i}"
            records.append(_common(rec) | {"id": new_id(), "source_record_id": sid, "entity_type": "set_record",
                                           "start_at": out["start_at"], "payload": sp})
        return records
    if et in ("running_session", "sleep_session"):
        out["start_at"] = payload.pop("start_at", rec.get("occurred_at"))
        if payload.get("end_at"):
            out["end_at"] = payload.pop("end_at")
    else:
        out["occurred_at"] = rec["occurred_at"]
    out["payload"] = payload
    return [out]


def read(paths: list[Path]) -> tuple[list[dict], str]:
    """Return converted envelopes and a sha256 fingerprint of the inputs."""
    records, digest = [], hashlib.sha256()
    for p in paths:
        data = p.read_bytes()
        digest.update(data)
        for line in data.decode().splitlines():
            if line.strip():
                records += convert(json.loads(line))
    return records, digest.hexdigest()
