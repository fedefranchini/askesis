"""Coach texts and pending decisions (synthetic data): validated texts only, explicit "approvo"/"rifiuto"."""

from __future__ import annotations

import os
import time
from datetime import timedelta

import pytest
import synthetic as syn
from test_calorie_rule import DAY21, TARGET, world
from test_cli_f3 import SYNTHETIC_SOURCES
from test_f3 import prereg

from askesis import coach
from askesis.ingestion.pipeline import ingest
from askesis.interventions import registry as reg
from askesis.plan import calorie_rule as cr
from askesis.plan import store
from askesis.safety import rules as safety
from askesis.store.db import connect


def test_intent_needs_one_explicit_word():
    assert coach.intent("Approvo!") == "approve" and coach.intent("rifiuto, troppo presto") == "reject"
    for bad in ("ok", "va bene", "approvo ma rifiuto", ""):
        with pytest.raises(coach.DecisionError):
            coach.intent(bad)


@pytest.fixture
def proposed(tmp_path):
    conn = connect(tmp_path / "p.db")
    ingest(conn, syn.athlete() + syn.linear_weights(21) + syn.constant_intake(21), "s")
    reg.propose(conn, "Fase", "phase_start_fat_loss", prereg(value_sources=SYNTHETIC_SOURCES))
    return conn


def test_a_proposal_that_fails_validation_cannot_be_approved_but_can_be_rejected(tmp_path):
    conn = connect(tmp_path / "u.db")
    ingest(conn, syn.athlete() + syn.linear_weights(21) + syn.constant_intake(21), "s")
    reg.propose(conn, "Fase", "phase_start_fat_loss", prereg())  # numbers without declared sources
    (item,) = coach.pending(conn)
    assert not item.validated and item.issues
    with pytest.raises(coach.DecisionError, match="da verificare"):
        coach.decide(conn, "intervention", "1", "approvo")
    coach.decide(conn, "intervention", "1", "rifiuto")
    assert reg.status(conn, reg.get(conn, 1)["id"]) == "rejected"


def test_intervention_proposal_is_pending_then_approved_through_the_registry(proposed):
    items = coach.pending(proposed)
    assert [(p.kind, p.ref, p.validated) for p in items] == [("intervention", "1", True)]
    with pytest.raises(coach.DecisionError):
        coach.decide(proposed, "intervention", "1", "va bene")
    msg = coach.decide(proposed, "intervention", "1", "approvo", "mi convince")
    assert "approvato" in msg and reg.status(proposed, reg.get(proposed, 1)["id"]) == "activated"
    row = proposed.execute("SELECT athlete_response_verbatim, reasoning FROM decision").fetchone()
    assert (row[0], row[1]) == ("approvo", "mi convince")
    assert coach.pending(proposed) == []
    with pytest.raises(coach.DecisionError, match="non più in attesa"):
        coach.decide(proposed, "intervention", "1", "approvo")


def test_intervention_rejection_is_recorded(proposed):
    coach.decide(proposed, "intervention", "1", "rifiuto", "non ora")
    assert reg.status(proposed, reg.get(proposed, 1)["id"]) == "rejected"
    assert proposed.execute("SELECT status FROM decision").fetchone()[0] == "rejected"


def test_safety_gate_still_applies(proposed, monkeypatch):
    def blocked(*a, **k):
        raise safety.SafetyBlock("flag T3 aperto")

    monkeypatch.setattr(reg.safety, "gate", blocked)
    with pytest.raises(coach.DecisionError, match="safety"):
        coach.decide(proposed, "intervention", "1", "approvo")
    assert reg.status(proposed, reg.get(proposed, 1)["id"]) == "proposed"


def test_calorie_proposal_applies_from_tomorrow(tmp_path):
    conn = world(tmp_path, -0.02)
    cr.evaluate(conn, DAY21)
    (item,) = coach.pending(conn)
    assert item.kind == "calorie" and "→" in item.summary
    coach.decide(conn, "calorie", item.ref, "approvo", today=DAY21)
    nxt = DAY21 + timedelta(days=1)
    assert store.content(store.active(conn, "nutrition_target", nxt))["energy_kcal"] == TARGET - 150
    assert store.content(store.active(conn, "nutrition_target", DAY21))["energy_kcal"] == TARGET
    assert coach.pending(conn) == []


def test_calorie_rejection_keeps_the_target(tmp_path):
    conn = world(tmp_path, -0.02)
    cr.evaluate(conn, DAY21)
    (item,) = coach.pending(conn)
    coach.decide(conn, "calorie", item.ref, "rifiuto", "preferisco aspettare", today=DAY21)
    assert coach.pending(conn) == []
    assert store.content(store.active(conn, "nutrition_target", DAY21 + timedelta(days=1)))["energy_kcal"] == TARGET


def test_calorie_change_is_refused_with_an_open_safety_flag(tmp_path, monkeypatch):
    conn = world(tmp_path, -0.02)
    cr.evaluate(conn, DAY21)
    (item,) = coach.pending(conn)
    monkeypatch.setattr(coach.safety, "open_flags", lambda c: [object()])
    with pytest.raises(coach.DecisionError, match="safety"):
        coach.decide(conn, "calorie", item.ref, "approvo", today=DAY21)


def test_a_newer_evaluation_supersedes_an_old_proposal(tmp_path):
    conn = world(tmp_path, -0.02)
    cr.evaluate(conn, DAY21)
    cr.evaluate(conn, DAY21 + timedelta(days=1))  # too soon after a proposal: no new proposal
    assert coach.pending(conn) == []


def test_only_validated_texts_count_as_validated(tmp_path):
    rep = tmp_path / "reports"
    rep.mkdir()
    (rep / "daily-2025-03-10.md").write_text("# Controllo\n\nNessuna segnalazione.\n")
    (rep / "review-2025-W11-commento.md").write_text("# Commento\n\nIl peso è sceso di 4,7 kg.\n")
    old = rep / "retro-2025-02.DA-VERIFICARE.md"
    old.write_text("> ⚠ **DA VERIFICARE**\n>\n> Punti\n\n# Retro\n\nTesto 12,3.\n")
    past = time.time() - 3600
    os.utime(old, (past, past))
    (rep / "retro-2025-02.md").write_text("# Retro\n\nNessuna conclusione.\n")  # newer final version
    (rep / "review-2025-W12.DA-VERIFICARE.md").write_text("> ⚠ **DA VERIFICARE**\n\n# Review\n\nTesto.\n")
    (rep / "note-varie.md").write_text("non è un testo del coach")
    docs = {d.name: d for d in coach.documents(rep, None)}
    assert docs["daily-2025-03-10.md"].validated
    assert not docs["review-2025-W11-commento.md"].validated and docs["review-2025-W11-commento.md"].issues
    assert "retro-2025-02.DA-VERIFICARE.md" not in docs and docs["retro-2025-02.md"].validated
    flagged = docs["review-2025-W12.DA-VERIFICARE.md"]
    assert not flagged.validated and flagged.issues and "DA VERIFICARE" not in flagged.body
    assert "note-varie.md" not in docs
