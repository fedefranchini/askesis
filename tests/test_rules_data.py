"""Rules are data: every rule file is well-formed and cites only existing claims."""

import yaml

from askesis.config import ROOT

CLAIMS = {c["id"] for c in yaml.safe_load((ROOT / "knowledge/evidence/claims.yaml").read_text())["claims"]}
BASES = {"evidence", "expert_opinion", "engineering_choice"}


def test_rule_files_are_valid():
    files = sorted((ROOT / "knowledge/rules").glob("*.yaml"))
    assert files
    for f in files:
        rule = yaml.safe_load(f.read_text())
        assert {"id", "version", "level", "domain", "status", "preconditions", "limits", "basis"} <= rule.keys(), f
        assert rule["status"] in {"proposed", "shadow", "active", "retired"}
        assert rule["preconditions"].get("no_open_safety_flags") is True, "safety always has priority"
        for name, b in rule["basis"].items():
            assert b["basis"] in BASES, (f.name, name)
            assert set(b.get("claims", [])) <= CLAIMS, (f.name, name)
            if b["basis"] == "evidence":
                assert b.get("claims"), (f.name, name)
