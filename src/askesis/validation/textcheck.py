"""Validator for assistant-generated texts (review comments, retrospectives, intervention proposals).

"Critical guarantees are enforced by code": a number in a generated text must be traceable.

Reference syntax inside texts:
  [claim:<id>]      evidence claim (knowledge/evidence/claims.yaml, plus private evidence if present)
  [param:<id>]      parameter (knowledge/parameters/*.yaml)
  `metric_id@N`     derived metric (metric_value table)
  `rule_id@N`       versioned rule (knowledge/rules/*.yaml), e.g. its preconditions and limits
  [record:<id>]     RAW record (full id or its last 8 characters)
  [flag:<id>]       safety flag (message and signals)
Table rows inherit the references written in the table's header row.

Rules (per segment = paragraph, list item or table row):
  1. every number with a unit (kg, kcal, g/kg, %, km, …) or a decimal must match the value of a reference in the
     same segment (claim text, parameter value, metric value, record payload), with rounding tolerance;
  2. every cited claim/param/metric/record id must exist;
  3. every cited source (author–year, DOI) must exist in the evidence KB.
Dates, ISO weeks, times, versions, plain counts and years are ignored. The check is deterministic.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from askesis.analytics.params import all_params
from askesis.config import ROOT

UNITS = (r"kcal/die|kcal|g/kg(?:/die)?|g/die|kg/sett|%/sett|%|kg|km|cm|bpm|min|g|h|ore|s")
NUM = r"\d+(?:[.,]\d+)*"
QUANTITY = re.compile(
    rf"(?<![\w@./:-])(?P<a>[+−-]?{NUM})(?:\s*(?:–|-|—|e|to)\s*(?P<b>[+−-]?{NUM}))?\s*(?P<unit>{UNITS})(?![\w])",
    re.I,
)
DECIMAL = re.compile(r"(?<![\w@./:-])(?P<a>\d+,\d+)(?![\w,/])")
CLAIM_REF = re.compile(r"\[claim:([a-z0-9_.]+)\]", re.I)
PARAM_REF = re.compile(r"\[param:([a-z0-9_]+)\]", re.I)
RECORD_REF = re.compile(r"\[record:([0-9a-f-]{8,36})\]", re.I)
FLAG_REF = re.compile(r"\[flag:([0-9a-f-]{8,36})\]", re.I)
METRIC_REF = re.compile(r"`([a-z0-9_]+)@(\d+)`")
DOI = re.compile(r"\b10\.\d{4,9}/[^\s)\]>,;]+", re.I)
MONTHS = {"gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre",
          "novembre", "dicembre", "january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december", "settimana", "fase", "intervento", "review"}
CITATION = re.compile(
    r"(?P<who>[A-ZÀ-Ý][A-Za-zà-ÿ'\-]+)(?:\s+(?:et al\.?|&\s*[A-ZÀ-Ý][A-Za-zà-ÿ'\-]+|e\s+[A-ZÀ-Ý][A-Za-zà-ÿ'\-]+))?,?\s+"
    r"(?P<year>(?:19|20)\d{2})"
)
STRIP = [
    re.compile(r"\]\((?:https?://|mailto:)[^)]*\)"),  # markdown link targets (keep link text)
    re.compile(r"https?://\S+"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),  # ISO dates
    re.compile(r"\b\d{4}-W\d{2}\b|\bW\d{2}\b", re.I),  # ISO weeks
    re.compile(r"\b\d{1,2}:\d{2}\b"),  # times
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),  # d/m dates
    re.compile(r"`[^`]*`"),  # code spans (metric refs captured before stripping)
    re.compile(r"\[(?:claim|param|record|flag):[^\]]*\]", re.I),
]


@dataclass
class Issue:
    segment: str
    problem: str

    def render(self) -> str:
        seg = self.segment if len(self.segment) <= 160 else self.segment[:157] + "…"
        return f"- {self.problem} — «{seg}»"


@dataclass
class Result:
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


# ------------------------------------------------------------------ knowledge sources
def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) if path.exists() else {}


@lru_cache(maxsize=1)
def _kb() -> tuple[dict[str, dict], dict[str, dict]]:
    """(claims by id, sources by id), public KB plus private evidence files if present."""
    claims, sources = {}, {}
    files = [ROOT / "knowledge/evidence/claims.yaml", ROOT / "knowledge/evidence/sources.yaml",
             *sorted((ROOT / "private/evidence").glob("*.yaml"))]
    for f in files:
        data = _load_yaml(f)
        for c in data.get("claims", []) or []:
            claims[c["id"]] = c
        for s in data.get("sources", []) or []:
            sources[s["id"]] = s
    return claims, sources


@lru_cache(maxsize=1)
def _rules() -> dict[tuple[str, int], dict]:
    out = {}
    for f in sorted((ROOT / "knowledge/rules").glob("*.yaml")):
        data = _load_yaml(f)
        if data.get("id") is not None:
            out[(data["id"], int(data.get("version", 0)))] = data
    return out


def clear_cache() -> None:
    _kb.cache_clear()
    _rules.cache_clear()


def parse_number(token: str) -> list[float]:
    """Italian/English tolerant: '68,40'→68.4; '1.900'→1900 or 1.9; '1.5'→1.5; '−0,5'→-0.5."""
    t = token.replace("−", "-").replace("+", "")
    out: list[float] = []
    if re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", t):  # Italian thousands separator
        out.append(float(t.replace(".", "")))
    if "," in t and "." in t:
        out.append(float(t.replace(".", "").replace(",", ".")))
    elif "," in t:
        out.append(float(t.replace(",", ".")))
    else:
        out.append(float(t))
    return out


def _decimals(token: str) -> int:
    t = token.replace("−", "-")
    if re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", t):
        return 0
    m = re.search(r"[.,](\d+)$", t)
    return len(m.group(1)) if m else 0


def _numbers_in(obj) -> list[float]:
    """All numbers found in a value or text (claims are written with decimal commas and dashes)."""
    if obj is None:
        return []
    if isinstance(obj, bool):
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in _numbers_in(v)]
    if isinstance(obj, (list, tuple)):
        return [x for v in obj for x in _numbers_in(v)]
    vals = []
    for tok in re.findall(rf"[+−-]?{NUM}", str(obj)):
        try:
            vals += parse_number(tok)
        except ValueError:
            continue
    return vals


def _matches(token: str, candidates: list[float], percent_scale: bool = False) -> bool:
    nd = _decimals(token)
    tol = 0.5 * 10 ** (-nd) + 1e-9
    for v in parse_number(token):
        for c in candidates:
            for cc in ((c, c * 100) if percent_scale else (c,)):
                if abs(abs(v) - abs(cc)) <= tol or abs(v - cc) <= tol:
                    return True
    return False


# ------------------------------------------------------------------ segmenting
def segments(text: str) -> list[str]:
    segs, cur = [], []
    for raw in text.splitlines():
        line = raw.rstrip()
        starts_new = (not line.strip() or re.match(r"\s*([-*+]|\d+\.|\|)\s", line) or line.startswith("#")
                      or line.startswith(">"))
        if starts_new and cur:
            segs.append(" ".join(cur))
            cur = []
        if line.strip():
            cur.append(line.strip())
        if line.startswith(("#", "|")) and cur:
            segs.append(" ".join(cur))
            cur = []
    if cur:
        segs.append(" ".join(cur))
    return segs


# ------------------------------------------------------------------ validation
def validate(text: str, conn: sqlite3.Connection | None = None) -> Result:
    claims, sources = _kb()
    params = all_params()
    res = Result()
    dois = {str(s.get("doi", "")).lower() for s in sources.values() if s.get("doi")}

    header_refs = ""
    segs = segments(text)
    for i, seg in enumerate(segs):
        if seg.startswith("#"):
            continue
        if seg.startswith("|"):
            if re.fullmatch(r"\|[\s:|-]+\|?", seg):
                continue  # separator row
            if i + 1 < len(segs) and re.fullmatch(r"\|[\s:|-]+\|?", segs[i + 1]):
                header_refs = " ".join(m.group(0) for m in re.finditer(
                    r"`[a-z0-9_]+@\d+`|\[(?:claim|param|record|flag):[^\]]*\]", seg, re.I))
                continue  # header row: its references apply to the rows below
            seg = f"{seg} {header_refs}" if header_refs else seg
        else:
            header_refs = ""
        support: list[tuple[str, list[float], bool]] = []  # (label, values, percent_scale)
        for cid in CLAIM_REF.findall(seg):
            c = claims.get(cid)
            if c is None:
                res.issues.append(Issue(seg, f"claim inesistente: {cid}"))
                continue
            support.append((f"claim:{cid}", _numbers_in([c.get("claim"), c.get("notes"), c.get("limitations"),
                                                         c.get("population_scope")]), False))
        for pid in PARAM_REF.findall(seg):
            p = params.get(pid)
            if p is None:
                res.issues.append(Issue(seg, f"parametro inesistente: {pid}"))
                continue
            support.append((f"param:{pid}", _numbers_in([p.get("value"), p.get("sd")]), False))
        for mid, ver in METRIC_REF.findall(seg):
            rule = _rules().get((mid, int(ver)))
            if rule is not None:
                support.append((f"{mid}@{ver}", _numbers_in(rule), True))
                continue
            if conn is None:
                res.issues.append(Issue(seg, f"metrica {mid}@{ver} non verificabile (nessun database)"))
                continue
            rows = conn.execute("SELECT value, lo, hi, detail FROM metric_value WHERE metric_id = ? AND version = ?",
                                (mid, int(ver))).fetchall()
            if not rows:
                res.issues.append(Issue(seg, f"metrica senza valori registrati: {mid}@{ver}"))
                continue
            vals = [x for r in rows for x in (r["value"], r["lo"], r["hi"]) if x is not None]
            for r in rows:
                if r["detail"]:
                    vals += _numbers_in(json.loads(r["detail"]))
            support.append((f"{mid}@{ver}", vals, True))
        for rid in RECORD_REF.findall(seg):
            row = None
            if conn is not None:
                row = conn.execute("SELECT payload FROM raw_record WHERE id = ? OR id LIKE ?",
                                   (rid, f"%{rid}")).fetchone()
            if row is None:
                res.issues.append(Issue(seg, f"record inesistente: {rid}"))
                continue
            support.append((f"record:{rid}", _numbers_in(json.loads(row["payload"])), False))

        for fid in FLAG_REF.findall(seg):
            row = None
            if conn is not None:
                row = conn.execute("SELECT message, signals FROM safety_flag WHERE id = ? OR id LIKE ?",
                                   (fid, f"%{fid}")).fetchone()
            if row is None:
                res.issues.append(Issue(seg, f"flag inesistente: {fid}"))
                continue
            support.append((f"flag:{fid}", _numbers_in([row["message"], json.loads(row["signals"])]), False))

        for doi in DOI.findall(seg):
            if doi.rstrip(".").lower() not in dois:
                res.issues.append(Issue(seg, f"DOI non presente nella KB: {doi}"))
        cited = seg
        for rx in STRIP:
            cited = rx.sub(" ", cited)
        for m in CITATION.finditer(cited):
            who, year = m.group("who"), m.group("year")
            if who.lower() in MONTHS:
                continue
            found = any(
                (str(s.get("citation", "")).lower().startswith(who.lower()) and year in str(s.get("citation", "")))
                or s["id"].lower().startswith(f"{who.lower()}{year}")
                for s in sources.values()
            )
            if not found:
                res.issues.append(Issue(seg, f"fonte citata non presente nella KB: {who} {year}"))

        body = cited
        tokens: list[str] = []
        for q in QUANTITY.finditer(body):
            tokens += [t for t in (q.group("a"), q.group("b")) if t]
        for d in DECIMAL.finditer(QUANTITY.sub(" ", body)):
            tokens.append(d.group("a"))
        for tok in tokens:
            if not support:
                res.issues.append(Issue(seg, f"numero senza riferimento: {tok}"))
            elif not any(_matches(tok, vals, pct) for _, vals, pct in support):
                refs = ", ".join(label for label, _, _ in support)
                res.issues.append(Issue(seg, f"valore {tok} non corrisponde ai riferimenti ({refs})"))
    return res


PREREG_TEXT = ("hypothesis", "reason", "change_description", "success_criteria", "stop_criteria", "expert_opinion",
               "personal_preference")
LABELS = ("expert_opinion", "personal_preference", "engineering_choice")


def _leaves(obj, path: str):
    """(dotted path, number) for every numeric leaf. Plan changes are addressed by name."""
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        yield path, float(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _leaves(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _leaves(v, f"{path}.{i}")


def _covers(key: str, path: str) -> bool:
    """`key` is a dotted path prefix; a `*` segment matches any single segment (e.g. a list index)."""
    k, p_ = key.split("."), path.split(".")
    return len(k) <= len(p_) and all(a == "*" or a == b for a, b in zip(k, p_, strict=False))


def structured_numbers(prereg: dict):
    for c in prereg.get("plan_changes") or []:
        yield from _leaves(c.get("content"), f"plan_changes.{c.get('name')}.content")
    for key in ("expected_outcome", "min_adherence"):
        yield from _leaves(prereg.get(key), key)
    for d in (prereg.get("derived") or {}).get("values", []):
        yield from _leaves(d.get("args"), f"derived.{d.get('name')}.args")


def check_source(value: float, source: str, conn: sqlite3.Connection | None = None) -> str | None:
    """None if the source supports the value, otherwise the problem."""
    claims, _ = _kb()
    kind, _, rest = source.partition(":")
    if kind in LABELS:
        return None if len(rest.strip()) >= 10 else f"etichetta {kind} senza motivazione"
    if kind == "claim":
        c = claims.get(rest)
        if c is None:
            return f"claim inesistente: {rest}"
        nums = _numbers_in(c.get("claim"))
        return None if any(abs(abs(value) - abs(n)) <= 1e-9 for n in nums) else f"valore non presente nel claim {rest}"
    if kind == "within":
        m = re.fullmatch(r"claim:([a-z0-9_.]+):([0-9.]+)-([0-9.]+)", rest)
        if not m:
            return f"formato non valido: {source} (atteso within:claim:<id>:<min>-<max>)"
        c = claims.get(m.group(1))
        if c is None:
            return f"claim inesistente: {m.group(1)}"
        lo, hi = float(m.group(2)), float(m.group(3))
        nums = _numbers_in(c.get("claim"))
        if not all(any(abs(x - n) <= 1e-9 for n in nums) for x in (lo, hi)):
            return f"intervallo {lo:g}–{hi:g} non presente nel claim {m.group(1)}"
        return None if lo <= abs(value) <= hi else f"valore fuori dall'intervallo {lo:g}–{hi:g} del claim {m.group(1)}"
    if kind == "param":
        prm = all_params().get(rest)
        if prm is None:
            return f"parametro inesistente: {rest}"
        return None if any(abs(value - n) <= 1e-9 for n in _numbers_in(prm.get("value"))) else (
            f"valore diverso dal parametro {rest}")
    if kind == "rule":
        m = re.fullmatch(r"([a-z0-9_]+)@(\d+)", rest)
        rule = _rules().get((m.group(1), int(m.group(2)))) if m else None
        if rule is None:
            return f"regola inesistente: {rest}"
        nums = _numbers_in(rule)
        ok = any(abs(abs(value) - abs(n)) <= 1e-9 for n in nums)
        return None if ok else f"valore non presente nella regola {rest}"
    if kind == "record":
        row = conn.execute("SELECT payload FROM raw_record WHERE id = ? OR id LIKE ?", (rest, f"%{rest}")).fetchone() \
            if conn is not None and rest else None
        if row is None:
            return f"record inesistente: {rest}"
        nums = _numbers_in(json.loads(row["payload"]))
        return None if any(abs(value - n) <= 1e-9 for n in nums) else f"valore non presente nel record {rest}"
    return f"tipo di fonte sconosciuto: {source}"


def check_prereg(prereg: dict, conn: sqlite3.Connection | None = None) -> Result:
    """Intervention proposal: cited claims must exist; every number in the plan, expected outcome, adherence
    thresholds and derivation arguments needs a source in `value_sources` (longest path prefix wins) that supports
    it; free-text fields follow the same rules as reports."""
    claims, _ = _kb()
    res = Result()
    for cid in prereg.get("evidence_claims") or []:
        if cid not in claims:
            res.issues.append(Issue(f"evidence_claims: {cid}", f"claim inesistente: {cid}"))
    sources: dict[str, str] = prereg.get("value_sources") or {}
    for path, value in structured_numbers(prereg):
        keys = [k for k in sources if _covers(k, path)]
        if not keys:
            res.issues.append(Issue(f"{path} = {value:g}", "numero strutturato senza fonte in value_sources"))
            continue
        problem = check_source(value, sources[max(keys, key=lambda k: (k.count(".") + 1, -k.count("*")))], conn)
        if problem:
            res.issues.append(Issue(f"{path} = {value:g}", problem))
    parts = []
    for key in PREREG_TEXT:
        v = prereg.get(key)
        parts += [str(x) for x in v] if isinstance(v, list) else [str(v)] if v else []
    res.issues += validate("\n\n".join(parts), conn).issues
    return res


# ------------------------------------------------------------------ finalize
HEADER = "> ⚠ **DA VERIFICARE** — il validatore ha trovato punti non supportati. Non è un report definitivo.\n"


def pending_path(out: Path) -> Path:
    """Where a text that failed validation is written instead of the final file."""
    return out.with_name(out.name.removesuffix(".md") + ".DA-VERIFICARE.md")


def finalize(draft: Path, out: Path, conn: sqlite3.Connection | None) -> tuple[Result, Path]:
    """Validate a draft. Pass → written as the final file. Fail → the final file is NOT written: the text goes to
    `<name>.DA-VERIFICARE.md` with a header and the list of unsupported points."""
    text = draft.read_text()
    res = validate(text, conn)
    out.parent.mkdir(parents=True, exist_ok=True)
    if res.ok:
        out.write_text(text)
        return res, out
    target = pending_path(out)
    issues = "\n".join(f"> {i.render()}" for i in res.issues)
    target.write_text(f"{HEADER}>\n> Punti non supportati:\n{issues}\n\n{text}")
    return res, target
