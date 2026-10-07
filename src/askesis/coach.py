"""The coach's texts and the decisions waiting for the athlete, for any interface (dashboard, CLI).

Texts: the files in reports/ (evening check comment, session comment, weekly review, monthly retrospective,
proposals) are re-checked by
the validator every time they are shown: only a text that passes now is "validated"; anything else is shown as
"da verificare" with the unsupported points. A `.DA-VERIFICARE.md` file superseded by a newer final file is hidden.

Decisions: pending intervention proposals and calorie-rule proposals. A decision needs the athlete's explicit text,
"approvo" or "rifiuto" (one of the two, never both), and goes through the same functions as the CLI: registry.approve
(safety gate, domain check) / registry.reject, calorie_rule.apply. Applying a calorie proposal is refused while a safety
flag is open (stricter than the CLI: the rule itself never proposes a cut with open flags).
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from askesis.core.timeutil import now_utc
from askesis.interventions import present
from askesis.interventions import registry as reg
from askesis.plan import calorie_rule
from askesis.safety import rules as safety
from askesis.validation import textcheck

NAME = re.compile(r"^(daily|review|retro|proposta|seduta)[\w.-]*\.md$")
PENDING_SUFFIX = ".DA-VERIFICARE.md"
KINDS = {"daily": "Controllo serale", "review": "Review settimanale", "retro": "Retrospettiva mensile",
         "proposta": "Proposta", "seduta": "Commento seduta"}


@dataclass
class Doc:
    name: str
    kind: str
    label: str
    validated: bool
    issues: list[str] = field(default_factory=list)
    body: str = ""
    mtime: float = 0.0


def _strip_pending_header(text: str) -> str:
    """Body of a `.DA-VERIFICARE.md` file without the validator's quoted header."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (lines[i].startswith(">") or not lines[i].strip()):
        i += 1
    return "\n".join(lines[i:])


def _label(name: str) -> str:
    stem = name.removesuffix(PENDING_SUFFIX).removesuffix(".md")
    kind = stem.split("-", 1)[0]
    rest = stem.split("-", 1)[1] if "-" in stem else ""
    extra = " · commento" if "commento" in rest else ""
    extra += " · versione manuale" if rest.endswith(".manual") else ""
    when = re.sub(r"-commento|\.manual", "", rest)
    return f"{KINDS.get(kind, kind)} {when}{extra}".strip()


def documents(reports: Path, conn: sqlite3.Connection | None) -> list[Doc]:
    """All coach texts, newest first, each re-validated now."""
    if not reports.exists():
        return []
    files = {p.name: p for p in reports.glob("*.md") if NAME.match(p.name)}
    out = []
    for name, path in files.items():
        if name.endswith(PENDING_SUFFIX):
            final = files.get(name.removesuffix(PENDING_SUFFIX) + ".md")
            if final is not None and final.stat().st_mtime >= path.stat().st_mtime:
                continue  # superseded by a validated final version
            body = _strip_pending_header(path.read_text())
        else:
            body = path.read_text()
        m = re.match(r"^proposta-intervento-(\d+)\.md$", name)
        row = reg.get(conn, int(m[1])) if m and conn is not None else None
        if row is not None:  # a proposal is shown from the registry and checked like at registration
            data = json.loads(row["prereg"])
            body = present.render_proposal(row["title"], data, None)
            res = textcheck.check_prereg({**data, "title": row["title"]}, conn)
        else:
            res = textcheck.validate(body, conn)
        validated = res.ok and not name.endswith(PENDING_SUFFIX)
        issues = [i.render().removeprefix("- ") for i in res.issues]
        if name.endswith(PENDING_SUFFIX) and not issues:
            issues = ["testo mai convalidato: rigenerarlo o validarlo con bin/ak validate"]
        out.append(Doc(name, name.split("-", 1)[0], _label(name), validated, issues, body, path.stat().st_mtime))
    return sorted(out, key=lambda d: d.mtime, reverse=True)


# ------------------------------------------------------------------ pending decisions
@dataclass
class Pending:
    kind: str  # intervention | calorie
    ref: str  # intervention number, or rule execution id
    title: str
    summary: str
    body: str  # Markdown shown to the athlete
    validated: bool
    issues: list[str] = field(default_factory=list)


def _calorie_decided(conn: sqlite3.Connection, exec_id: str) -> bool:
    applied = conn.execute("SELECT 1 FROM intervention_event WHERE event = 'amended' AND "
                           "json_extract(payload, '$.rule_execution') = ?", (exec_id,)).fetchone()
    rejected = conn.execute("SELECT 1 FROM decision WHERE question = ?", (_calorie_question(exec_id),)).fetchone()
    return bool(applied or rejected)


def _calorie_question(exec_id: str) -> str:
    return f"Regola calorica: proposta {exec_id}"


def pending(conn: sqlite3.Connection) -> list[Pending]:
    out = []
    for r in conn.execute("SELECT i.number, i.title, i.prereg FROM intervention i JOIN v_intervention_status s "
                          "ON s.id = i.id WHERE s.status = 'proposed' ORDER BY i.number"):
        data = json.loads(r["prereg"])
        check = textcheck.check_prereg({**data, "title": r["title"]}, conn)
        out.append(Pending("intervention", str(r["number"]), f"Intervento n. {r['number']}: {r['title']}",
                           f"avvio {_it(data['start_date'])} · valutazione {_it(data['evaluation_date'])}",
                           present.render_proposal(r["title"], data, None), check.ok,
                           [i.render().removeprefix("- ") for i in check.issues]))
    latest = conn.execute("SELECT * FROM rule_execution WHERE rule_id LIKE 'calorie_adjustment@%' "
                          "ORDER BY local_date DESC, recorded_at DESC LIMIT 1").fetchone()
    if latest is not None:  # only the latest evaluation counts: an older proposal is superseded by a newer run
        o = json.loads(latest["output"])
        if o.get("status") == "propose" and not _calorie_decided(conn, latest["id"]):
            i = o.get("inputs") or {}
            body = "\n".join([
                f"# Proposta della regola calorica — {latest['local_date']}", "",
                f"Target energetico: **{o['current_kcal']:g} → {o['proposed_kcal']:g} kcal/die** "
                f"({o['change_kcal']:+g} kcal), regola `{o['rule']}`.", "",
                *(f"- {x}" for x in o.get("reasons", [])),
                f"- velocità del peso {i['rate_pct_per_week']:+.2f} %/settimana ({i.get('rate_metric', '')}), "
                f"finestra {i['window'][0]} → {i['window'][1]}" if i.get("rate_pct_per_week") is not None else "",
                f"- soglia minima di safety {o['floor_kcal']:.0f} kcal" if o.get("floor_kcal") else "", "",
                "Calcolata dal codice dai dati registrati. Si applica dal giorno dopo l'approvazione."])
            out.append(Pending("calorie", latest["id"], "Modifica calorica proposta",
                               f"{o['current_kcal']:g} → {o['proposed_kcal']:g} kcal/die", body, True))
    return out


def _it(d) -> str:
    return date.fromisoformat(str(d)).strftime("%d/%m/%Y")


class DecisionError(ValueError):
    pass


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def intent(text: str) -> str:
    """'approve' or 'reject' from the athlete's explicit words; anything ambiguous is refused."""
    t = _norm(text)
    yes, no = "approvo" in t, "rifiuto" in t
    if yes == no:
        raise DecisionError("scrivi «approvo» oppure «rifiuto» (uno dei due)")
    return "approve" if yes else "reject"


def decide(conn: sqlite3.Connection, kind: str, ref: str, text: str, reason: str = "", today: date | None = None,
           at: datetime | None = None) -> str:
    """Record the athlete's decision through the same core functions as the CLI. Returns a short confirmation."""
    text, reason = text.strip(), reason.strip()
    action = intent(text)
    item = next((p for p in pending(conn) if p.kind == kind and p.ref == ref), None)
    if item is None:
        raise DecisionError("proposta non più in attesa (già decisa o sostituita)")
    if kind == "intervention":
        row = reg.get(conn, int(ref))
        if action == "reject":
            reg.reject(conn, row["id"], text, reason or "rifiutata dall'atleta (dashboard)", at=at)
            return f"Intervento n. {ref} rifiutato: decisione registrata."
        if not item.validated:
            raise DecisionError("proposta da verificare: non si può approvare finché il validatore non la accetta")
        try:
            versions = reg.approve(conn, row["id"], text, reason or "approvata dall'atleta (dashboard)", "moderate",
                                   at=at)
        except safety.SafetyBlock as exc:
            raise DecisionError(f"bloccata dalla safety: {exc}") from exc
        if reg.status(conn, row["id"]) == "approved":
            start = json.loads(row["prereg"])["start_date"]
            return (f"Intervento n. {ref} approvato. Si attiva dal {start} con bin/ak intervention activate {ref} "
                    "(valori calcolati e congelati in quel momento).")
        return f"Intervento n. {ref} approvato e attivato ({len(versions)} versioni del piano)."
    if kind == "calorie":
        if action == "reject":
            with conn:
                reg._record_decision(conn, _calorie_question(ref), ["applica", "non applicare"], None,
                                     reason or "rifiutata dall'atleta (dashboard)", [], "moderate", "rejected", text,
                                     at or now_utc())
            return "Modifica calorica rifiutata: decisione registrata, il target non cambia."
        if safety.open_flags(conn):
            raise DecisionError("c'è un flag di safety aperto: nessuna modifica calorica finché non è risolto")
        day = (today or date.today()) + timedelta(days=1)  # same default as the CLI: from tomorrow
        calorie_rule.apply(conn, ref, text, day, at=at)
        return f"Modifica calorica approvata: nuovo target dal {day.strftime('%d/%m')}."
    raise DecisionError("tipo di proposta sconosciuto")
