"""Gym sheet as a web form: the same planned session as the Apple Notes day sheet, filled in from the phone.

The form fields are turned into the very text the note would hold ("80x8 r2, 80x7 r1") and go through the same
parser, the same record builder, the same deterministic keys (`gymnote.keyed_records`) and the same reconciliation
(`gymnote.reconcile`) as `gym-note import`. Whatever is saved from the page and whatever is imported from the note
therefore reconcile against each other in either order: identical values are "already imported", different values
become corrections (supersedes), never duplicates.

Nothing here writes on its own: `build_sheet` / `compose` / `plan` only read; `save` is the single writer.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from askesis import config as config_mod
from askesis import services
from askesis.core.timeutil import at_local, now_utc
from askesis.ingestion import gymnote, manual
from askesis.ingestion.pipeline import Receipt, ingest
from askesis.parsers.text import Intent, ParsedExercise, ParseError, parse_gym, parse_run
from askesis.plan import rules as plan_rules
from askesis.plan.rest import rest_label
from askesis.reference import find_exercise
from askesis.safety import rules as safety

EXTRA_ROWS = 2  # at most this many set rows beyond the planned ones
KG = re.compile(r"^\d+(?:[.,]\d+)?$")
SIGNED = re.compile(r"^([+-])\s*(\d+(?:[.,]\d+)?)$")
BW = re.compile(r"^bw(?:[+-]\d+(?:[.,]\d+)?)?$", re.I)
REPS = re.compile(r"^\d{1,3}$")
RIR = re.compile(r"^\d+(?:[.,]5)?$")
RPE = re.compile(r"^@\d+(?:[.,]5)?$")
RUN_KEY = "run"


# ------------------------------------------------------------------ the sheet (read only)
@dataclass
class Lift:
    key: str
    name: str
    prescription: str | None
    rest: str | None
    last: str | None
    machine: str | None
    planned_sets: int
    base_rows: int
    bodyweight: bool
    load_hint: str  # recommended load, shown as a placeholder only
    existing: dict[int, dict] = field(default_factory=dict)  # n -> current payload (note or page)


@dataclass
class Sheet:
    day: date
    title: str = ""
    lifts: list[Lift] = field(default_factory=list)
    has_run: bool = False
    run_hint: str = ""
    message: str | None = None  # no session planned / blocked: nothing to fill
    existing_run: str = ""
    has_existing: bool = False


def _n(v: float) -> str:
    return f"{v:g}".replace(".", ",")


def _split_key(source_record_id: str) -> tuple[str, int] | None:
    """gym_note:<date>:<exercise>:<n>[#vK] -> (exercise, n)."""
    base = source_record_id.split("#")[0]
    parts = base.split(":", 2)
    if len(parts) < 3 or ":" not in parts[2]:
        return None
    ex, n = parts[2].rsplit(":", 1)
    return (ex, int(n)) if n.isdigit() else None


def current_results(conn: sqlite3.Connection, day: date) -> tuple[dict[str, dict[int, dict]], dict | None]:
    """Current (non-superseded) gym-note results for the day: sets by exercise and position, and the run."""
    sets: dict[str, dict[int, dict]] = {}
    run = None
    for r in conn.execute(
        "SELECT entity_type, source_record_id, payload FROM v_current WHERE source_id = ? AND local_date = ? "
        "AND entity_type IN ('set_record', 'running_session') ORDER BY source_record_id",
        (gymnote.SOURCE_ID, day.isoformat()),
    ):
        payload = json.loads(r["payload"])
        if r["entity_type"] == "running_session":
            run = payload
        elif (k := _split_key(r["source_record_id"])) is not None:
            sets.setdefault(k[0], {})[k[1]] = payload
    return sets, run


def _fmt_dur(s: float) -> str:
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def run_text(p: dict) -> str:
    """Payload of a recorded run back to the free text `parse_run` reads (round trip: same payload)."""
    inv = {"easy": "easy", "long": "long", "recovery": "recovery", "tempo": "tempo", "threshold": "threshold",
           "intervals": "intervals", "fartlek": "fartlek", "race": "race", "test": "test"}
    out = []
    if p.get("run_type") in inv:
        out.append(inv[p["run_type"]])
    if p.get("environment") == "treadmill":
        out.append("tm")
    if p.get("distance_m"):
        d = p["distance_m"]
        out.append(f"{d / 1000:g}km" if d >= 1000 else f"{d:g}m")
    if p.get("elapsed_s"):
        out.append(_fmt_dur(p["elapsed_s"]))
    if p.get("avg_hr"):
        out.append(f"fc{int(p['avg_hr'])}")
    if p.get("max_hr"):
        out.append(f"fcmax{int(p['max_hr'])}")
    if p.get("session_rpe") is not None:
        out.append(f"rpe{p['session_rpe']:g}")
    if p.get("stop_reason"):
        out.append(f"stop:{p['stop_reason']}")
    return " ".join(out)


def _row_text(p: dict) -> dict[str, str]:
    kind = p.get("load_kind", "external")
    load = p.get("load_kg") or 0
    if kind == "bodyweight":
        kg = ""
    elif kind == "bodyweight_plus":
        kg = f"+{_n(load)}"
    elif kind == "assisted":
        kg = f"-{_n(load)}"
    else:
        kg = _n(load)
    if "rir" in p:
        rir = _n(p["rir"])
    elif "rpe" in p:
        rir = f"@{_n(p['rpe'])}"
    else:
        rir = ""
    return {"kg": kg, "reps": str(p.get("reps", "")), "rir": rir}


def build_sheet(conn: sqlite3.Connection, day: date, settings: dict[str, str] | None = None,
                last_performance=None) -> Sheet:
    """Planned session for the day (record=False: reading never writes), with what is already recorded."""
    if settings is None or last_performance is None:
        from askesis.cli import f3  # reuse the note's helpers (resolved at call time)

        settings = f3._settings() if settings is None else settings
        last_performance = f3._last_performance if last_performance is None else last_performance
    sets, run = current_results(conn, day)
    sheet = Sheet(day, has_existing=bool(sets or run))
    out = plan_rules.next_session(conn, day, record=False)
    if out["status"] != "session":
        from askesis.cli import f3

        sheet.message = f3._status_message(out).removeprefix("✗ ")
        sheet.has_existing = False
        return sheet
    names, planned_keys = [], set()
    for s in out["sessions"]:
        names.append(s["name"])
        for lift in s["lifts"]:
            ref = find_exercise(lift["exercise"])
            key = ref["id"] if ref else lift["exercise"]
            planned_keys.add(key)
            load = lift["load_kg"]
            presc = (f"{lift['sets']} × {lift['rep_range'][0]}–{lift['rep_range'][1]} · RIR {_n(lift['target_rir'])}"
                     f" · {'consigliato ' + _n(load) + ' kg' if load is not None else 'carico da calibrare'}")
            existing = sets.get(key, {})
            sheet.lifts.append(Lift(
                key=key, name=ref.get("name_it", ref["name"]) if ref else lift["exercise"], prescription=presc,
                rest=rest_label(key), last=last_performance(conn, lift["exercise"], day), machine=settings.get(key),
                planned_sets=lift["sets"], base_rows=max(lift["sets"], max(existing, default=0)),
                bodyweight=bool(ref and ref.get("load_kind") == "bodyweight"),
                load_hint=_n(load) if load is not None else "", existing=existing))
        if s.get("run") and not sheet.has_run:
            r = s["run"]
            sheet.has_run = True
            sheet.run_hint = " · ".join(x for x in (
                f"{_n(r['duration_min'])} min" if r.get("duration_min") else "",
                f"{_n(r['distance_km'])} km" if r.get("distance_km") else "",
                f"intensità: {r['intensity']}" if r.get("intensity") else "") if x)
    sheet.title = " + ".join(dict.fromkeys(names))
    for key, existing in sets.items():  # recorded but not planned: keep it visible so a re-save never drops it
        if key not in planned_keys:
            ref = find_exercise(key)
            sheet.lifts.append(Lift(
                key=key, name=ref.get("name_it", ref["name"]) if ref else key, prescription=None, rest=None,
                last=None, machine=None, planned_sets=0, base_rows=max(existing), load_hint="",
                bodyweight=bool(ref and ref.get("load_kind") == "bodyweight"), existing=existing))
    if run:
        sheet.has_run = True
        sheet.existing_run = run_text(run)
    return sheet


# ------------------------------------------------------------------ form values
Rows = list[list[dict[str, str]]]  # per lift, per set row: {"kg", "reps", "rir"}


@dataclass
class Values:
    rows: Rows
    run: str = ""
    notes: str = ""


class SheetChanged(ValueError):
    pass


def prefill(sheet: Sheet) -> Values:
    rows: Rows = []
    for lift in sheet.lifts:
        r = [_row_text(lift.existing[j]) if j in lift.existing else {"kg": "", "reps": "", "rir": ""}
             for j in range(1, lift.base_rows + 1)]
        rows.append(r)
    return Values(rows, sheet.existing_run)


def values_from_form(sheet: Sheet, form: dict) -> Values:
    """Entered values, kept as typed. The planned lifts are fixed by the server; a mismatch means the plan changed."""
    rows: Rows = []
    for i, lift in enumerate(sheet.lifts):
        if form.get(f"ex_{i}", lift.key) != lift.key:
            raise SheetChanged("Il piano è cambiato dopo l'apertura della pagina: ricaricala.")
        try:
            n = int(form.get(f"n_{i}", lift.base_rows))
        except (TypeError, ValueError):
            n = lift.base_rows
        n = min(max(n, lift.base_rows), lift.base_rows + EXTRA_ROWS)
        rows.append([{k: str(form.get(f"{k}_{i}_{j}", "")).strip()[:20] for k in ("kg", "reps", "rir")}
                     for j in range(n)])
    return Values(rows, str(form.get("run", "")).strip()[:200], str(form.get("notes", "")).strip()[:500])


def add_row(sheet: Sheet, values: Values, i: int) -> None:
    lift = sheet.lifts[i]
    if 0 <= i < len(values.rows) and len(values.rows[i]) < lift.base_rows + EXTRA_ROWS:
        values.rows[i].append({"kg": "", "reps": "", "rir": ""})


def fingerprint(values: Values) -> str:
    import hashlib

    return hashlib.sha256(json.dumps([values.rows, values.run, values.notes], sort_keys=True).encode()).hexdigest()


# ------------------------------------------------------------------ form -> note text -> records
def _kg_token(kg: str, bodyweight: bool) -> str:
    kg = kg.strip().lower()
    if BW.match(kg):
        return kg
    if not kg:
        if bodyweight:
            return "bw"
        raise ParseError("manca il carico (kg)")
    if m := SIGNED.match(kg):
        if not bodyweight:
            raise ParseError(f"carico {kg!r}: il segno vale solo per gli esercizi a corpo libero")
        return f"bw{m[1]}{m[2]}"
    if KG.match(kg):
        if bodyweight:
            raise ParseError(f"carico {kg!r}: a corpo libero scrivi +10 (zavorra) o -10 (assistito); vuoto = corpo")
        return kg
    raise ParseError(f"carico non leggibile: {kg!r}")


def _set_text(row: dict[str, str], bodyweight: bool) -> str | None:
    """One row as note text ('80x8 r2'), None for an untouched row, ParseError for a row that cannot be read."""
    kg, reps, rir = row["kg"], row["reps"], row["rir"].strip().lower().removeprefix("r")
    if not reps:
        if kg or rir:
            raise ParseError("ripetizioni mancanti")
        return None
    if not REPS.match(reps):
        raise ParseError(f"ripetizioni non leggibili: {reps!r}")
    text = f"{_kg_token(kg, bodyweight)}x{reps}"
    if rir:
        if RIR.match(rir):
            text += f" r{rir.replace(',', '.')}"
        elif RPE.match(rir):
            text += f" {rir.replace(',', '.')}"
        else:
            raise ParseError(f"RIR non leggibile: {rir!r}")
    return text


@dataclass
class Composed:
    exercises: list[ParsedExercise] = field(default_factory=list)
    run: dict | None = None
    errors: list[tuple[str, str]] = field(default_factory=list)  # (label, problem)
    skipped: list[str] = field(default_factory=list)
    notes: str | None = None


def compose(sheet: Sheet, values: Values) -> Composed:
    """Same validation as the note: a row that cannot be read is reported and that exercise is not saved."""
    comp = Composed(notes=values.notes or None)
    for lift, rows in zip(sheet.lifts, values.rows, strict=True):
        try:
            parts = [t for row in rows if (t := _set_text(row, lift.bodyweight)) is not None]
            if not parts:
                comp.skipped.append(lift.name)
                continue
            sets = parse_gym(f"{lift.key} {gymnote.normalize_sets(', '.join(parts))}")[0].sets
            comp.exercises.append(ParsedExercise(lift.key, sets))
        except ParseError as exc:
            comp.errors.append((lift.name, str(exc)))
    if values.run:
        try:
            comp.run = parse_run(values.run)
        except ParseError as exc:
            comp.errors.append(("Corsa", f"{values.run!r}: {exc}"))
    return comp


def build_records(cfg, day: date, comp: Composed, now: datetime | None = None) -> list[dict]:
    """Exactly as `gym-note import` does: deterministic start time and deterministic keys."""
    now = now or now_utc()
    intents = [Intent("gym", {"exercises": comp.exercises})] if comp.exercises else []
    if comp.run:
        intents.append(Intent("run", comp.run))
    start = at_local(day, manual.NOON, cfg.timezone)
    records = [r for it in intents for r in manual.build(it, cfg, day, now)]
    for r in records:
        if "start_at" in r:
            dur = (r["end_at"] - r["start_at"]) if r.get("end_at") else None
            r["start_at"] = start
            if dur is not None:
                r["end_at"] = start + dur
    return gymnote.keyed_records(records, day)


@dataclass
class Plan:
    comp: Composed
    to_ingest: list[dict]
    unchanged: list[str]
    missing: list[str]
    lines: list[str]
    corrections: int

    @property
    def nothing_new(self) -> bool:
        return not self.to_ingest


def key_label(key: str) -> str:
    """'gym_note:2025-03-03:bench_press:3' -> 'Panca piana, serie 3'."""
    k = _split_key(key)
    if key.endswith(":run"):
        return "Corsa"
    if k is None:
        return key
    ref = find_exercise(k[0])
    return f"{ref.get('name_it', ref['name']) if ref else k[0]}, serie {k[1]}"


def plan(conn: sqlite3.Connection, cfg, day: date, sheet: Sheet, values: Values, now: datetime | None = None) -> Plan:
    comp = compose(sheet, values)
    records = build_records(cfg, day, comp, now)
    if records:
        to_ingest, unchanged, missing = gymnote.reconcile(conn, records)
    else:
        to_ingest, unchanged = [], []
        missing = sorted(k for k in _existing_keys(conn, day))
    return Plan(comp, to_ingest, unchanged, [key_label(k) for k in missing],
                services.preview(to_ingest).lines if to_ingest else [],
                sum(1 for r in to_ingest if r.get("supersedes_id")))


def _existing_keys(conn: sqlite3.Connection, day: date) -> list[str]:
    return [r["source_record_id"].split("#")[0] for r in conn.execute(
        "SELECT source_record_id FROM v_current WHERE source_id = ? AND local_date = ? AND source_record_id "
        "NOT LIKE '%:session'", (gymnote.SOURCE_ID, day.isoformat()))]


# ------------------------------------------------------------------ save
@dataclass
class Saved:
    receipt: Receipt
    flags: list[dict]
    comment: str | None  # file name of the comment, None when there is no comment
    comment_ok: bool = True


def write_comment(conn: sqlite3.Connection, day: date) -> tuple[str | None, bool]:
    """Post-session comment, validated like every generated text. Never prints (the dashboard log must hold no
    health numbers). Returns (file name or None, validated)."""
    from askesis.analytics import engine, session_comment
    from askesis.validation import textcheck

    _, values, _ = engine.run(conn, day, day)
    md = session_comment.render(conn, values, day)
    if md is None:
        return None, True
    out = config_mod.reports_dir() / f"seduta-{day.isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    draft = out.with_name(f".{out.name}.draft")
    draft.write_text(md + "\n")
    try:
        res, target = textcheck.finalize(draft, out, conn)
    finally:
        draft.unlink(missing_ok=True)
    return Path(target).name, res.ok


def save(conn: sqlite3.Connection, cfg, day: date, sheet: Sheet, values: Values) -> tuple[Plan, Saved | None]:
    """Re-parse and re-reconcile on the server (a preview is never trusted), then ingest."""
    pl = plan(conn, cfg, day, sheet, values)
    if pl.nothing_new:
        return pl, None
    receipt = ingest(conn, pl.to_ingest, "gym_note")
    safety.evaluate(conn, day)
    flags = [dict(f) | {"actions": json.loads(f["actions"])} for f in safety.open_flags(conn)]
    name, ok = (None, True)
    if any(e.entity_type == "set_record" for e in receipt.inserted):
        name, ok = write_comment(conn, day)
    return pl, Saved(receipt, flags, name, ok)
