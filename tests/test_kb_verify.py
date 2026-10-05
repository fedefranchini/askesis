"""KB identifier check (offline: fetchers are replaced by fakes)."""

from __future__ import annotations

from askesis import kb_verify as kv

SRC = [{"id": "x2020", "citation": "Rossi A, et al. A study of synthetic training effects. J Test. 2020;1:1-2.",
        "doi": "10.0000/x", "pmid": "1"}]


def fake(title="A Study of Synthetic Training Effects", author="Rossi", years=("2020",)):
    return lambda _ident: kv.Record(title, author, set(years))


def test_matching_record_passes():
    assert kv.verify(SRC, fake(), fake(), pause=0) == ([], [])


def test_wrong_title_author_or_year_is_reported():
    problems, _ = kv.verify(SRC, fake(title="Something else entirely here", author="Bianchi", years=("2019",)),
                            fake(), pause=0)
    assert len(problems) == 1 and all(w in problems[0] for w in ("titolo", "primo autore", "anno"))


def test_network_errors_are_kept_apart():
    def boom(_ident):
        raise OSError("offline")

    problems, errors = kv.verify(SRC, boom, fake(), pause=0)
    assert problems == [] and len(errors) == 1


def test_typographic_quotes_and_accents_are_normalised():
    assert kv.mismatches("Jäger R. Updating ACSM's view. 2017.", kv.Record("Updating ACSM’s View", "Jager", {"2017"})) \
        == []
