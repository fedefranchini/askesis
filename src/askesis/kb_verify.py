"""Periodic check of the evidence KB identifiers against Crossref and PubMed (run in CI on a schedule).

For each source: the DOI must resolve on Crossref and the PMID on PubMed, and the record's title, first author and
year must match the citation stored in the KB. Network failures are reported separately from mismatches, so a
temporary outage is not confused with a wrong citation.

Usage: python -m askesis.kb_verify [sources.yaml ...]      (exit 1 on any mismatch, 2 on network errors only)
"""

from __future__ import annotations

import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from askesis.config import ROOT

USER_AGENT = "askesis-kb-verify/1.0 (+https://github.com/fedefranchini/askesis)"
TITLE_OVERLAP_MIN = 0.9


@dataclass
class Record:
    title: str
    first_author: str
    years: set[str]


def _norm(s: str) -> str:
    s = re.sub(r"[\W_]+", " ", s)  # punctuation (incl. typographic quotes) → space, before dropping accents
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (fixed https endpoints)
        return json.load(r)


def crossref(doi: str) -> Record:
    m = _get_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}")["message"]
    years = {str(m[k]["date-parts"][0][0]) for k in ("issued", "published-print", "published-online", "created")
             if m.get(k) and m[k].get("date-parts") and m[k]["date-parts"][0][0]}
    authors = m.get("author") or [{}]
    return Record((m.get("title") or [""])[0], authors[0].get("family") or authors[0].get("name", ""), years)


def pubmed(pmid: str) -> Record:
    r = _get_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&retmode=json&id=" + pmid)
    x = r["result"][pmid]
    years = {y for y in re.findall(r"(?:19|20)\d{2}", " ".join([x.get("pubdate", ""), x.get("epubdate", "")]))}
    first = (x.get("authors") or [{"name": ""}])[0]["name"].split(" ")[0]
    return Record(x.get("title", ""), first, years)


def mismatches(citation: str, rec: Record) -> list[str]:
    cit = _norm(citation)
    words = [w for w in _norm(rec.title).split() if len(w) > 2]
    out = []
    if words and sum(w in cit.split() for w in words) / len(words) < TITLE_OVERLAP_MIN:
        out.append("titolo")
    if rec.first_author and _norm(rec.first_author).split()[0] not in cit:
        out.append("primo autore")
    if rec.years and not any(y in citation for y in rec.years):
        out.append("anno")
    return out


def verify(sources: list[dict], fetch_doi: Callable[[str], Record] = crossref,
           fetch_pmid: Callable[[str], Record] = pubmed, pause: float = 0.4) -> tuple[list[str], list[str]]:
    """(problems, network errors)."""
    problems, errors = [], []
    for s in sources:
        for kind, ident, fetch in (("DOI", s.get("doi"), fetch_doi), ("PMID", s.get("pmid"), fetch_pmid)):
            if not ident:
                continue
            try:
                rec = fetch(str(ident))
            except Exception as exc:  # noqa: BLE001 - reported, never hidden
                errors.append(f"{s['id']}: {kind} {ident} non raggiungibile ({type(exc).__name__})")
                continue
            finally:
                time.sleep(pause)
            bad = mismatches(s["citation"], rec)
            if bad:
                problems.append(f"{s['id']}: {kind} {ident} non corrisponde ({', '.join(bad)}): «{rec.title}»")
    return problems, errors


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] or [ROOT / "knowledge/evidence/sources.yaml"]
    sources = [s for f in files for s in (yaml.safe_load(f.read_text()).get("sources") or [])]
    problems, errors = verify(sources)
    for line in problems + errors:
        print(f"✗ {line}")
    print(f"{len(sources)} fonti · {len(problems)} non corrispondenti · {len(errors)} errori di rete")
    return 1 if problems else 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
