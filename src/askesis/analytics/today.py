"""The "Today" summary shown by the dashboard: key numbers with a status colour, computed here, not in the web layer.

Every number comes from recorded data or an engine metric (each tile carries its source ref). Status colours:
- "ok" (in line) / "warn" (attention) only against a target or threshold that exists in the active plan, a versioned
  rule or a parameter with its basis; "neutral" otherwise (no target, too few data). Red is reserved to safety flags,
  which are not computed here.
- Weight has no colour: its verdict is trend vs noise (CI of the 14-day rate), and weight-loss safety is the safety
  layer's job.
Only data up to `day` is used (no look-ahead): yesterday is the last closed nutrition day.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import numpy as np

from askesis.plan import calorie_rule
from askesis.plan import rules as plan_rules
from askesis.plan import store as plan_store

from . import adherence, body, engine
from .params import p


@dataclass
class Tile:
    key: str
    label: str
    value: float | None
    unit: str
    sub: str = ""
    status: str = "neutral"  # ok | warn | neutral
    ref: str = ""
    progress: float | None = None  # 0..1 for rings and bars (actual / target), None = no target
    detail: dict = field(default_factory=dict)


def _fmt(x: float, nd: int = 0) -> str:
    s = f"{x:,.{nd}f}".replace(",", " ")  # narrow no-break space as thousands separator
    return s.replace(".", ",") if nd else s


def weight_tile(inp: engine.Inputs, day: date) -> Tile:
    daily = {d: v for d, v in body.daily_weights(inp.weighins).items() if d <= day}
    ema = body.weight_ema_series(daily)
    if not ema:
        return Tile("weight", "Peso di tendenza", None, "kg", "nessuna pesata", ref="weight_ema@1")
    last = max(ema)
    before = [d for d in ema if d <= last - timedelta(days=7)]
    change = ema[last] - ema[max(before)] if before else None
    sub = f"{'+' if change >= 0 else '−'}{_fmt(abs(change), 1)} kg in 7 giorni" if change is not None else ""
    verdict, detail = "", {"last_weighin": last.isoformat()}
    rate = next((m for m in body.weight_rate(daily, last, 14) if m.metric_id == "weight_rate_pct_14d"), None)
    if rate is not None and rate.lo is not None and rate.hi is not None:
        if rate.lo <= 0 <= rate.hi:
            verdict = "variazione nel rumore"
        else:
            verdict = f"{'in calo' if rate.value < 0 else 'in aumento'} oltre il rumore"
        detail |= {"rate_pct_week": rate.value, "lo": rate.lo, "hi": rate.hi, "rate_ref": rate.ref}
    sub = " · ".join(x for x in (sub, verdict) if x)
    if last < day:
        sub += f" · ultima pesata {last.strftime('%d/%m')}"
    return Tile("weight", "Peso di tendenza", float(ema[last]), "kg", sub, ref="weight_ema@1", detail=detail)


def _nutrition(inp: engine.Inputs, d: date):
    return next((n for n in inp.nutrition if n.day == d), None)


def nutrition_tiles(inp: engine.Inputs, day: date) -> list[Tile]:
    """Yesterday's intake and protein vs the target in force yesterday."""
    y = day - timedelta(days=1)
    n = _nutrition(inp, y)
    target = adherence.plan_on(inp.plans, "nutrition_target", y)
    tol_e = calorie_rule.load_rule()["preconditions"]["intake_vs_target_within"]
    tol_p = p("protein_status_tolerance")
    out = []
    for key, label, field_, tkey, unit in (("energy", "Energia di ieri", "kcal", "energy_kcal", "kcal"),
                                           ("protein", "Proteine di ieri", "protein_g", "protein_g", "g")):
        v = getattr(n, field_) if n else None
        t = (target or {}).get(tkey)
        tile = Tile(key, label, v, unit, ref="nutrition_day (dato registrato)")
        if v is None:
            tile.sub = "non registrato"
        elif n.completeness != "complete":
            tile.sub = "giornata incompleta"
        if t:
            tile.detail["target"] = t
            tile.sub = " · ".join(x for x in (tile.sub, f"target {_fmt(t)} {unit}") if x)
            if v is not None and n.completeness == "complete":
                tile.progress = v / t
                if key == "energy":
                    tile.status = "ok" if abs(v - t) / t <= tol_e else "warn"
                    tile.detail["tolerance"] = {"value": tol_e, "ref": "calorie_adjustment@2"}
                else:
                    tile.status = "ok" if v >= t * (1 - tol_p) else "warn"
                    tile.detail["tolerance"] = {"value": tol_p, "ref": "protein_status_tolerance"}
        out.append(tile)
    return out


def sessions_tile(inp: engine.Inputs, day: date) -> Tile:
    """Strength sessions this ISO week: done vs planned. Attention only for sessions planned before today."""
    monday = day - timedelta(days=day.weekday())
    week = adherence.sessions_vs_plan(inp.sets, inp.plans, monday, monday + timedelta(days=6))
    if week is None:
        return Tile("sessions", "Sedute della settimana", None, "", "nessun programma attivo",
                    ref="sessions_vs_plan_week@1")
    yesterday = day - timedelta(days=1)
    done_before = len({s.session_id for s in inp.sets if monday <= s.day <= yesterday and s.set_type != "warmup"})
    planned_before = _planned(inp, monday, yesterday)
    done = len({s.session_id for s in inp.sets if monday <= s.day <= day and s.set_type != "warmup"})
    planned = week.detail["planned"]
    tile = Tile("sessions", "Sedute della settimana", float(done), f"di {planned}", ref="sessions_vs_plan_week@1",
                progress=done / planned if planned else None, detail={"planned": planned})
    if planned:
        behind = planned_before - done_before
        tile.status = "ok" if behind <= 0 else "warn"
        tile.sub = "in linea con il programma" if behind <= 0 else f"{behind} da recuperare"
    return tile


def _planned(inp: engine.Inputs, start: date, end: date) -> int:
    return sum(adherence.planned_on(inp.plans, start + timedelta(days=i)) or 0 for i in range((end - start).days + 1))


def sleep_tile(inp: engine.Inputs, day: date) -> Tile:
    m = adherence.sleep_week(inp.sleep, day - timedelta(days=6), day)
    tile = Tile("sleep", "Sonno, media 7 notti", None, "h", ref="sleep_mean_week@1")
    if m is None:
        tile.sub = "nessuna notte registrata"
        return tile
    if m.value is None:
        tile.sub = f"{m.n_obs} notti su 7: troppo poche per la media"
        return tile
    thr = p("sleep_recommended_min_h")
    tile.value, tile.progress = m.value, m.value / thr
    tile.status = "ok" if m.value >= thr else "warn"
    tile.sub = f"{m.n_obs} notti · soglia {thr} h"
    tile.detail = {"threshold": thr, "threshold_ref": "sleep_recommended_min_h", "nights": m.n_obs}
    return tile


def steps_tile(inp: engine.Inputs, day: date) -> Tile:
    """Mean steps of the last 7 days up to yesterday (vs target only when the active nutrition target defines one)."""
    y = day - timedelta(days=1)
    vals = [v for d, v in inp.steps.items() if y - timedelta(days=6) <= d <= y]
    mean = float(np.mean(vals)) if len(vals) >= p("steps_min_days_for_mean") else None
    tile = Tile("steps", "Passi, media 7 giorni", mean, "", ref="steps (dati registrati, media)")
    target = (adherence.plan_on(inp.plans, "nutrition_target", y) or {}).get("steps_target")
    parts = [f"ieri {_fmt(inp.steps[y])}" if y in inp.steps else "ieri non registrati"]
    if mean is None:
        parts.append(f"{len(vals)} giorni su 7: troppo pochi per la media")
    elif target:
        tile.status = "ok" if mean >= target else "warn"
        tile.progress = mean / target
        parts.append(f"target {_fmt(target)}")
        tile.detail = {"target": target, "days": len(vals)}
    tile.sub = " · ".join(parts)
    return tile


def phase_progress(conn: sqlite3.Connection, inp: engine.Inputs, day: date) -> dict | None:
    """Week N of M of the active phase and trend-weight change since its start; before the start, when it begins."""
    row = plan_store.active(conn, "phase", day)
    if row is not None:
        c = plan_store.content(row)
        start = date.fromisoformat(row["valid_from"])
        week = (day - start).days // 7 + 1
        daily = {d: v for d, v in body.daily_weights(inp.weighins).items() if d <= day}
        ema = body.weight_ema_series(daily)
        base = [d for d in ema if d < start]
        change = ema[max(ema)] - ema[max(base)] if base and ema else None
        return {"phase": c["phase"], "week": week, "of": c["max_duration_weeks"], "start": start.isoformat(),
                "weight_change_kg": change, "ref": "weight_ema@1"}
    nxt = conn.execute(
        "SELECT i.number, i.title, json_extract(i.prereg, '$.start_date') AS s FROM intervention i "
        "JOIN v_intervention_status st ON st.id = i.id WHERE st.status = 'approved' AND s > ? ORDER BY s LIMIT 1",
        (day.isoformat(),)).fetchone()
    if nxt:
        return {"upcoming": True, "number": nxt["number"], "title": nxt["title"], "start": nxt["s"]}
    return None


def todo(conn: sqlite3.Connection, day: date) -> list[str]:
    def has(entity: str, d: date) -> bool:
        return bool(conn.execute("SELECT 1 FROM v_current WHERE entity_type = ? AND local_date = ? LIMIT 1",
                                 (entity, d.isoformat())).fetchone())
    out = []
    if not has("body_weight", day):
        out.append("Pesata di oggi")
    if not has("nutrition_day", day - timedelta(days=1)):
        out.append("Cibo di ieri")
    return out


def summary(conn: sqlite3.Connection, day: date, inp: engine.Inputs | None = None) -> dict:
    inp = inp or engine.load_inputs(conn)
    energy, protein = nutrition_tiles(inp, day)
    tiles = [weight_tile(inp, day), energy, protein, sleep_tile(inp, day), sessions_tile(inp, day),
             steps_tile(inp, day)]
    return {"day": day.isoformat(), "tiles": [asdict(t) for t in tiles], "phase": phase_progress(conn, inp, day),
            "todo": todo(conn, day), "session": plan_rules.next_session(conn, day, record=False)}
