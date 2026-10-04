from datetime import date, timedelta

import pytest
import synthetic as syn
import yaml
from test_f3 import programme
from typer.testing import CliRunner

from askesis.cli.main import app
from askesis.config import load
from askesis.ingestion import gymnote
from askesis.plan import store
from askesis.store.db import connect

DAY = syn.START  # Monday
SESSION = {"name": "A", "run": {"kind": "easy", "duration_min": 30, "distance_km": None, "intensity": "talk_test"},
           "lifts": [{"exercise": "bench_press", "sets": 3, "load_kg": 52.5, "rep_range": [6, 8], "target_rir": 2},
                     {"exercise": "leg_press", "sets": 2, "load_kg": None, "rep_range": [10, 12], "target_rir": 2}]}


def note(results: dict[str, str]) -> list[str]:
    lines = gymnote.render(DAY, SESSION, {"bench_press": "(24/2): 50 kg × 8 @RIR 2"}, {"leg_press": "schienale 3"})
    out, current = [], None
    for line in lines:
        m = gymnote.MARKER.search(line)
        if m:
            current = m.group(1)
        if line.startswith("→") and current in results:
            line = "→ " + results[current]
        out.append(line)
    return gymnote.html_to_lines(gymnote.to_html(out))  # through the HTML round trip, as with Apple Notes


def test_render_contains_plan_and_privacy_safe_content():
    lines = gymnote.render(DAY, SESSION, {}, {"leg_press": "schienale 3"})
    text = "\n".join(lines)
    assert lines[0] == "Askesis · 2025-03-03 · A"
    assert "Panca piana [bench_press]" in text and "3 × 6–8 · RIR 2 · consigliato 52,5 kg" in text
    assert "carico da calibrare" in text and "macchina: schienale 3" in text and "Corsa easy [run]" in text


@pytest.mark.parametrize("written,expected", [
    ("52,5x8 r2, 52,5x7 r1, 52,5x6 r1", [(52.5, 8, 2), (52.5, 7, 1), (52.5, 6, 1)]),
    ("52.5 × 8 @RIR 2; 52.5kg x 7 R1", [(52.5, 8, 2), (52.5, 7, 1)]),
    ("50X8 rir2,50x8 r2", [(50, 8, 2), (50, 8, 2)]),
    ("50x8*3 r2", [(50, 8, 2)] * 3),
])
def test_tolerant_parsing(written, expected):
    res = gymnote.parse_note(note({"bench_press": written}))
    assert [(s.load_kg, s.reps, s.rir) for s in res.exercises[0].sets] == expected
    assert not res.errors


def test_unreadable_line_is_reported_not_guessed():
    res = gymnote.parse_note(note({"bench_press": "ottanta per otto", "leg_press": "100x12 r2"}))
    assert [e.name for e in res.exercises] == ["leg_press"]
    assert res.errors and res.errors[0][0] == "bench_press"


def test_empty_results_are_skipped_and_run_and_notes_read():
    lines = note({"run": "4km 24:00 fc150"}) + ["Note → stanco ma ok"]
    res = gymnote.parse_note(lines)
    assert set(res.skipped) == {"bench_press", "leg_press"}
    assert res.run_text == "4km 24:00 fc150" and res.notes == "stanco ma ok"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "g.db"))
    conn = connect(load().db_path)
    store.add_version(conn, "programme", "base", programme(), DAY, None)
    conn.commit()
    conn.close()
    fake = {"body": None}
    import askesis.notes_bridge as nb

    monkeypatch.setattr(nb, "upsert", lambda title, body: fake.update(body=body) or "note-id")
    monkeypatch.setattr(nb, "read", lambda title: fake["body"])
    runner = CliRunner()
    return (lambda *a: runner.invoke(app, list(a), catch_exceptions=False)), fake


def write_results(fake, results):
    lines = gymnote.html_to_lines(fake["body"])
    out, current = [], None
    for line in lines:
        m = gymnote.MARKER.search(line)
        if m:
            current = m.group(1)
        if line.startswith("→") and current in results:
            line = "→ " + results[current]
        out.append(line)
    fake["body"] = gymnote.to_html([x for x in out if x])


def test_cli_create_import_idempotent_and_corrections(cli):
    run, fake = cli
    created = run("gym-note", "create", "--for", DAY.isoformat()).output
    assert "Panca piana [bench_press]" in created and "carico da calibrare" in created
    write_results(fake, {"bench_press": "50x8 r2, 50x8 r2, 50x7 r1"})
    preview = run("gym-note", "import", "--for", DAY.isoformat()).output
    assert "ANTEPRIMA" in preview and "Panca piana: 50 kg × 8 @RIR 2 · 50 kg × 8 @RIR 2 · 50 kg × 7 @RIR 1" in preview
    saved = run("gym-note", "import", "--for", DAY.isoformat(), "--yes").output
    assert "inseriti 4" in saved  # session + 3 sets
    again = run("gym-note", "import", "--for", DAY.isoformat(), "--yes").output
    assert "nulla di nuovo" in again and "invariati: 4" in again
    write_results(fake, {"bench_press": "50x8 r2, 50x8 r2, 50x8 r1"})  # third set corrected after import
    fixed = run("gym-note", "import", "--for", DAY.isoformat(), "--yes").output
    assert "inseriti 1" in fixed
    conn = connect(load().db_path)
    reps = [r["reps"] for r in conn.execute("SELECT reps FROM v_set_record ORDER BY sequence")]
    assert reps == [8, 8, 8]  # the corrected set superseded the old one; history kept
    assert conn.execute("SELECT COUNT(*) FROM raw_record WHERE entity_type='set_record'").fetchone()[0] == 4


def test_double_progression_flows_into_next_note(cli):
    run, fake = cli
    for k in (0, 7):
        d = (DAY + timedelta(days=k)).isoformat()
        run("gym-note", "create", "--for", d)
        write_results(fake, {"bench_press": "50x8 r2, 50x8 r2, 50x8 r3"})
        run("gym-note", "import", "--for", d, "--yes")
    nxt = run("gym-note", "create", "--for", (DAY + timedelta(days=14)).isoformat(), "--print").output
    assert "consigliato 52,5 kg" in nxt and "ultima (10/3): 50 kg × 8 @RIR 2" in nxt


def test_programme_fixture_is_generic():
    assert yaml.safe_dump(programme())  # synthetic plan only
    assert date(2025, 3, 3).weekday() == 0
