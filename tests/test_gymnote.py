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


def test_rest_between_sets_from_evidence_parameters():
    from askesis.analytics.params import all_params
    from askesis.plan.rest import rest_label, rest_range

    lines = gymnote.render(DAY, SESSION, {}, {})
    bench = lines.index("Panca piana [bench_press]")
    assert lines[bench + 2] == "recupero 2–3 min"  # multi-joint
    assert rest_label("lateral_raise") == "recupero 1,5–2 min"  # single-joint
    assert rest_range("esercizio sconosciuto") == rest_range("bench_press")  # unknown: the longer range
    for key in ("rest_compound_range_s", "rest_isolation_range_s"):
        assert all_params()[key]["basis"] == "evidence" and all_params()[key]["claims"]
    # the rest line is not a result line: the parser ignores it
    res = gymnote.parse_note(note({"bench_press": "52,5x8 r2"}))
    assert [e.name for e in res.exercises] == ["bench_press"] and not res.errors


@pytest.mark.parametrize("written,expected", [
    ("52,5x8 r2, 52,5x7 r1, 52,5x6 r1", [(52.5, 8, 2), (52.5, 7, 1), (52.5, 6, 1)]),
    ("52.5 × 8 @RIR 2; 52.5kg x 7 R1", [(52.5, 8, 2), (52.5, 7, 1)]),
    ("50X8 rir2,50x8 r2", [(50, 8, 2), (50, 8, 2)]),
    ("50x8*3 r2", [(50, 8, 2)] * 3),
    ("65x8/7 rir0,0", [(65, 8, 0), (65, 7, 0)]),  # reps only: same load as the set before; one RIR per set
    ("30x8/7rir0,0", [(30, 8, 0), (30, 7, 0)]),
    ("26x3/22x7 r0", [(26, 3, None), (22, 7, 0)]),
    ("20x9/", [(20, 9, None)]),
    ("26x8 rir0, 28x6rir0", [(26, 8, 0), (28, 6, 0)]),
    ("30x10/8 rir 1/1", [(30, 10, 1), (30, 8, 1)]),
    ("10x9 rir 2, 10x9 rir1, 10x7rir0", [(10, 9, 2), (10, 9, 1), (10, 7, 0)]),
    ("12,5x10/8", [(12.5, 10, None), (12.5, 8, None)]),
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


def test_import_writes_a_validated_session_comment(cli):
    from askesis.config import reports_dir

    run, fake = cli
    for week, results in enumerate(["50x8 r2, 50x8 r2", "52,5x8 r2, 52,5x7 r1"]):
        day = DAY + timedelta(weeks=week)
        run("gym-note", "create", "--for", day.isoformat())
        write_results(fake, {"bench_press": results})
        out = run("gym-note", "import", "--for", day.isoformat(), "--yes").output
        assert "validata e salvata" in out, out
    text = (reports_dir() / f"seduta-{day.isoformat()}.md").read_text()
    assert "carico più alto di sempre, 52,5 kg (prima 50 kg)" in text
    again = run("session-comment", "--for", day.isoformat())
    assert again.exit_code == 0 and "validata e salvata" in again.output
    assert run("session-comment", "--for", (day + timedelta(days=1)).isoformat()).exit_code == 1


def test_bodyweight_reps_and_mismatched_rir_group():
    res = gymnote.parse_note(note({"bench_press": "60x8/7 rir0,0,0"}))  # three RIR for two sets: not guessed
    assert not res.exercises and res.errors
    from askesis.ingestion.gymnote import normalize_sets
    assert normalize_sets("5/5/4", bodyweight=True) == "bwx5, bwx5, bwx4"
    assert normalize_sets("5/5/4") == "5, 5, 4"  # an external load is never assumed


def test_words_around_the_sets_become_a_note_not_an_error():
    res = gymnote.parse_note(note({"bench_press": "fatto dopo le croci 50x11/8", "leg_press": "100x10 piedi alti"}))
    assert not res.errors
    assert [(s.load_kg, s.reps) for s in res.exercises[0].sets] == [(50, 11), (50, 8)]
    assert "fatto dopo le croci" in res.notes and "piedi alti" in res.notes
    res = gymnote.parse_note(note({"bench_press": "50x8 poi 50x7"}))  # words between sets: reported, not guessed
    assert res.errors and not res.exercises
