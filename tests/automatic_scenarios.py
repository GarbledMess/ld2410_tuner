"""Scenarios where human labels and confident automatic evidence disagree (P2b).

Kept free of P2b-only APIs so the same builders captured the frozen pre-P2b parity fixtures
(``fixtures/pre_p2b/automatic_parity.json``) from the pre-change tree.
"""

import sys

import test_tuner as harness
from test_rooms import member


def device_conflict(slow, automatic_label="not_present"):
    """Human present at 30 except ``slow`` rows at 25, human empty at 5; 100 confident (0.9)
    automatic rows at 28 (90% of the human mass) labelled ``automatic_label``."""
    present = [(i, {"g0_move": 25 if i < slow else 30}, "present") for i in range(100)]
    empty = [(100 + i, {"g0_move": 5}, "not_present") for i in range(100)]
    guesses = [(-300 + i, {"g0_move": 28}, automatic_label, 0.9) for i in range(100)]
    return present + empty, ["g0_move"], guesses


def device_mixed():
    """Two gates; automatic presence seen only by a second gate (P1 learning test)."""
    keys = ["g0_move", "g1_still"]
    rows = harness.row_samples({keys[0]: 30, keys[1]: 5}, dict.fromkeys(keys, 5))
    guesses = [(-100 + i, {keys[0]: 5, keys[1]: 6}, "present", 0.6) for i in range(100)]
    return rows, keys, guesses


DEVICE = {
    "conflict_4": lambda: device_conflict(4),
    "conflict_10": lambda: device_conflict(10),
    "mixed": device_mixed,
}


def room_conflict(slow):
    """Present 0-600 s at 70 except ``slow`` samples at 25; human empty 600-900 s at 10;
    unlabelled 900-1200 s at 30 that the radar confidently guessed empty."""
    values = [25 if i < slow else 70 for i in range(100)] + [10] * 50 + [30] * 50
    item = member(
        "a",
        values,
        [
            {"start": 0, "end": 600, "state": "present"},
            {"start": 600, "end": 900, "state": "not_present"},
        ],
    )
    records = [
        (i * 6, [value] + [255] * 17 + ([2, 90] if i >= 150 else [0, 0]))
        for i, value in enumerate(values)
    ]
    harness.set_records(item["view"], "a", records)
    return {"a": item}


def device_rank(rows, keys, guesses, thresholds, *weight):
    """The device search's rank of ``thresholds`` on the learner's prepared evidence."""
    reliability = sys.modules["tuner_under_test.calibration.reliability"]
    search = sys.modules["tuner_under_test.calibration.search"]
    labels = ("present", "not_present")
    human = {label: [row for row in rows if row[2] == label] for label in labels}
    automatic = {label: [row for row in guesses if row[2] == label] for label in labels}
    human, automatic, _, _ = reliability.prepare_evidence(human, automatic, keys)
    context = search._ThresholdSearch(
        human["present"], human["not_present"], automatic, keys, None, None, *weight
    )
    selected = {key: context.tables[key][thresholds[key]] for key in keys}
    return context.rank(
        tuple(search._union_masks(selected)),
        context._gate_false(selected),
        context._distance(thresholds),
    )
