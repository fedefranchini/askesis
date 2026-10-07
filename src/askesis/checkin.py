"""Daily subjective questionnaire: the items, their anchored scales and direction, dictation, merging, response quality.

One definition for every interface (dashboard, CLI, chat dictation). Each scale measures "how much" of its item, so the
direction differs between items: `direction` tells the indices how to read it (higher_better, higher_worse, or neutral
when more is neither good nor bad by itself). Answers are stored in `subjective_checkin` records:
- morning: one record per day (moment = morning);
- after a session: one record per day and session kind (moment = post_session, session_kind = strength | run).
A new answer for the same day and moment supersedes the current record with the merged answers (append-only): items
left blank keep their previous value.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from askesis.analytics.params import p


@dataclass(frozen=True)
class Item:
    key: str  # dictation keyword and form name
    field: str  # payload field
    label: str
    question: str
    lo: int
    hi: int
    anchors: dict[int, str]
    direction: str  # higher_better | higher_worse | neutral
    moment: str  # morning | post_session
    note: str = ""

    def describe(self, v: int) -> str:
        """Words for a value: its anchor, or the two anchors around it."""
        if v in self.anchors:
            return self.anchors[v]
        below = max(k for k in self.anchors if k < v)
        above = min(k for k in self.anchors if k > v)
        return f"tra «{self.anchors[below]}» e «{self.anchors[above]}»"

    def descriptions(self) -> dict[int, str]:
        return {v: self.describe(v) for v in range(self.lo, self.hi + 1)}


ITEMS: tuple[Item, ...] = (
    Item("sonno", "sleep_quality_1_10", "Sonno", "Come hai dormito stanotte?", 1, 10,
         {1: "Pessimo, quasi non ho dormito", 3: "Scarso, risvegli frequenti o sonno agitato", 5: "Discreto",
          7: "Buono", 10: "Ottimo, profondo e riposante"}, "higher_better", "morning"),
    Item("stanchezza", "fatigue_1_10", "Stanchezza", "Quanto ti senti stanco adesso?", 1, 10,
         {1: "Poca, pieno di energia", 3: "Leggera", 5: "Normale", 7: "Stanco", 10: "Tantissima, esausto"},
         "higher_worse", "morning"),
    Item("indolenzimento", "soreness_1_10", "Indolenzimento", "Quanto sono indolenziti i muscoli?", 1, 10,
         {1: "Nessuno", 3: "Leggero, lo noto solo se ci penso", 5: "Moderato, lo sento nei movimenti",
          7: "Forte, limita alcuni movimenti", 10: "Tantissimo, limita anche i movimenti di tutti i giorni"},
         "higher_worse", "morning",
         "Un dolore localizzato a un'articolazione o una fitta non va qui: si registra come dolore 0–10."),
    Item("stress", "stress_1_10", "Stress", "Quanto ti senti sotto pressione (lavoro, studio, vita)?", 1, 10,
         {1: "Nessuno, rilassato", 3: "Poco", 5: "Abbastanza, gestibile", 7: "Molto",
          10: "Tantissimo, faccio fatica a gestirlo"}, "higher_worse", "morning"),
    Item("umore", "mood_1_10", "Umore", "Com'è il tuo umore?", 1, 10,
         {1: "Terribile", 3: "Giù", 5: "Neutro", 7: "Buono", 10: "Ottimo"}, "higher_better", "morning"),
    Item("fame", "hunger_1_10", "Fame", "Quanta fame hai avuto ieri, nell'arco della giornata?", 1, 10,
         {1: "Poca", 3: "Meno del solito", 5: "Normale, gestibile", 7: "Molta, ci pensavo spesso",
          10: "Moltissima, difficile da gestire"}, "neutral", "morning"),
    Item("voglia", "motivation_1_10", "Voglia di allenarti", "Quanta voglia hai di allenarti oggi?", 1, 10,
         {1: "Poca", 3: "Scarsa", 5: "Normale", 7: "Tanta", 10: "Tantissima, non vedo l'ora"},
         "higher_better", "morning"),
    Item("fatica", "session_rpe_cr10", "Fatica della seduta", "Quanto è stata dura la seduta nel complesso?", 0, 10,
         {0: "Riposo", 1: "Molto, molto facile", 2: "Facile", 3: "Moderata", 4: "Un po' dura", 5: "Dura",
          7: "Molto dura", 10: "Massimale"}, "neutral", "post_session",
         "Scala CR-10 di Foster: rispondi circa 30 minuti dopo la fine della seduta."),
    Item("qualita", "session_quality_1_10", "Qualità della seduta", "Com'è andata la seduta?", 1, 10,
         {1: "Terribile, ho dovuto ridurre o interrompere", 3: "Sotto le attese", 5: "Come previsto",
          7: "Buona, sopra le attese", 10: "Perfetta, tutto facile e preciso"}, "higher_better", "post_session"),
)
BY_KEY = {i.key: i for i in ITEMS}
BY_FIELD = {i.field: i for i in ITEMS}
MORNING = tuple(i for i in ITEMS if i.moment == "morning")
POST = tuple(i for i in ITEMS if i.moment == "post_session")
SESSION_KINDS = {"pesi": "strength", "corsa": "run"}
KIND_LABEL = {"strength": "pesi", "run": "corsa"}
ALIASES = {"stanco": "stanchezza", "doms": "indolenzimento", "motivazione": "voglia", "rpe": "fatica",
           "qualità": "qualita"}
MINUTES_MAX = 600


class CheckinError(ValueError):
    pass


def _key(word: str) -> str:
    w = word.lower()
    return ALIASES.get(w, w)


def _pairs(tokens: list[str]) -> list[tuple[str, str]]:
    if len(tokens) % 2:
        raise CheckinError("scrivi ogni voce seguita dal suo valore, es. «sonno 7 umore 6»")
    return [(tokens[i], tokens[i + 1]) for i in range(0, len(tokens), 2)]


def _value(item: Item, raw: str) -> int:
    if not re.fullmatch(r"\d{1,2}", raw):
        raise CheckinError(f"{item.label.lower()}: valore intero da {item.lo} a {item.hi}, non {raw!r}")
    v = int(raw)
    if not item.lo <= v <= item.hi:
        raise CheckinError(f"{item.label.lower()}: valore da {item.lo} a {item.hi}, non {v}")
    return v


def parse_morning(text: str) -> dict:
    """'sonno 7 stanchezza 4 …' → payload fields (any subset, each item at most once)."""
    out: dict = {}
    for word, raw in _pairs(text.split()):
        item = BY_KEY.get(_key(word))
        if item is None or item.moment != "morning":
            raise CheckinError(f"voce del check-in non riconosciuta: {word!r} "
                               f"(voci: {', '.join(i.key for i in MORNING)})")
        if item.field in out:
            raise CheckinError(f"{item.label.lower()} indicato due volte")
        out[item.field] = _value(item, raw)
    if not out:
        raise CheckinError("check-in vuoto: indica almeno una voce, es. «checkin sonno 7»")
    return out


def parse_session(text: str) -> dict:
    """'pesi fatica 7 qualità 8 70min' → payload fields; the session kind comes first."""
    tokens = text.split()
    if not tokens or tokens[0].lower() not in SESSION_KINDS:
        raise CheckinError("seduta: indica prima il tipo, «pesi» o «corsa» (es. «seduta pesi fatica 7»)")
    out: dict = {"session_kind": SESSION_KINDS[tokens[0].lower()]}
    rest = []
    for t in tokens[1:]:
        m = re.fullmatch(r"(\d{1,3})(?:min|m|')", t.lower())
        if m:
            minutes = int(m[1])
            if not 1 <= minutes <= MINUTES_MAX or "session_minutes" in out:
                raise CheckinError(f"seduta: durata non valida {t!r}")
            out["session_minutes"] = minutes
        else:
            rest.append(t)
    for word, raw in _pairs(rest):
        item = BY_KEY.get(_key(word))
        if item is None or item.moment != "post_session":
            raise CheckinError(f"voce della seduta non riconosciuta: {word!r} (voci: fatica, qualità, durata in min)")
        if item.field in out:
            raise CheckinError(f"{item.label.lower()} indicato due volte")
        out[item.field] = _value(item, raw)
    if len(out) == 1:
        raise CheckinError("seduta: indica almeno fatica, qualità o durata")
    return out


# ------------------------------------------------------------------ storage
def _current(conn: sqlite3.Connection, day: date, moment: str, kind: str | None = None) -> sqlite3.Row | None:
    rows = conn.execute(
        "SELECT id, payload FROM v_current WHERE entity_type = 'subjective_checkin' AND local_date = ? "
        "AND json_extract(payload, '$.moment') = ? ORDER BY recorded_at DESC", (day.isoformat(), moment)).fetchall()
    for r in rows:
        if kind is None or json.loads(r["payload"]).get("session_kind") == kind:
            return r
    return None


def merge(conn: sqlite3.Connection, records: list[dict]) -> list[dict]:
    """New answers for a day and moment that already has a record: merged into a new version that supersedes it."""
    out = []
    for rec in records:
        pl = rec.get("payload") or {}
        if rec.get("entity_type") == "subjective_checkin" and pl.get("moment") and not rec.get("supersedes_id"):
            day = date.fromisoformat(str(rec["local_date"]))
            cur = _current(conn, day, pl["moment"], pl.get("session_kind"))
            if cur is not None:
                rec = rec | {"payload": json.loads(cur["payload"]) | pl, "supersedes_id": cur["id"]}
        out.append(rec)
    return out


def today_answers(conn: sqlite3.Connection, day: date) -> dict:
    """Current answers for a day: {'morning': payload | {}, 'strength': payload | {}, 'run': payload | {}}."""
    m = _current(conn, day, "morning")
    out = {"morning": json.loads(m["payload"]) if m else {}}
    for kind in KIND_LABEL:
        r = _current(conn, day, "post_session", kind)
        out[kind] = json.loads(r["payload"]) if r else {}
    return out


def describe(payload: dict) -> str:
    """Compact read-back of a questionnaire record."""
    parts = []
    for f, v in payload.items():
        item = BY_FIELD.get(f)
        if item is not None:
            scale = " CR-10" if item.lo == 0 else ""  # no "/10": next to a date it reads like one
            parts.append(f"{item.label.lower()} {v}{scale} ({item.describe(int(v)).lower()})")
    if payload.get("session_minutes"):
        parts.append(f"{payload['session_minutes']} min")
    return " · ".join(parts)


# ------------------------------------------------------------------ response quality
def response_quality(conn: sqlite3.Connection, day: date) -> dict:
    """How regularly the morning questionnaire is filled in, and whether answers look automatic (identical runs).

    Uses only days up to `day`. Thresholds are versioned parameters (engineering choices).
    """
    window = int(p("checkin_quality_window_days"))
    start = day - timedelta(days=window - 1)
    rows = conn.execute(
        "SELECT local_date, payload FROM v_current WHERE entity_type = 'subjective_checkin' AND local_date BETWEEN ? "
        "AND ? AND json_extract(payload, '$.moment') = 'morning' ORDER BY local_date", (start.isoformat(),
                                                                                       day.isoformat())).fetchall()
    by_day = {r["local_date"]: {k: v for k, v in json.loads(r["payload"]).items() if k in BY_FIELD}
              for r in rows}
    answered = [d for d, v in by_day.items() if v]
    streak, prev = 0, None
    for d in sorted(answered, reverse=True):  # identical answers on consecutive answered days, newest first
        if prev is None or by_day[d] == prev:
            streak += 1
            prev = by_day[d]
        else:
            break
    flag = int(p("checkin_identical_streak_days"))
    week = [d for d in answered if d >= (day - timedelta(days=6)).isoformat()]
    return {"window_days": window, "answered": len(answered), "last7": len(week),
            "identical_streak": streak if streak >= 2 else 0,
            "identical_warning": streak >= flag and len(by_day[answered[-1]]) >= 3 if answered else False}
