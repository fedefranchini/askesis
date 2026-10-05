"""Validator for generated texts: unsupported numbers and sources are intercepted (synthetic texts only)."""

from __future__ import annotations

from askesis.validation import textcheck as tc


def problems(text: str) -> list[str]:
    return [i.problem for i in tc.validate(text).issues]


def test_reintroduced_protein_range_is_blocked():
    # the historical error: a range not present in the KB, attributed to a real source
    bad = "Proteine nel range 1,6–2,4 g/kg/die [claim:protein.general_range]."
    out = problems(bad)
    assert any("1,6" in p for p in out) and any("2,4" in p for p in out)


def test_unreferenced_range_is_blocked():
    assert any("numero senza riferimento" in p for p in problems("Target 1,6–2,4 g/kg/die in deficit."))


def test_value_matching_claim_passes():
    assert problems("Range generale 1,4–2,0 g/kg/die [claim:protein.general_range].") == []


def test_unknown_ids_and_sources_are_blocked():
    out = problems("Vedi [claim:protein.invented] e Smith et al. 2019, doi 10.9999/fake.1.")
    assert any("claim inesistente" in p for p in out)
    assert any("Smith 2019" in p for p in out)
    assert any("DOI non presente" in p for p in out)


def test_known_source_passes():
    assert problems("Position stand di Thomas et al. 2016.") == []


def test_finalize_marks_draft(tmp_path):
    draft, out = tmp_path / "d.md", tmp_path / "o.md"
    draft.write_text("Target 1,6–2,4 g/kg/die.\n")
    res, target = tc.finalize(draft, out, None)
    assert not res.ok and not out.exists()  # never saved as final
    assert target.name == "o.DA-VERIFICARE.md" and target.read_text().startswith(tc.HEADER)
    draft.write_text("Range generale 1,4–2,0 g/kg/die [claim:protein.general_range].\n")
    res, target = tc.finalize(draft, out, None)
    assert res.ok and target == out and out.exists()


def test_prereg_with_unsupported_numbers_or_claims_is_rejected():
    base = {"hypothesis": "un deficit moderato riduce il peso di tendenza", "reason": "fase di dimagrimento",
            "change_description": "nuovo target", "success_criteria": "EMA in calo", "stop_criteria": ["flag T2+"],
            "evidence_claims": ["protein.general_range"]}
    assert tc.check_prereg(base).ok
    bad = {**base, "reason": "proteine a 1,6–2,4 g/kg/die [claim:protein.general_range]"}
    assert not tc.check_prereg(bad).ok
    assert not tc.check_prereg({**base, "evidence_claims": ["protein.invented"]}).ok
