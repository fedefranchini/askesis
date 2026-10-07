"""Day sheet for the gym as a note (e.g. Apple Notes): render the planned session, parse the results back.

The note holds only exercises and loads (no health data). Layout, one item per line:

    Askesis · 2025-03-03 · A
    Scrivi i risultati dopo → (es. 60x8 r2, 60x7 r1). Non modificare le righe con [..].
    Panca piana [bench_press]
    3 × 6–8 · RIR 2 · consigliato 52,5 kg
    recupero 2–3 min
    ultima (24/2): 50 kg × 8 @RIR 2 · 50 kg × 8 @RIR 2
    →

Parsing is tolerant to phone autocorrect (×, X, R2, kg, @RIR 2, decimal comma). Lines that cannot be parsed are
reported and never guessed. Re-import is idempotent; a line edited after import becomes a correction.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from askesis.model.entities import payload_model
from askesis.parsers.text import ParsedExercise, ParseError, parse_gym
from askesis.plan.rest import rest_label
from askesis.reference import find_exercise

MARKER = re.compile(r"\[([a-z0-9_:.\- ]+)\]\s*$", re.I)
RESULT = re.compile(r"^\s*(?:→|->|>|=>)\s*(.*)$")
SOURCE_ID = "gym_note"


def title_for(day: date, session_name: str) -> str:
    return f"Askesis · {day.isoformat()} · {session_name}"


def _n(v: float) -> str:
    return (f"{v:g}").replace(".", ",")


def render(day: date, session: dict, last: dict[str, str], settings: dict[str, str]) -> list[str]:
    """Plain lines of the note for one planned session (output of plan.rules.next_session)."""
    lines = [
        title_for(day, session["name"]),
        "Scrivi i risultati dopo → (es. 60x8 r2, 60x7 r1). Non modificare le righe con [..].",
    ]
    for lift in session["lifts"]:
        ref = find_exercise(lift["exercise"])
        name = ref.get("name_it", ref["name"]) if ref else lift["exercise"]
        key = ref["id"] if ref else lift["exercise"]
        load = f"consigliato {_n(lift['load_kg'])} kg" if lift["load_kg"] is not None else "carico da calibrare"
        lines += [
            f"{name} [{key}]",
            f"{lift['sets']} × {lift['rep_range'][0]}–{lift['rep_range'][1]} · RIR {_n(lift['target_rir'])} · {load}",
            rest_label(key),
        ]
        if key in last:
            lines.append(f"ultima {last[key]}")
        if key in settings:
            lines.append(f"macchina: {settings[key]}")
        lines.append("→ ")
    if session.get("run"):
        r = session["run"]
        lines += [
            f"Corsa {r['kind']} [run]",
            " · ".join(
                x
                for x in (
                    f"{_n(r['duration_min'])} min" if r.get("duration_min") else "",
                    f"{_n(r['distance_km'])} km" if r.get("distance_km") else "",
                    f"intensità: {r['intensity']}",
                )
                if x
            ),
            "→ ",
        ]
    lines.append("Note → ")
    return lines


def to_html(lines: list[str]) -> str:
    head, *rest = lines
    return f"<div><h1>{html.escape(head)}</h1></div>" + "".join(f"<div>{html.escape(x)}</div>" for x in rest)


def html_to_lines(body: str) -> list[str]:
    """Apple Notes returns HTML; turn it back into text lines."""
    text = re.sub(r"(?i)<br\s*/?>|</(div|p|h[1-6]|li)>", "\n", body)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).replace(" ", " ")
    return [line.rstrip() for line in text.split("\n")]


def normalize_sets(text: str) -> str:
    """Tolerate autocorrect and natural variants: '80 × 8 @RIR 2; 80kg x7 R1' → '80x8 r2, 80x7 r1'."""
    t = text.strip()
    t = re.sub(r"(?i)(\d)\s*kg\b", r"\1", t)  # 80kg → 80 (before the × rule: "80kg x 7")
    t = re.sub(r"(\d)\s*[×xX*]\s*(\d)", r"\1x\2", t)  # 80 × 8 → 80x8
    t = re.sub(r"(?i)@?\s*rir\s*(\d)", r" r\1", t)  # @RIR 2 / rir2 → r2
    t = re.sub(r"(?i)\bR(\d)", r"r\1", t)
    t = re.sub(r"(?i)@\s*rpe\s*(\d)", r" @\1", t)
    t = re.sub(r"\s*[;/|]\s*", ", ", t)  # other separators → ", "
    # a comma separates sets only right after the end of a set (reps, RIR or RPE): "12,5x10" stays a decimal
    t = re.sub(r"((?:x\d+)|(?:\br\d+(?:[.,]5)?)|(?:@\d+(?:[.,]5)?)),(?=\S)", r"\1, ", t)
    return re.sub(r"\s+", " ", t).strip(" ,")


@dataclass
class NoteResult:
    exercises: list[ParsedExercise] = field(default_factory=list)
    run_text: str | None = None
    notes: str | None = None
    errors: list[tuple[str, str]] = field(default_factory=list)  # (exercise, problem)
    skipped: list[str] = field(default_factory=list)  # planned but no result written


def parse_note(lines: list[str]) -> NoteResult:
    res = NoteResult()
    current: str | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        m = MARKER.search(line)
        if m and not RESULT.match(line):
            current = m.group(1).strip()
            continue
        nm = re.match(r"(?i)^note\s*(?:→|->|>|=>)\s*(.*)$", line)
        if nm:
            res.notes = nm.group(1).strip() or None
            continue
        r = RESULT.match(line)
        if not r or current is None:
            continue
        content = r.group(1).strip()
        if not content:
            res.skipped.append(current)
            current = None
            continue
        if current == "run":
            res.run_text = content
        else:
            try:
                sets = parse_gym(f"{current} {normalize_sets(content)}")[0].sets
                res.exercises.append(ParsedExercise(current, sets))
            except ParseError as exc:
                res.errors.append((current, f"{content!r}: {exc}"))
        current = None
    return res


def keyed_records(records: list[dict], day: date) -> list[dict]:
    """Assign deterministic keys so that re-imports are idempotent: gym_note:<date>:<exercise>:<n>."""
    counters: dict[str, int] = {}
    for r in records:
        r["source_id"] = SOURCE_ID
        if r["entity_type"] == "training_session":
            r["source_record_id"] = f"{SOURCE_ID}:{day.isoformat()}:session"
        elif r["entity_type"] == "set_record":
            ex = r["payload"].get("exercise_id") or r["payload"]["exercise_raw"]
            counters[ex] = counters.get(ex, 0) + 1
            r["source_record_id"] = f"{SOURCE_ID}:{day.isoformat()}:{ex}:{counters[ex]}"
        elif r["entity_type"] == "running_session":
            r["source_record_id"] = f"{SOURCE_ID}:{day.isoformat()}:run"
    return records


def reconcile(conn: sqlite3.Connection, records: list[dict]) -> tuple[list[dict], list[str], list[str]]:
    """Compare with what was already imported from the same note.

    Returns (records to ingest, unchanged keys, keys previously imported but now missing from the note).
    Changed lines become corrections (supersedes_id); session ids are re-pointed to the existing session."""
    to_ingest, unchanged = [], []
    if not records:
        return [], [], []
    prefix = ":".join(records[0]["source_record_id"].split(":")[:2]) + ":%"  # gym_note:<date>:%
    existing = {
        r["source_record_id"].split("#")[0]: r
        for r in conn.execute(
            "SELECT * FROM v_current WHERE source_id = ? AND source_record_id LIKE ?", (SOURCE_ID, prefix)
        )
    }
    session_key = next((r["source_record_id"] for r in records if r["entity_type"] == "training_session"), None)
    old_session = existing.get(session_key)
    for r in records:
        key = r["source_record_id"]
        if r["entity_type"] == "set_record" and old_session is not None:
            r["payload"]["session_id"] = old_session["id"]
        old = existing.get(key)
        if old is None:
            to_ingest.append(r)
            continue
        normalized = (
            payload_model(r["entity_type"]).model_validate(r["payload"]).model_dump(mode="json", exclude_none=True)
        )
        if r["entity_type"] == "training_session" or json.loads(old["payload"]) == normalized:
            unchanged.append(key)
            continue
        version = int(old["source_record_id"].split("#v")[1]) + 1 if "#v" in old["source_record_id"] else 2
        r["source_record_id"] = f"{key}#v{version}"
        r["supersedes_id"] = old["id"]
        to_ingest.append(r)
    present = {r["source_record_id"].split("#")[0] for r in records}
    missing = sorted(k for k in existing if k not in present and not k.endswith(":session"))
    return to_ingest, unchanged, missing
