"""Reference data: exercise catalog lookup."""

from __future__ import annotations

import re
from functools import lru_cache

import yaml

from askesis.config import ROOT

CATALOG = ROOT / "knowledge" / "reference" / "exercises.yaml"


def normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


@lru_cache(maxsize=1)
def catalog() -> dict:
    return yaml.safe_load(CATALOG.read_text())


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, dict]:
    index = {}
    for ex in catalog()["exercises"]:
        for key in [ex["id"], ex["name"], *ex.get("aliases", [])]:
            index[normalize(key)] = ex
    return index


def find_exercise(name: str) -> dict | None:
    return _alias_index().get(normalize(name))


def exercise_label(key: str) -> str:
    """Italian name of a catalog exercise (falls back to the raw text for uncatalogued ones)."""
    e = next((x for x in catalog()["exercises"] if x["id"] == key), None)
    return (e.get("name_it") or e["name"]) if e else key.removeprefix("raw:")
