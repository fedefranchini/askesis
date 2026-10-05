"""Parsers for fast daily logging (Italian shorthand), producing structured intents.

Gym:   "panca 80x8 r2, 80x7 r1 · rematore 60x10 r2"   (sets separated by comma + space)
       set = [w]LOADxREPS[*N] [rRIR | @RPE];  LOAD may be "bw", "bw+10"; "w" marks a warm-up set
Run:   "5.2km 31:40 fc145 fcmax178 rpe6 stop:fiato"
Day:   "p 68.4 · cibo 1850 115 [parz] [+600[/30]] · vita 74.1 74.3 · pesi: <gym> · corsa <run> · passi 8200 ·
        sonno 7h30 · fcr 55 · dolore spalla 3/10 · <alias> <value>"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from askesis.core.units import UnitError, parse_duration_s


class ParseError(ValueError):
    pass


def _num(s: str) -> float:
    return float(s.replace(",", "."))


# ------------------------------------------------------------------ gym
@dataclass
class ParsedSet:
    load_kg: float
    reps: int
    set_type: str = "working"
    load_kind: str = "external"
    rir: float | None = None
    rpe: float | None = None


@dataclass
class ParsedExercise:
    name: str
    sets: list[ParsedSet] = field(default_factory=list)


_SET = re.compile(
    r"^(?P<warm>w)?(?P<load>bw(?:[+-]\d+(?:[.,]\d+)?)?|\d+(?:[.,]\d+)?)x(?P<reps>\d+)(?:\*(?P<n>\d+))?$",
    re.I,
)
_RIR = re.compile(r"^r(\d+(?:[.,]5)?)$", re.I)
_RPE = re.compile(r"^@(\d+(?:[.,]5)?)$")
_BLOCK_SEP = re.compile(r"\s*[·;\n]\s*")


def _parse_set_tokens(tokens: list[str]) -> list[ParsedSet]:
    if not tokens:
        raise ParseError("serie mancante")
    m = _SET.match(tokens[0])
    if not m:
        raise ParseError(f"serie non riconosciuta: {tokens[0]!r} (atteso es. 80x8)")
    load = m["load"].lower()
    if load.startswith("bw"):
        extra = _num(load[2:]) if len(load) > 2 else 0.0
        load_kg, load_kind = abs(extra), ("assisted" if extra < 0 else "bodyweight_plus" if extra else "bodyweight")
    else:
        load_kg, load_kind = _num(load), "external"
    rir = rpe = None
    for t in tokens[1:]:
        if mr := _RIR.match(t):
            rir = _num(mr[1])
        elif mp := _RPE.match(t):
            rpe = _num(mp[1])
        else:
            raise ParseError(f"token non riconosciuto nella serie: {t!r}")
    one = ParsedSet(load_kg, int(m["reps"]), "warmup" if m["warm"] else "working", load_kind, rir, rpe)
    return [ParsedSet(**one.__dict__) for _ in range(int(m["n"] or 1))]


def parse_gym(text: str) -> list[ParsedExercise]:
    out: list[ParsedExercise] = []
    for block in filter(None, _BLOCK_SEP.split(text.strip())):
        # sets are separated by ", " (comma + space); "12,5" stays a decimal number
        chunks = [c.strip() for c in re.split(r",\s+", block)]
        words = chunks[0].split()
        idx = next((i for i, w in enumerate(words) if _SET.match(w)), None)
        if idx is None or idx == 0:
            raise ParseError(f"esercizio senza nome o senza serie: {block!r}")
        ex = ParsedExercise(" ".join(words[:idx]))
        ex.sets += _parse_set_tokens(words[idx:])
        for chunk in chunks[1:]:
            ex.sets += _parse_set_tokens(chunk.split())
        out.append(ex)
    if not out:
        raise ParseError("nessun esercizio")
    return out


# ------------------------------------------------------------------ run
_RUN_TYPES = {
    "facile": "easy", "easy": "easy", "lungo": "long", "long": "long", "recupero": "recovery",
    "tempo": "tempo", "soglia": "threshold", "ripetute": "intervals", "intervalli": "intervals",
    "fartlek": "fartlek", "gara": "race", "test": "test",
}


def parse_run(text: str) -> dict:
    out: dict = {}
    tokens = text.split()
    i = 0
    while i < len(tokens):
        t = tokens[i].lower()
        if m := re.fullmatch(r"(\d+(?:[.,]\d+)?)(km|m)", t):
            out["distance_m"] = _num(m[1]) * (1000 if m[2] == "km" else 1)
        elif re.fullmatch(r"\d+:\d{2}(?::\d{2})?", t):
            out["elapsed_s"] = parse_duration_s(t)
        elif m := re.fullmatch(r"fcmax(\d+)", t):
            out["max_hr"] = int(m[1])
        elif m := re.fullmatch(r"fc(\d+)", t):
            out["avg_hr"] = int(m[1])
        elif m := re.fullmatch(r"rpe(\d+(?:[.,]5)?)", t):
            out["session_rpe"] = _num(m[1])
        elif t.startswith("stop:"):
            out["stop_reason"] = " ".join([tokens[i][5:], *tokens[i + 1:]]).strip()
            break
        elif t in ("tm", "tapis"):
            out["environment"] = "treadmill"
        elif t in _RUN_TYPES:
            out["run_type"] = _RUN_TYPES[t]
        else:
            raise ParseError(f"token corsa non riconosciuto: {tokens[i]!r}")
        i += 1
    if "distance_m" not in out or "elapsed_s" not in out:
        raise ParseError("corsa: servono almeno distanza (es. 5.2km) e tempo (es. 31:40)")
    return out


# ------------------------------------------------------------------ day line
@dataclass
class Intent:
    kind: str  # weight | food | waist | gym | run | steps | sleep | rhr | pain | context
    data: dict


def _hours(text: str) -> int:
    t = text.lower().replace(" ", "")
    if m := re.fullmatch(r"(\d+(?:[.,]\d+)?)h", t):
        return int(round(_num(m[1]) * 3600))
    if m := re.fullmatch(r"(\d+)h(\d{1,2})(?:m)?", t):
        return int(m[1]) * 3600 + int(m[2]) * 60
    try:
        return parse_duration_s(t + ":00") if re.fullmatch(r"\d+:\d{2}", t) else parse_duration_s(t)
    except UnitError as exc:
        raise ParseError(f"durata sonno non valida: {text!r}") from exc


def parse_day(text: str, context_aliases: dict[str, str] | None = None) -> list[Intent]:
    aliases = {k.lower(): v for k, v in (context_aliases or {}).items()}
    intents: list[Intent] = []
    # A "pesi:" segment uses "·" between exercises: split only where a known keyword follows.
    keywords = ["p", "peso", "cibo", "vita", "pesi", "corsa", "passi", "sonno", "fcr", "dolore", *aliases]
    lookahead = "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True))
    segments = re.split(rf"\s*(?:·|;|\n)\s*(?=(?:{lookahead})\b)", text.strip(), flags=re.I)
    for seg in filter(None, (s.strip() for s in segments)):
        head, _, rest = seg.partition(" ")
        key = head.lower().rstrip(":")
        rest = rest.strip()
        if key in ("p", "peso"):
            intents.append(Intent("weight", {"value_kg": _num(rest)}))
        elif key == "cibo":
            parts = rest.split()
            partial = any(p.lower() in ("parz", "parziale") for p in parts)
            extras = [p[1:] for p in parts if p.startswith("+")]  # free meal not in the app: "+800" or "+800/40"
            nums = [p for p in parts if p.lower() not in ("parz", "parziale", "ieri", "oggi") and not p.startswith("+")]
            if not nums:
                raise ParseError("cibo: servono kcal (e proteine)")
            when = "oggi" if any(p.lower() == "oggi" for p in parts) else "ieri"
            extra_kcal = sum(_num(x.split("/")[0]) for x in extras)
            extra_prot = sum(_num(x.split("/")[1]) for x in extras if "/" in x)
            intents.append(Intent("food", {
                "energy_kcal": _num(nums[0]), "protein_g": _num(nums[1]) if len(nums) > 1 else None,
                "partial": partial, "when": when, "free_meal_kcal": extra_kcal or None,
                "free_meal_protein_g": extra_prot or None}))
        elif key == "vita":
            intents.append(Intent("waist", {"readings_cm": [_num(x) for x in rest.split()]}))
        elif key == "pesi":
            intents.append(Intent("gym", {"exercises": parse_gym(rest)}))
        elif key == "corsa":
            intents.append(Intent("run", parse_run(rest)))
        elif key == "passi":
            intents.append(Intent("steps", {"steps": int(rest.replace(".", ""))}))
        elif key == "sonno":
            intents.append(Intent("sleep", {"asleep_s": _hours(rest)}))
        elif key == "fcr":
            intents.append(Intent("rhr", {"bpm": int(rest)}))
        elif key == "dolore":
            m = re.fullmatch(r"(.+?)\s+(\d+(?:[.,]\d+)?)(?:/10)?", rest)
            if not m:
                raise ParseError("dolore: formato 'dolore <zona> <0-10>/10'")
            intents.append(Intent("pain", {"region": m[1], "score_0_10": _num(m[2])}))
        elif key in aliases:
            value = rest
            try:
                value = _num(rest)
            except ValueError:
                pass
            intents.append(Intent("context", {"key": aliases[key], "value": value}))
        else:
            raise ParseError(f"voce non riconosciuta: {seg!r}")
    if not intents:
        raise ParseError("riga vuota")
    return intents
