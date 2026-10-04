"""Safety layer (docs/architecture.md §12): deterministic rules, tiers, idempotent flags, and a gate.

The system describes patterns and recommends professional evaluation; it never diagnoses.
Tiers: T1 caution (blocks stress-increasing changes) · T2 pause + professional evaluation · T3 urgent.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from askesis.analytics import body, energy
from askesis.analytics.engine import athlete_as_of, load_inputs
from askesis.analytics.params import p
from askesis.core.ids import new_id
from askesis.core.timeutil import iso, now_utc
from askesis.plan import store as plan_store

TIER_ORDER = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}
STRESS_INCREASING = {
    "energy_deficit_increase",
    "training_volume_increase",
    "training_intensity_increase",
    "running_volume_increase",
    "phase_start_fat_loss",
}


@dataclass
class Flag:
    rule_id: str
    tier: str
    local_date: date
    message: str
    signals: dict
    actions: list[str]
    window_key: str  # makes re-evaluation idempotent
    extra: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.rule_id}|{self.window_key}".encode()).hexdigest()


def _rapid_weight_loss(conn, inp, on: date) -> list[Flag]:
    daily = body.daily_weights(inp.weighins)
    thr, severe, weeks = (
        p("safety_rapid_loss_pct_per_week"),
        p("safety_rapid_loss_pct_severe"),
        p("safety_rapid_loss_min_weeks"),
    )
    phase = plan_store.active(conn, "phase", on)
    exclude_before = date.fromisoformat(phase["valid_from"]) + timedelta(days=7) if phase else None
    rates = []
    for k in range(weeks):
        end = on - timedelta(days=7 * k)
        if exclude_before and end - timedelta(days=13) < exclude_before:
            return []  # first diet week (water, glycogen) is excluded from this rule
        r = body.weight_rate(daily, end, 14)
        if not r:
            return []
        rates.append(r[1].value)  # %BW/week
    if all(v <= -thr for v in rates):
        tier = "T2" if rates[0] <= -severe else "T1"
        return [
            Flag(
                "safety.rapid_weight_loss@1",
                tier,
                on,
                f"Perdita di peso più rapida di {thr:g}%/settimana per {weeks} settimane consecutive "
                f"(ultima: {rates[0]:.2f}%/sett).",
                {"weekly_rates_pct": rates},
                [
                    "non aumentare il deficit",
                    "proporre una correzione verso l'alto dell'intake",
                    "verificare segnali di bassa disponibilità energetica",
                ],
                f"{on - timedelta(days=on.weekday())}",
            )
        ]
    return []


def _pain(conn, on: date) -> list[Flag]:
    thr, severe = p("safety_pain_threshold"), p("safety_pain_severe")
    start = (on - timedelta(days=6)).isoformat()
    hits = []
    for r in conn.execute(
        """SELECT id, local_date, json_extract(payload,'$.exercise_raw') ex,
                                    json_extract(payload,'$.pain') pain, json_extract(payload,'$.pain_region') reg
                             FROM v_current WHERE entity_type='set_record' AND local_date BETWEEN ? AND ?
                               AND json_extract(payload,'$.pain') >= ?""",
        (start, on.isoformat(), thr),
    ):
        hits.append({"date": r["local_date"], "score": r["pain"], "where": r["reg"] or r["ex"], "record": r["id"]})
    for r in conn.execute(
        """SELECT id, local_date, payload FROM v_current WHERE entity_type='subjective_checkin'
                             AND local_date BETWEEN ? AND ?""",
        (start, on.isoformat()),
    ):
        for item in json.loads(r["payload"]).get("pain", []) or []:
            if (item.get("score_0_10") or 0) >= thr:
                hits.append(
                    {
                        "date": r["local_date"],
                        "score": item["score_0_10"],
                        "where": item.get("region"),
                        "record": r["id"],
                    }
                )
    if not hits:
        return []
    worst = max(h["score"] for h in hits)
    return [
        Flag(
            "safety.pain@1",
            "T2" if worst >= severe else "T1",
            on,
            f"Dolore ≥ {thr:g}/10 registrato ({len(hits)} volte negli ultimi 7 giorni, massimo {worst:g}/10).",
            {"events": hits},
            [
                "sospendere o sostituire il movimento che provoca dolore",
                "dolore che persiste o peggiora: valutazione da fisioterapista o medico",
            ],
            "|".join(sorted(h["record"] for h in hits)),
        )
    ]


def _health_signals(conn, on: date) -> list[Flag]:
    cv, ed = set(p("safety_cardiovascular_red_flags")), set(p("safety_eating_disorder_signals"))
    out = []
    for r in conn.execute(
        """SELECT id, local_date, payload FROM v_current WHERE entity_type='health_event'
                             AND local_date BETWEEN ? AND ?""",
        ((on - timedelta(days=30)).isoformat(), on.isoformat()),
    ):
        pl = json.loads(r["payload"])
        signals = set(pl.get("red_flags", []) or []) | set(pl.get("signals", []) or [])
        if signals & cv:
            out.append(
                Flag(
                    "safety.cardiovascular@1",
                    "T3",
                    date.fromisoformat(r["local_date"]),
                    "Sintomi cardiovascolari segnalati. Interrompere l'allenamento. Se i sintomi sono in "
                    "corso o gravi: 112. Altrimenti valutazione medica prima di riprendere.",
                    {"signals": sorted(signals & cv), "record": r["id"]},
                    [
                        "stop allenamento",
                        "valutazione medica prima di riprendere",
                        "nessun test massimale finché non chiarito",
                    ],
                    r["id"],
                )
            )
        if signals & ed:
            out.append(
                Flag(
                    "safety.eating_disorder_signals@1",
                    "T2",
                    date.fromisoformat(r["local_date"]),
                    "Segnali che meritano attenzione nel rapporto con cibo/esercizio. I target restrittivi "
                    "sono sospesi; è possibile ridurre il tracking. Utile parlarne con un professionista.",
                    {"signals": sorted(signals & ed), "record": r["id"]},
                    [
                        "sospendere target calorici restrittivi",
                        "offrire riduzione del tracking",
                        "suggerire un professionista (medico, psicologo, dietista)",
                    ],
                    r["id"],
                )
            )
    return out


def _low_intake(inp, on: date) -> list[Flag]:
    daily = body.daily_weights(inp.weighins)
    means = energy.intake_mean(inp.nutrition, on, 7)
    if not means or means[0].value is None or not daily:
        return []
    past = [d for d in daily if d <= on]  # never a weigh-in after the evaluated date
    if not past:
        return []
    ree = energy.mifflin(daily[max(past)], athlete_as_of(inp, on))
    if ree is None:
        return []
    ratio = means[0].value / ree
    if ratio < p("safety_low_intake_ratio_to_ree"):
        return [
            Flag(
                "safety.low_energy_intake@1",
                "T1",
                on,
                "Intake medio della settimana sotto il metabolismo a riposo stimato: possibile bassa "
                "disponibilità energetica (indicatore indiretto, non una diagnosi).",
                {"intake_mean_7d": means[0].value, "ree_estimate": ree, "ratio": ratio},
                ["non aumentare il deficit", "rivalutare il target", "monitorare fatica, performance, malattie"],
                f"{on - timedelta(days=on.weekday())}",
            )
        ]
    return []


def evaluate(conn: sqlite3.Connection, on: date, cutoff: datetime | None = None, persist: bool = True) -> list[Flag]:
    inp = load_inputs(conn, cutoff)
    flags = _rapid_weight_loss(conn, inp, on) + _pain(conn, on) + _health_signals(conn, on) + _low_intake(inp, on)
    if persist:
        with conn:
            for f in flags:
                conn.execute(
                    """INSERT OR IGNORE INTO safety_flag(id, rule_id, tier, local_date, fingerprint, message, signals,
                           actions, opened_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id(),
                        f.rule_id,
                        f.tier,
                        f.local_date.isoformat(),
                        f.fingerprint,
                        f.message,
                        json.dumps(f.signals, default=str),
                        json.dumps(f.actions, ensure_ascii=False),
                        iso(now_utc()),
                    ),
                )
    return flags


def open_flags(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM v_safety_open ORDER BY tier DESC, local_date DESC").fetchall()


def resolve(conn: sqlite3.Connection, flag_id: str, resolution: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO safety_resolution(id, flag_id, resolution, at) VALUES (?,?,?,?)",
            (new_id(), flag_id, resolution, iso(now_utc())),
        )


class SafetyBlock(Exception):
    pass


def gate(conn: sqlite3.Connection, category: str, override_reason: str | None = None) -> None:
    """Raise SafetyBlock if open flags forbid an intervention of this category."""
    flags = open_flags(conn)
    worst = max((TIER_ORDER[f["tier"]] for f in flags), default=0)
    if worst >= 3:
        raise SafetyBlock("flag T3 aperto: nessun nuovo intervento finché non è risolto con valutazione medica")
    if worst >= 1 and category in STRESS_INCREASING and not override_reason:
        raise SafetyBlock(
            f"flag di safety aperti (max {['T0', 'T1', 'T2', 'T3'][worst]}): interventi di tipo '{category}' bloccati"
        )
