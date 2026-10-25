"""Attribution: did this radar see the occupied episode, compared with its own empty baseline."""

import test_tuner  # noqa: F401  (registers tuner_under_test)
from sample_rows import attribute, baseline_of, cells_of, episodes_of, found_of
from tuner_under_test.calibration import constants
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.rooms import attribution

QUIET = [i % 10 for i in range(100)]  # 99th percentile 9, so "above" means > 9 + margin.


def rows(values, start=0, step=6, key="g0_move"):
    return [(start + i * step, {key: value}, "unknown") for i, value in enumerate(values)]


def unlabelled(_timestamp):
    return None


def episodes(truth_at, times):
    """Each episode's first and last reading time: runs of recorded readings (each held until
    the next, at most a sample) under present truth."""
    cells = cells_of([(t, {"g0_move": 1}, "unknown") for t in times])
    return [(run[0][0], run[-1][0]) for run in episodes_of(cells, truth_at)]


def decide(values, base_values=QUIET, label_at=unlabelled):
    base = baseline_of(rows(base_values, start=10_000), lambda _t: True)
    episode_rows = rows(values)
    episode = (episode_rows[0][0], episode_rows[-1][0])
    return attribute(episode_rows, episode, base, label_at)


def test_constants_are_the_calibrated_values():
    assert constants.ATTRIBUTION_TAIL_QUANTILE == 0.99
    assert constants.ATTRIBUTION_MARGIN == 3
    assert constants.ATTRIBUTION_SUSTAIN_SECONDS == 30
    assert constants.ATTRIBUTION_FULL_CONFIDENCE_SAMPLES == 100


def test_quiet_member_not_attributed():
    result = decide([5, 9, 12, 7] * 10)
    assert result == {"state": "no", "confidence": 1.0}


def test_brief_spike_below_sustain_not_attributed():
    assert decide([3] * 10 + [60] * 4 + [3] * 10)["state"] == "no"  # 24 s < 30 s.
    # Runs are broken by recording gaps, so two separated bursts are not one sustained run.
    gap = rows([60] * 4, start=0) + rows([60] * 4, start=100)
    base = baseline_of(rows(QUIET, start=10_000), lambda _t: True)
    assert attribute(gap, (0, 118), base, unlabelled)["state"] == "no"


def test_sustained_rise_attributed():
    assert decide([3] * 10 + [60] * 5 + [3] * 10) == {"state": "yes", "confidence": 1.0}
    assert decide([13] * 5)["state"] == "yes"  # Five 6 s samples cover the 30 s needed.
    assert decide([12] * 30)["state"] == "no"  # Exactly quantile + margin is not above it.


def test_any_channel_attributes_and_confidence_scales_with_its_baseline():
    base = baseline_of(
        [(i * 6, {"a": i % 10, "b": 0}, "unknown") for i in range(50)]
        + [(i * 6, {"b": 0}, "unknown") for i in range(50, 80)],
        lambda t: t < 1000,
    )
    episode_rows = [(2000 + i * 6, {"a": 0, "b": 40}, "unknown") for i in range(5)]
    result = attribute(episode_rows, (2000, 2024), base, unlabelled)
    assert result == {"state": "yes", "confidence": 0.8}
    quiet_rows = [(2000 + i * 6, {"a": 0, "b": 0}, "unknown") for i in range(5)]
    assert attribute(quiet_rows, (2000, 2024), base, unlabelled)["confidence"] == 0.8


def test_human_radar_label_decides():
    present = decide([3] * 20, label_at=lambda _t: "present")
    assert present == {"state": "yes", "confidence": 1.0}
    empty = decide([60] * 20, label_at=lambda _t: "not_present")
    assert empty == {"state": "no", "confidence": 1.0}
    unknown = decide([60] * 20, label_at=lambda _t: "unknown")
    assert unknown["state"] == "yes"
    partly = decide([3] * 20, label_at=lambda t: "present" if t >= 60 else "not_present")
    assert partly["state"] == "yes"


def test_small_baseline_is_unknown():
    assert decide([60] * 20, base_values=QUIET[:19]) == {"state": "unknown", "confidence": 0.0}
    assert decide([60] * 20, base_values=QUIET[:20]) == {"state": "yes", "confidence": 0.2}
    assert decide([60] * 20, base_values=[]) == {"state": "unknown", "confidence": 0.0}


def test_baseline_uses_quiet_samples_only_and_keeps_each_gate_apart():
    a, b = HISTORY_KEYS.index("g0_move"), HISTORY_KEYS.index("g0_still")
    data = [(i * 6, {"g0_move": 5, "g0_still": 1}, "unknown") for i in range(30)]
    data += [(180 + i * 6, {"g0_move": 2}, "unknown") for i in range(30)]
    data += [(360 + i * 6, {"g0_move": 90}, "x") for i in range(30)]
    base = baseline_of(data, lambda t: t < 360)
    assert set(base) == {a, b}
    assert len(base[a]) == 60 and len(base[b]) == 30  # Quiet time only, in samples.
    assert base[a].ceiling() == 5 + constants.ATTRIBUTION_MARGIN  # Never the loud 90.
    assert base[b].ceiling() == 1 + constants.ATTRIBUTION_MARGIN
    assert baseline_of([], lambda _t: True) == {}


def test_episodes_split_at_recording_gaps_and_empty_truth():
    def truth_at(t):
        return ("present", "human", 1.0) if t < 100 or t >= 200 else ("not_present", "human", 1.0)

    times = [0, 6, 12, 60, 66, 120, 200, 206]
    assert episodes(truth_at, times) == [(0, 12), (60, 66), (200, 206)]
    assert episodes(lambda _t: None, times) == []
    assert episodes(truth_at, []) == []


def states(group_truths, found):
    """The target state of radar "r" from precomputed attributions (``found``)."""
    target = attribution.weighted_member_target("r", unlabelled, group_truths, found=found)
    return lambda t: (target(t) or (None,))[0]


def test_shared_radar_target_is_union_of_groups():
    present, empty = ("present", "human", 1.0), ("not_present", "human", 1.0)
    office = {0: present, 10: empty, 20: empty, 30: None, 40: present}
    desk = {0: empty, 10: present, 20: empty, 30: empty, 40: present}
    yes, no = {"state": "yes", "confidence": 1.0}, {"state": "no", "confidence": 1.0}
    target = states(
        {"office": office.get, "desk": desk.get},
        {"office": {"r": [((0, 5), yes), ((40, 45), no)]}, "desk": {"r": [((10, 15), no)]}},
    )
    assert target(0) == "present"  # Office occupied and r saw it; desk empty.
    assert target(10) is None  # Desk occupied, r did not see it, so no quiet target either.
    assert target(20) == "not_present"
    assert target(30) is None  # Office truth unknown.
    assert target(40) is None  # Both occupied, attributed in neither.
    assert states({}, {})(0) is None
    between = states({"g": lambda _t: present}, {"g": {"r": [((10, 20), yes), ((40, 50), yes)]}})
    assert [between(t) for t in (5, 10, 20, 30, 45, 55)] == [
        None,
        "present",
        "present",
        None,
        "present",
        None,
    ]


def test_deterministic_repeat():
    values = [3] * 5 + [40] * 6 + [3] * 5

    def run():
        base = baseline_of(rows(QUIET, start=10_000), lambda _t: True)
        data = rows(values)
        spans = episodes(lambda _t: ("present", "human", 1.0), [r[0] for r in data])
        return [attribute(data, span, base, unlabelled) for span in spans]

    assert run() == run() == [{"state": "yes", "confidence": 1.0}]


def inferred(quiet, automatic=None, label_at=unlabelled):
    """Baseline: ``quiet`` samples while the room is human-Empty; then a sustained rise under
    human room Present from t=600. ``automatic`` is the room's automatic evidence reader."""
    present, empty = ("present", "human", 1.0), ("not_present", "human", 1.0)
    data = rows(QUIET[:quiet]) + rows([60] * 10, start=600)
    room = {"g": lambda t: empty if t < 600 else present}
    extra = {"g": automatic} if automatic else None
    found = found_of("r", data, label_at, room)
    return attribution.weighted_member_target("r", label_at, room, extra, found=found)


def test_rule_two_under_human_room_present_is_an_automatic_guess():
    target = inferred(60)
    assert target(0) == ("not_present", "human", 1.0)  # Human room Empty stays human.
    # Rule 2 is an inference: an automatic guess carrying the attribution confidence.
    assert target(600) == ("present", "attributed", 0.6)
    # Rule 1 (the radar's own human Present label in the episode) stays human.
    assert inferred(60, label_at=lambda t: "present" if t == 600 else None)(654) == (
        "present",
        "human",
        1.0,
    )
    # Automatic room truth keeps its own confidence and source, whichever rule attributed it.
    data = rows(QUIET[:60]) + rows([60] * 10, start=600)
    empty = ("not_present", "human", 1.0)
    estimate = {"g": lambda t: empty if t < 600 else ("present", "estimate", 0.7)}
    found = found_of("r", data, unlabelled, estimate)
    guessed = attribution.weighted_member_target("r", unlabelled, estimate, found=found)
    assert guessed(600) == ("present", "estimate", 0.7)


def test_rule_two_guess_is_floored_like_the_room_estimate():
    # 54 quiet samples: confidence 0.54 < MIN_AUTO_CONFIDENCE (0.55); 55 reach it.
    assert inferred(54)(600) is None
    assert inferred(55)(600) == ("present", "attributed", 0.55)


def test_rule_two_guess_and_automatic_room_evidence_take_the_more_confident():
    assert inferred(60, lambda t: ("present", "estimate", 0.9))(600) == ("present", "estimate", 0.9)
    assert inferred(60, lambda t: ("present", "automatic", 0.8))(600) == (
        "present",
        "automatic",
        0.8,
    )
    assert inferred(60, lambda t: ("present", "estimate", 0.55))(600) == (
        "present",
        "attributed",
        0.6,
    )
    # Evidence of Empty never contradicts human room Present; a floored guess leaves the
    # automatic evidence to stand alone.
    assert inferred(60, lambda t: ("not_present", "estimate", 0.9))(600) == (
        "present",
        "attributed",
        0.6,
    )
    assert inferred(40, lambda t: ("present", "estimate", 0.7))(600) == ("present", "estimate", 0.7)
    assert inferred(40, lambda t: None)(600) is None
    # Human evidence still wins outright.
    labelled = inferred(60, lambda t: ("present", "estimate", 0.9), lambda t: "present")
    assert labelled(600) == ("present", "human", 1.0)
    assert inferred(60, lambda t: ("present", "estimate", 0.9))(0) == ("not_present", "human", 1.0)


def test_stronger_room_guess():
    attributed, estimate = ("present", "attributed", 0.6), ("present", "estimate", 0.94)
    assert attribution.stronger(attributed, estimate) == estimate
    assert attribution.stronger(estimate, attributed) == estimate
    assert attribution.stronger(attributed, ("present", "estimate", 0.6)) == attributed
    assert attribution.stronger(attributed, ("not_present", "estimate", 0.9)) == attributed
    assert attribution.stronger(attributed, ("present", "human", 1.0)) == attributed
    assert attribution.stronger(None, estimate) == estimate
    assert attribution.stronger(attributed, None) == attributed
    assert attribution.stronger(None, ("present", "human", 1.0)) is None
