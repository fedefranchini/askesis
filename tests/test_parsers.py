import pytest
from hypothesis import given
from hypothesis import strategies as st

from askesis.parsers.text import ParseError, parse_day, parse_gym, parse_run


def test_gym_basic():
    ex = parse_gym("panca 80x8 r2, 80x7 r1 · rematore 60x10 r2")
    assert [e.name for e in ex] == ["panca", "rematore"]
    assert [(s.load_kg, s.reps, s.rir) for s in ex[0].sets] == [(80, 8, 2), (80, 7, 1)]


def test_gym_multiword_name_warmup_repeat_rpe_and_bodyweight():
    ex = parse_gym("panca inclinata manubri w20x10, 28x10*3 @8; trazioni bw+10x6 r2, bwx8")
    sets = ex[0].sets
    assert ex[0].name == "panca inclinata manubri"
    assert sets[0].set_type == "warmup" and len(sets) == 4 and sets[1].rpe == 8
    assert ex[1].sets[0].load_kind == "bodyweight_plus" and ex[1].sets[0].load_kg == 10
    assert ex[1].sets[1].load_kind == "bodyweight"


def test_gym_decimal_comma():
    assert parse_gym("curl 12,5x10 r1")[0].sets[0].load_kg == 12.5


@pytest.mark.parametrize("bad", ["", "80x8", "panca", "panca 80x8 zz", "panca ottanta"])
def test_gym_rejects(bad):
    with pytest.raises(ParseError):
        parse_gym(bad)


@given(
    load=st.integers(min_value=1, max_value=300),
    reps=st.integers(min_value=1, max_value=30),
    rir=st.integers(min_value=0, max_value=5),
)
def test_gym_roundtrip(load, reps, rir):
    s = parse_gym(f"squat {load}x{reps} r{rir}")[0].sets[0]
    assert (s.load_kg, s.reps, s.rir) == (load, reps, rir)


def test_run():
    r = parse_run("5.2km 31:40 fc145 fcmax178 facile stop:fiato corto")
    assert r == {"distance_m": 5200, "elapsed_s": 1900, "avg_hr": 145, "max_hr": 178, "run_type": "easy",
                 "stop_reason": "fiato corto"}


def test_run_requires_distance_and_time():
    with pytest.raises(ParseError):
        parse_run("fc150")


def test_day_line_full():
    line = ("p 74.6 · cibo 1650 95 parz · vita 80.1 80.3 · pesi: panca 60x8 r2 · rematore 50x10 r2 · "
            "corsa 4km 25:00 fc140 · passi 9000 · sonno 7h30 · fcr 55 · dolore spalla 3/10 · xvar 4")
    kinds = [i.kind for i in parse_day(line, {"xvar": "custom_key"})]
    assert kinds == ["weight", "food", "waist", "gym", "run", "steps", "sleep", "rhr", "pain", "context"]


def test_day_gym_segment_keeps_exercises_together():
    gym = parse_day("pesi: panca 60x8 r2 · rematore 50x10 r2 · p 74")[0]
    assert [e.name for e in gym.data["exercises"]] == ["panca", "rematore"]


def test_day_food_defaults_to_yesterday_and_partial():
    food = parse_day("cibo 1800 120 parz")[0].data
    assert food["when"] == "ieri" and food["partial"] is True and food["protein_g"] == 120


def test_day_unknown_alias_rejected():
    with pytest.raises(ParseError):
        parse_day("xvar 4")


def test_day_sleep_formats():
    assert parse_day("sonno 7h30")[0].data["asleep_s"] == 27000
    assert parse_day("sonno 7,5h")[0].data["asleep_s"] == 27000
    assert parse_day("sonno 6:45")[0].data["asleep_s"] == 24300


def test_free_meal_estimate_is_added_and_kept_apart():
    (food,) = parse_day("cibo 1850 115 +600/30")
    assert food.data["energy_kcal"] == 1850 and food.data["free_meal_kcal"] == 600
    assert food.data["free_meal_protein_g"] == 30
