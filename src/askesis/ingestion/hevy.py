"""Hevy workout export (CSV/TSV) → training sessions and sets.

- Exercises are never guessed: every Hevy exercise title must be mapped explicitly (private mapping file) to a
  catalog id, or marked `ignore`/`raw`. The preview proposes candidates; import is refused while titles are unmapped.
- Hevy set types: normal → working, warmup → warmup, failure → working with to_failure, dropset → drop.
  RPE is kept as recorded (RIR is derived from RPE by the analytics where needed). Duration/distance-only sets
  (cardio) are not imported.
- A Hevy session overlapping (≥ half of the shorter) a session imported from Apple Health supersedes it: the session
  with sets becomes current, the Health one stays in history.
- Idempotent: fingerprints of (start, title) for sessions and (session, position) for sets.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from askesis.config import Config
from askesis.core.ids import new_id
from askesis.core.timeutil import local_date, now_utc
from askesis.reference import catalog

SOURCE_ID = "hevy"
DATE_FORMATS = ("%d %b %Y, %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
SET_TYPES = {"normal": ("working", None), "warmup": ("warmup", None), "failure": ("working", True),
             "dropset": ("drop", None)}
LB = 0.45359237


class HevyError(ValueError):
    pass


@dataclass
class HevySet:
    exercise: str
    position: int
    set_type: str
    load_kg: float | None
    reps: int | None
    rpe: float | None


@dataclass
class HevySession:
    title: str
    start: datetime
    end: datetime | None
    sets: list[HevySet] = field(default_factory=list)


def _dt(value: str, tz: str) -> datetime:
    for fmt in DATE_FORMATS:
        try:
            d = datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
        return d if d.tzinfo else d.replace(tzinfo=ZoneInfo(tz))
    raise HevyError(f"formato data non riconosciuto: {value!r} (nessuna ipotesi: serve un formato noto)")


def _num(v: str | None) -> float | None:
    v = (v or "").strip()
    return float(v.replace(",", ".")) if v else None


def parse(path: Path, tz: str) -> list[HevySession]:
    text = path.read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",\t;")
    rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    if not rows or "exercise_title" not in rows[0]:
        raise HevyError("colonne attese assenti (exercise_title, start_time…): è un'esportazione degli allenamenti?")
    weight_col = "weight_kg" if "weight_kg" in rows[0] else "weight_lbs" if "weight_lbs" in rows[0] else None
    sessions: dict[tuple, HevySession] = {}
    position: Counter = Counter()
    for r in rows:
        key = (r["title"], r["start_time"])
        if key not in sessions:
            sessions[key] = HevySession(r["title"], _dt(r["start_time"], tz),
                                        _dt(r["end_time"], tz) if r.get("end_time") else None)
        position[key] += 1
        load = _num(r.get(weight_col)) if weight_col else None
        if load is not None and weight_col == "weight_lbs":
            load = round(load * LB, 2)
        reps = _num(r.get("reps"))
        sessions[key].sets.append(HevySet(r["exercise_title"].strip(), position[key], (r.get("set_type") or "normal")
                                          .strip().lower(), load, int(reps) if reps is not None else None,
                                          _num(r.get("rpe"))))
    return sorted(sessions.values(), key=lambda s: s.start)


# ------------------------------------------------------------------ exercise mapping
def load_mapping(path: Path) -> dict[str, str]:
    """title → catalog id | 'ignore' | 'raw'. Only explicit entries count."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {str(k): str(v) for k, v in (data.get("exercises") or {}).items() if v}


def propose(title: str) -> str | None:
    """Candidate catalog id for a Hevy title (shown to the athlete, never applied without confirmation)."""
    t = title.lower()
    equipment = re.search(r"\(([^)]+)\)", t)
    eq = equipment.group(1) if equipment else ""
    base = re.sub(r"\([^)]*\)", "", t).strip()
    best, score = None, 0
    for ex in catalog()["exercises"]:
        names = [ex["name"].lower(), ex["id"].replace("_", " "), *[a.lower() for a in ex.get("aliases", [])]]
        for n in names:
            words = set(re.findall(r"[a-z]+", n)) - {"barbell", "dumbbell", "db", "machine", "cable"}
            if not words:
                continue
            overlap = len(words & set(re.findall(r"[a-z]+", base))) / len(words)
            dumbbell_ok = ("dumbbell" in eq) == ("dumbbell" in ex["name"].lower() or ex["id"].startswith("db_")
                                                 or "manubri" in ex.get("name_it", "").lower())
            s = overlap + (0.2 if dumbbell_ok else -0.5)
            if overlap == 1 and s > score:
                best, score = ex["id"], s
    return best


def mapping_report(sessions: list[HevySession],
                   mapping: dict[str, str]) -> list[tuple[str, int, str | None, str | None]]:
    """(title, sets, confirmed mapping, proposal) for every exercise title in the export."""
    counts = Counter(s.exercise for sess in sessions for s in sess.sets)
    return [(t, n, mapping.get(t), None if t in mapping else propose(t)) for t, n in counts.most_common()]


# ------------------------------------------------------------------ envelopes
def _fp(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]


@dataclass
class Plan:
    records: list[dict] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    unmapped: list[str] = field(default_factory=list)


def build(sessions: list[HevySession], mapping: dict[str, str], cfg: Config, conn: sqlite3.Connection,
          now: datetime | None = None) -> Plan:
    now = now or now_utc()
    plan = Plan(unmapped=sorted({s.exercise for sess in sessions for s in sess.sets if s.exercise not in mapping}))
    if plan.unmapped:
        return plan  # never import with unconfirmed exercises
    by_id = {e["id"]: e for e in catalog()["exercises"]}
    health = defaultdict(list)
    for r in conn.execute("SELECT id, local_date, start_at, end_at FROM v_current WHERE entity_type = "
                          "'training_session' AND source_id = 'apple_health'"):
        health[r["local_date"]].append(r)
    for sess in sessions:
        day = local_date(sess.start, cfg.timezone)
        sfp = _fp("session", sess.start.isoformat(), sess.title)
        if conn.execute("SELECT 1 FROM raw_record WHERE source_id = ? AND source_record_id = ?",
                        (SOURCE_ID, sfp)).fetchone():
            plan.counts[("seduta", "già presente")] += 1
            continue
        session = {"id": new_id(), "entity_type": "training_session", "tz": cfg.timezone, "local_date": day,
                   "recorded_at": now, "source_id": SOURCE_ID, "source_record_id": sfp, "entry_method": "imported",
                   "payload": {"session_kind": "strength"}, "start_at": sess.start,
                   "original_values": {"title": sess.title}}
        if sess.end:
            session["end_at"] = sess.end
            for h in health.get(day.isoformat(), []):
                hs, he = datetime.fromisoformat(h["start_at"]), datetime.fromisoformat(h["end_at"] or h["start_at"])
                inter = (min(he, sess.end) - max(hs, sess.start)).total_seconds()
                shorter = min((he - hs).total_seconds(), (sess.end - sess.start).total_seconds()) or 1
                if inter / shorter >= 0.5:
                    session["supersedes_id"] = h["id"]
                    plan.counts[("seduta", "sostituisce la seduta importata da Salute")] += 1
                    break
        plan.records.append(session)
        plan.counts[("seduta", "nuova")] += 1
        seq = 0
        for s in sess.sets:
            target = mapping[s.exercise]
            if target == "ignore":
                plan.counts[("serie", "ignorata (esercizio da ignorare)")] += 1
                continue
            if s.reps is None:
                plan.counts[("serie", "non importata: solo durata o distanza")] += 1
                continue
            set_type, failure = SET_TYPES.get(s.set_type, ("working", None))
            seq += 1
            ex = by_id.get(target)
            load = s.load_kg or 0.0
            kind = "external"
            if ex and ex.get("load_kind") == "bodyweight":
                kind = ("assisted" if "assist" in s.exercise.lower()
                        else "bodyweight" if load == 0 else "bodyweight_plus")
            payload = {"session_id": session["id"], "exercise_raw": s.exercise, "sequence": seq, "set_type": set_type,
                       "load_kg": load, "load_kind": kind, "reps": s.reps}
            if ex:
                payload["exercise_id"] = target
            if s.rpe is not None:
                payload["rpe"] = s.rpe
            if failure:
                payload["to_failure"] = True
            plan.records.append({"id": new_id(), "entity_type": "set_record", "tz": cfg.timezone, "local_date": day,
                                 "recorded_at": now, "source_id": SOURCE_ID, "entry_method": "imported",
                                 "source_record_id": _fp(sfp, s.position), "payload": payload,
                                 "start_at": sess.start})
            plan.counts[("serie", "nuova")] += 1
    return plan
