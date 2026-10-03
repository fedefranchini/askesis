"""Export the ingestion contract as language-agnostic JSON Schema (ADR-007)."""

from __future__ import annotations

import json
from pathlib import Path

from .entities import ENTITIES, SCHEMA_VERSION, Envelope


def export(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    files = {"envelope": Envelope} | {name: model for name, (model, _) in ENTITIES.items()}
    for name, model in files.items():
        schema = model.model_json_schema()
        schema["$comment"] = f"askesis schema {SCHEMA_VERSION}"
        path = directory / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n")
        written.append(path)
    return written
