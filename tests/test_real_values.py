"""Real-value guard (.githooks/real_values.py), tested with synthetic rules only."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("real_values", Path(__file__).parents[1] / ".githooks/real_values.py")
rv = importlib.util.module_from_spec(spec)
sys.modules["real_values"] = rv  # dataclasses need the module registered
spec.loader.exec_module(rv)


def rules(tmp_path):
    f = tmp_path / "rules.txt"
    f.write_text("# synthetic\nw_dec | 60 | 70 | yes |\nkcal | 1700 | 2000 | no | kcal|cibo\n"
                 "rate | 0.4 | 0.4 | yes |\n")
    return rv.load_rules(str(f))


def diff(path: str, *lines: str) -> str:
    return "\n".join([f"+++ b/{path}", f"@@ -0,0 +1,{len(lines)} @@", *(f"+{x}" for x in lines)])


def test_hits_respect_decimal_only_context_and_thousands(tmp_path):
    r = rules(tmp_path)
    hits = rv.scan_diff(diff("tests/x.py", "peso 64.2", "days = 65", "1.850 kcal", "x = 1850", "rate = 0.4"), r)
    assert [(h[1], h[2]) for h in hits] == [(1, "w_dec"), (3, "kcal"), (5, "rate")]


def test_knowledge_and_reviewed_lines_are_not_scanned(tmp_path):
    r = rules(tmp_path)
    assert rv.scan_diff(diff("knowledge/evidence/claims.yaml", "rate 0,4 %"), r) == []
    assert rv.scan_diff(diff("docs/a.md", "rate 0,4 %  <!-- real-values: ok -->"), r) == []
    assert rv.scan_diff(diff("docs/a.md", "rate 0,4 %"), r)
