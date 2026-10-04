"""Metric parameters loaded from knowledge/parameters/metrics.yaml (single source of truth)."""

from __future__ import annotations

from functools import lru_cache

import yaml

from askesis.config import ROOT

PARAMS_FILE = ROOT / "knowledge" / "parameters" / "metrics.yaml"
BASES = {"evidence", "expert_opinion", "engineering_choice"}


@lru_cache(maxsize=1)
def all_params() -> dict:
    data = yaml.safe_load(PARAMS_FILE.read_text())["parameters"]
    for key, p in data.items():
        if p.get("basis") not in BASES:
            raise ValueError(f"parametro {key}: basis mancante o non valida")
        if p["basis"] == "evidence" and not p.get("claims"):
            raise ValueError(f"parametro {key}: basis=evidence richiede almeno un claim")
    return data


def p(key: str):
    return all_params()[key]["value"]


def sd(key: str) -> float:
    return float(all_params()[key]["sd"])
