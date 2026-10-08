"""Daily sync from Apple Health through an iOS Shortcut (F4c): device tokens, payload conversion, probe.

The Shortcut reads Health samples while the iPhone is unlocked (Health data is encrypted while it is locked) and posts
JSON to the dashboard inside the private tunnel. The payload is converted into the same `HealthData` the export
importer produces and goes through the very same `apple_health.build`: same validations, same natural-key
fingerprints (re-sending is idempotent), same source priority (a record of another source, e.g. manual, for the same
entity and day always wins), same exclusions (days not closed yet, sources that are not the athlete's), nutrition
always `partial` until the athlete confirms the day.

Payload (version 1), one block per type; each block holds parallel lists, which is what the Shortcut's "Get details
of Health sample" produces from a list of samples:

    {"v": 1, "window_start": "<ISO date-time of the oldest sample asked for>",
     "probe": false,
     "samples": {"steps": {"value": [...], "unit": [...], "start": [...], "end": [...], "source": [...]},
                 "rhr": {...}, "hrv": {...}, "sleep": {...}, "kcal": {...}, "protein": {...}, "weight": {...},
                 "workouts": {"type": [...], "start": [...], "end": [...], "duration": [...], "distance": [...],
                              "distance_unit": [...], "source": [...]}}}

Daily totals need whole days: everything before the day after `window_start` is ignored (the next sync covers it).
With `probe: true` nothing is stored: the answer says, per type, how many samples were understood and which fields and
labels arrived — never values — so that what the phone really reads can be checked on the real device.

Tokens: random, shown once, stored only as SHA-256 in a private file next to the database, revocable one by one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from askesis.config import Config
from askesis.core.timeutil import now_utc

from . import apple_health as ah

VERSION = 1
MAX_BODY = 4 * 1024 * 1024
MAX_SAMPLES = 50_000
QUANTITY = {"steps", "rhr", "hrv", "kcal", "protein", "weight"}
KINDS = QUANTITY | {"sleep", "workouts"}
FIELDS = ("value", "unit", "start", "end", "source")
SLEEP = {  # labels the Shortcut may send (English or Italian) → Health sleep values
    "in bed": ah.IN_BED, "inbed": ah.IN_BED, "a letto": ah.IN_BED, "nel letto": ah.IN_BED,
    "asleep": "HKCategoryValueSleepAnalysisAsleepUnspecified", "sonno": "HKCategoryValueSleepAnalysisAsleepUnspecified",
    "addormentato": "HKCategoryValueSleepAnalysisAsleepUnspecified",
    "asleep unspecified": "HKCategoryValueSleepAnalysisAsleepUnspecified",
    "core": "HKCategoryValueSleepAnalysisAsleepCore", "principale": "HKCategoryValueSleepAnalysisAsleepCore",
    "sonno principale": "HKCategoryValueSleepAnalysisAsleepCore", "leggero": "HKCategoryValueSleepAnalysisAsleepCore",
    "deep": "HKCategoryValueSleepAnalysisAsleepDeep", "profondo": "HKCategoryValueSleepAnalysisAsleepDeep",
    "sonno profondo": "HKCategoryValueSleepAnalysisAsleepDeep",
    "rem": "HKCategoryValueSleepAnalysisAsleepREM", "sonno rem": "HKCategoryValueSleepAnalysisAsleepREM",
    "awake": "awake", "sveglio": "awake", "sveglia": "awake", "da sveglio": "awake",
}
UNIT_ALIASES = {"count/min": "bpm", "battiti/min": "bpm", "cal": "kcal", "kilocalorie": "kcal", "chilocalorie": "kcal",
                "grammi": "g", "gr": "g", "chilogrammi": "kg", "passi": "count", "conteggio": "count"}
NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


class SyncError(ValueError):
    """The payload cannot be used (shape, size, version). The message holds no health values."""


# ------------------------------------------------------------------ device tokens
def tokens_path(cfg: Config) -> Path:
    return cfg.db_path.parent / "sync_tokens.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _save(path: Path, data: dict) -> None:
    from askesis.web.auth import _write_private

    _write_private(path, json.dumps(data, indent=1))


def create_token(path: Path, label: str, now: float | None = None) -> tuple[str, dict]:
    """New device token: returns the secret (shown once) and the stored record (hash only)."""
    now = time.time() if now is None else now
    tid, secret = secrets.token_hex(4), secrets.token_urlsafe(32)
    data = _load(path)
    data[tid] = {"label": label[:40], "created": now, "last_used": None, "last_result": None,
                 "hash": hashlib.sha256(secret.encode()).hexdigest()}
    _save(path, data)
    return f"{tid}.{secret}", data[tid] | {"id": tid}


def check_token(path: Path, header: str | None) -> str | None:
    """Token id for a valid `Authorization: Bearer <id>.<secret>` header, else None (constant-time comparison)."""
    if not header or not header.startswith("Bearer ") or "." not in header:
        return None
    tid, secret = header.removeprefix("Bearer ").strip().split(".", 1)
    rec = _load(path).get(tid)
    if rec is None:
        hmac.compare_digest("0" * 64, hashlib.sha256(secret.encode()).hexdigest())  # same work for unknown ids
        return None
    return tid if hmac.compare_digest(rec["hash"], hashlib.sha256(secret.encode()).hexdigest()) else None


def note_use(path: Path, tid: str, result: dict, now: float | None = None) -> None:
    """Last use and the counts of the last sync (no values)."""
    data = _load(path)
    if tid in data:
        data[tid]["last_used"] = time.time() if now is None else now
        data[tid]["last_result"] = result
        _save(path, data)


def tokens(path: Path) -> list[dict]:
    return sorted(({"id": k} | {x: y for x, y in v.items() if x != "hash"} for k, v in _load(path).items()),
                  key=lambda t: -t["created"])


def revoke_token(path: Path, tid: str) -> bool:
    data = _load(path)
    if tid not in data:
        return False
    del data[tid]
    _save(path, data)
    return True


# ------------------------------------------------------------------ parsing
def parse_when(raw, tz: str) -> datetime:
    """ISO 8601 with or without offset (naive = local time zone), or Unix seconds."""
    if isinstance(raw, int | float):
        return datetime.fromtimestamp(raw, ZoneInfo("UTC"))
    s = str(raw).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}", s):
        return ah.parse_dt(s)
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T", 1) if "T" not in s else s)
    except ValueError as exc:
        raise SyncError("data in un formato non riconosciuto") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(tz))


def parse_number(raw) -> float:
    if isinstance(raw, bool):
        raise SyncError("valore non numerico")
    if isinstance(raw, int | float):
        return float(raw)
    m = NUMBER.search(str(raw))
    if m is None:
        raise SyncError("valore non numerico")
    return float(m.group().replace(",", "."))


def _unit(raw) -> str:
    u = str(raw or "").strip()
    return UNIT_ALIASES.get(u.lower(), u)


def _columns(block, names: tuple[str, ...]) -> tuple[list[list], int]:
    if not isinstance(block, dict):
        raise SyncError("ogni tipo deve essere un oggetto con liste parallele")
    cols = []
    for k in names:
        v = block.get(k)
        cols.append(v if isinstance(v, list) else ([] if v is None else [v]))
    n = max((len(c) for c in cols), default=0)
    return [c + [None] * (n - len(c)) for c in cols], n


def convert(payload: dict, cfg: Config, received_at: datetime | None = None) -> tuple[ah.HealthData, date, dict]:
    """Payload → (HealthData, first whole day, probe report). Unreadable samples are counted, never guessed."""
    if not isinstance(payload, dict) or payload.get("v") != VERSION:
        raise SyncError(f"versione del formato non supportata (serve v={VERSION})")
    samples = payload.get("samples")
    if not isinstance(samples, dict):
        raise SyncError("manca 'samples'")
    unknown = sorted(set(samples) - KINDS)
    tz = cfg.timezone
    received_at = received_at or now_utc()
    try:
        window = parse_when(payload["window_start"], tz)
    except KeyError as exc:
        raise SyncError("manca 'window_start'") from exc
    if window > received_at:
        raise SyncError("'window_start' nel futuro")
    since = window.astimezone(ZoneInfo(tz)).date() + timedelta(days=1)  # first whole day
    data = ah.HealthData(export_at=received_at)
    report: dict = {"types": {}, "ignored_types": unknown, "first_whole_day": since.isoformat()}
    total = 0
    for kind in sorted(set(samples) & KINDS):
        block = samples[kind]
        if kind == "workouts":
            cols, n = _columns(block, ("type", "start", "end", "duration", "distance", "distance_unit", "source",
                                       "avg_hr"))
        else:
            cols, n = _columns(block, FIELDS)
        total += n
        if total > MAX_SAMPLES:
            raise SyncError("troppi campioni in un solo invio")
        ok, bad, labels, units, sources = 0, Counter(), Counter(), Counter(), Counter()
        for row in zip(*cols, strict=True):
            try:
                if kind == "workouts":
                    wtype, s, e, dur, dist, dunit, src, hr = row
                    start, end = parse_when(s, tz), parse_when(e, tz)
                    label = str(wtype or "").strip()
                    labels[label] += 1
                    low = label.lower()
                    hk = ah.RUNNING if ("run" in low or "corsa" in low) else \
                        "HKWorkoutActivityTypeTraditionalStrengthTraining" if ("strength" in low or "forza" in low) \
                        else label
                    distance_m = None
                    if dist not in (None, ""):
                        distance_m = parse_number(dist) * ah.TO_M.get(_unit(dunit) or "km", 1000.0)
                    elapsed = round(parse_number(dur)) if dur not in (None, "") else \
                        round((end - start).total_seconds())
                    data.workouts.append({"type": hk, "start": start, "end": end, "source": str(src or "?"),
                                          "elapsed_s": elapsed, "distance_m": distance_m,
                                          "avg_hr": round(parse_number(hr)) if hr not in (None, "") else None,
                                          "max_hr": None})
                    sources[str(src or "?")] += 1
                else:
                    value, unit, s, e, src = row
                    start, end = parse_when(s, tz), parse_when(e if e not in (None, "") else s, tz)
                    unit = _unit(unit)
                    units[unit] += 1
                    sources[str(src or "?")] += 1
                    if kind == "sleep":
                        label = str(value or "").strip()
                        labels[label] += 1
                        hk = SLEEP.get(label.lower())
                        if hk is None:
                            raise SyncError("etichetta del sonno non riconosciuta")
                        if hk == "awake":
                            ok += 1  # understood, not stored (awake time inside the night)
                            continue
                        data.samples["sleep"].append((start, end, hk, unit, str(src or "?")))
                    else:
                        if kind == "weight" and unit not in ah.TO_KG:
                            raise SyncError("unità del peso non riconosciuta")
                        if kind == "kcal" and unit not in ah.TO_KCAL:
                            raise SyncError("unità dell'energia non riconosciuta")
                        data.samples[kind].append((start, end, f"{parse_number(value):g}", unit, str(src or "?")))
                ok += 1
            except (SyncError, TypeError, ValueError) as exc:
                bad[str(exc) if isinstance(exc, SyncError) else "campione non leggibile"] += 1
        report["types"][kind] = {"received": n, "understood": ok, "problems": dict(bad), "units": sorted(units),
                                 "labels": sorted(labels), "sources": sorted(sources)}
    return data, since, report


def run(conn: sqlite3.Connection, cfg: Config, payload: dict, received_at: datetime | None = None,
        probe: bool = False) -> dict:
    """Convert and (unless probing) ingest. Returns counts only (never values)."""
    from askesis.ingestion.pipeline import ingest

    probe = probe or bool(payload.get("probe")) if isinstance(payload, dict) else probe
    data, since, report = convert(payload, cfg, received_at)
    if not cfg.health_sync_weight and data.samples.pop("weight", None) is not None:
        report["not_synced"] = ["weight: sincronizzazione del peso disattivata (health_sync_weight)"]
    exclude = set(cfg.health_exclude_sources)
    plan = ah.build(data, cfg, conn, since, now=received_at, exclude_sources=exclude)
    counts = Counter()
    for (entity, outcome), n in plan.counts.items():
        counts[f"{entity}: {outcome}"] += n
    result = {"probe": probe, "report": report, "plan": dict(sorted(counts.items())),
              "excluded_sources": sorted(s for t in report["types"].values() for s in t["sources"]
                                         if s.casefold() in {x.casefold() for x in exclude})}
    already = sum(n for (_e, outcome), n in plan.counts.items() if outcome == "già presente")
    skipped = sum(n for (_e, outcome), n in plan.counts.items() if outcome.startswith("saltato"))
    result |= {"already": already, "skipped_other_source": skipped}
    if probe or not plan.records:
        result |= {"inserted": 0, "duplicates": 0, "rejected": 0, "last_day": None}
        return result
    r = ingest(conn, plan.records, "apple_health_sync", input_ref="shortcut", source_kind="imported")
    result |= {"inserted": len(r.inserted), "duplicates": len(r.duplicates), "rejected": len(r.rejected),
               "last_day": max((e.local_date for e in r.inserted), default=None)}
    if result["last_day"] is not None:
        result["last_day"] = result["last_day"].isoformat()
    return result


def summary_line(result: dict) -> str:
    """One line for the phone (no values)."""
    if result["probe"]:
        parts = [f"{k} {v['understood']}/{v['received']}" for k, v in result["report"]["types"].items()]
        return "Prova: nulla salvato. Letti " + (", ".join(parts) if parts else "nessun tipo") + "."
    return (f"Askesis: {result['inserted']} nuovi, {result['already'] + result['duplicates']} già presenti"
            + (f", {result['skipped_other_source']} già registrati a mano" if result["skipped_other_source"] else "")
            + (f", {result['rejected']} rifiutati" if result["rejected"] else "") + ".")


# ------------------------------------------------------------------ partial nutrition days
def partial_days(conn: sqlite3.Connection, since: date | None = None) -> list[dict]:
    """Nutrition days imported from Health and still partial (not confirmed by the athlete), newest first."""
    rows = conn.execute(
        "SELECT id, local_date, payload FROM v_current WHERE entity_type = 'nutrition_day' AND source_id = ? "
        "AND json_extract(payload, '$.completeness') = 'partial' AND local_date >= ? ORDER BY local_date DESC",
        (ah.SOURCE_ID, (since or date.min).isoformat())).fetchall()
    return [{"id": r["id"], "day": date.fromisoformat(r["local_date"]), "payload": json.loads(r["payload"])}
            for r in rows]


def confirmation_record(conn: sqlite3.Connection, cfg: Config, day: date, now: datetime | None = None) -> dict:
    """Manual record that confirms an imported partial day as complete (supersedes it, same values). From then on the
    day belongs to the athlete: later syncs skip it (another source exists)."""
    from askesis.core.ids import new_id
    from askesis.core.timeutil import at_local

    rows = [r for r in partial_days(conn, day) if r["day"] == day]
    if not rows:
        raise SyncError("nessuna giornata parziale importata da Salute per questa data")
    cur = rows[0]
    payload = cur["payload"] | {"completeness": "complete", "logging_method": "via_apple_health_confirmed"}
    now = now or now_utc()
    return {"id": new_id(), "entity_type": "nutrition_day", "tz": cfg.timezone, "local_date": day,
            "occurred_at": at_local(day, datetime.min.time().replace(hour=12), cfg.timezone), "recorded_at": now,
            "source_id": "manual", "entry_method": "manual", "supersedes_id": cur["id"], "payload": payload,
            "notes": "giornata importata da Salute confermata completa dall'atleta"}
