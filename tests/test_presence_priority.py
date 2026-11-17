"""A room's presence priority trades missed presence against false presence in Learn."""

import pytest
import test_tuner  # noqa: F401
from test_hybrid_facade import KEY, episodes, member
from tuner_under_test.engine.hybrid import adapters, facade, selection
from tuner_under_test.engine.hybrid.constants import MISSED_WEIGHT
from tuner_under_test.engine.hybrid.scoring import cost_from_statistics
from tuner_under_test.engine.hybrid.types import EvidencePolicy

# Quiet presence overlaps noisy empty time: no threshold separates them, so where the fit
# lands depends on what a missed second costs against a false one.
OVERLAP = [
    ("present", 70),
    ("not_present", 10),
    ("present", 28),
    ("not_present", 34),
    ("present", 70),
    ("not_present", 22),
    ("present", 40),
    ("not_present", 46),
    ("present", 52),
    ("not_present", 10),
]


def room(group_id, labels, priority=None):
    group = {} if priority is None else {"presence_priority": priority}
    return {
        "group_id": group_id,
        "groups": {group_id: group},
        "room_labels": {group_id: [{**label, "source": "manual"} for label in labels]},
    }


def one_dip():
    """One six-second dip in presence, and empty periods 40% noisy just above it."""
    values, labels = [], []

    def add(state, energies):
        start = len(values) * 6
        values.extend(energies)
        labels.append({"start": start, "end": len(values) * 6, "state": state})

    for i in range(4):
        add("present", [70] * 19 + [30 if i == 0 else 70])
        add("not_present", [35] * 8 + [10] * 12 if i < 3 else [10] * 20)
    return values, labels


def learned(tmp_path, values, labels, priority, threshold=90):
    folder = tmp_path / str(priority)
    folder.mkdir()
    item = member(folder, "r", values, threshold=threshold)
    report = facade.room_fit({"r": item}, 0, len(values) * 6, room("zone", labels, priority))
    return report["thresholds"]["r"][KEY], report["after"]["room"]


def test_a_presence_room_accepts_false_time_to_catch_quiet_presence(tmp_path):
    values, labels = episodes(*OVERLAP)
    default, measured = learned(tmp_path, values, labels, None)
    assert learned(tmp_path, values, labels, 1.0)[0] == default
    high, caught = learned(tmp_path, values, labels, 10.0)
    assert high < default
    assert caught["presence_recall"] > measured["presence_recall"]
    assert caught["false_positive_percent"] > measured["false_positive_percent"]
    assert measured["missed_time_cost"] == 5
    assert caught["missed_time_cost"] == 50


def test_an_empty_room_gives_up_a_dip_rather_than_false_time(tmp_path):
    values, labels = one_dip()
    default, measured = learned(tmp_path, values, labels, None, threshold=50)
    low, quiet = learned(tmp_path, values, labels, 0.2, threshold=50)
    assert low > default
    assert measured["false_positive_percent"] > 0 == quiet["false_positive_percent"]
    assert quiet["presence_recall"] < measured["presence_recall"] == 1


def test_default_priority_keeps_the_cost_unchanged():
    statistics = {"present/human": [100, 10, 4, 1], "not_present/human": [200, 20, 3, 1]}
    default = cost_from_statistics(statistics, EvidencePolicy(0, False, {}))
    same = EvidencePolicy(0, False, {}, missed_weights={"z": MISSED_WEIGHT}).for_zone("z")
    assert cost_from_statistics(statistics, same) == default
    doubled = EvidencePolicy(0, False, {}, missed_weights={"z": 2 * MISSED_WEIGHT})
    heavier = cost_from_statistics(statistics, doubled.for_zone("z"))
    assert heavier["missed_weight"] == 2 * MISSED_WEIGHT
    # Missed time is 10% and episodes 25%, false time 10% and episodes 33%.
    assert heavier["cost"] - default["cost"] == pytest.approx(100 * 5 * (0.1 + 0.25))


def test_selection_terms_scale_with_priority():
    statistics = {"present/human": [100, 10, 4, 1], "not_present/human": [200, 20, 3, 1]}
    base = cost_from_statistics(statistics, EvidencePolicy(0, False, {}))
    policy = EvidencePolicy(0, False, {}, missed_weight=3 * MISSED_WEIGHT)
    tripled = cost_from_statistics(statistics, policy)
    assert selection.priority(base) == 1
    assert selection.priority(tripled) == 3
    assert selection.quiet_cost(tripled) == pytest.approx(3 * selection.quiet_cost(base))
    tripled["sufficient_statistics"] = base["sufficient_statistics"] = statistics
    assert selection._human_recall(base) == pytest.approx(0.9)
    assert selection._human_recall(tripled) == pytest.approx(0.7)


def test_room_priority_is_validated():
    assert adapters.presence_priority(None) == 1
    assert adapters.presence_priority(room("z", [], 4)) == 4
    for bad in (0, 50, float("nan"), "high"):
        with pytest.raises(ValueError):
            adapters.presence_priority(room("z", [], bad))
    assert adapters.missed_weights({"a": {}, "b": {}}, {"a": room("a", [], 2)}) == {
        "a": 2 * MISSED_WEIGHT
    }
