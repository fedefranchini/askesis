#!/usr/bin/env python3
"""Real-value guard: block numbers that fall inside the athlete's real ranges in added public lines.

Usage: git diff -U0 ... | real_values.py RULES_FILE      (exit 1 and report "file:line" on hits; values not printed)

RULES_FILE is private (never in git). One rule per line, fields separated by "|":
    name | min | max | decimal_only (yes/no) | context regex (optional, same line, case-insensitive)
e.g. a weight range matched only with a decimal separator, or an integer matched only next to a unit word.
Lines starting with "#" are comments.

Not scanned: knowledge/ (general evidence: published values may coincide with a personal plan by design),
generated JSON schemas, vendored third-party files (vendor/) and the dependency lock file. A reviewed false positive can be accepted with the marker "real-values: ok" on the line.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

SKIP = re.compile(r"^(knowledge/|schemas/)|(^|/)vendor/|(^|/)uv\.lock$")
NUMBER = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)(?![\d]|[.,]\d)")
THOUSANDS = re.compile(r"^\d{1,3}\.\d{3}$")  # Italian thousands separator: "2.450" is also read as 2450
MARKER = "real-values: ok"


@dataclass
class Rule:
    name: str
    lo: float
    hi: float
    decimal_only: bool
    context: re.Pattern | None


def load_rules(path: str) -> list[Rule]:
    rules = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            name, lo, hi, dec = parts[:4]
            ctx = parts[4] if len(parts) > 4 and parts[4] else None
            rules.append(Rule(name, float(lo), float(hi), dec.lower() in ("yes", "si", "sì", "true"),
                              re.compile(ctx, re.I) if ctx else None))
    return rules


def hits_in_line(text: str, rules: list[Rule]) -> list[str]:
    if MARKER in text:
        return []
    out = []
    for m in NUMBER.finditer(text):
        token = m.group(1)
        readings = [(float(token.replace(",", ".")), "." in token or "," in token)]
        if THOUSANDS.match(token):
            readings.append((float(token.replace(".", "")), False))
        for value, has_decimal in readings:
            for r in rules:
                if r.decimal_only and not has_decimal:
                    continue
                if r.lo <= value <= r.hi and (r.context is None or r.context.search(text)) and r.name not in out:
                    out.append(r.name)
    return out


def scan_diff(diff: str, rules: list[Rule]) -> list[tuple[str, int, str]]:
    """(file, line number in the new file, rule name) for every hit in added lines."""
    hits, path, line_no = [], None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            path = target[2:] if target.startswith("b/") else None
            continue
        h = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", raw)
        if h:
            line_no = int(h.group(1))
            continue
        if path is None or SKIP.search(path):
            continue
        if raw.startswith("+"):
            for name in hits_in_line(raw[1:], rules):
                hits.append((path, line_no, name))
            line_no += 1
        elif not raw.startswith("-"):
            line_no += 1
    return hits


def main() -> int:
    rules = load_rules(sys.argv[1])
    hits = scan_diff(sys.stdin.read(), rules)
    for path, line, name in hits:
        print(f"  valore nell'intervallo reale «{name}»: {path}:{line}", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
