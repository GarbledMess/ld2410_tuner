"""Room learning's evidence reads give what reading each row and cell one at a time gives.

``reference_readings`` and ``reference_human`` are the per-row and per-cell reads; the
learner's reads must equal them exactly, in content and order.
"""

import random
from math import ceil

import test_tuner  # noqa: F401
from tuner_under_test.calibration import event_quality, separation
from tuner_under_test.calibration.episodes import Episode
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.rooms import evidence

KEYS = ("g0_move", "g2_still", "g5_move")


def reference_readings(rows, key):
    return sorted((row[1][key], row[4] if len(row) > 4 else 1) for row in rows if key in row[1])


def reference_human(cells, keys):
    episodes, masks, densities = {}, {}, {}
    empty = {key: [0.0] * 101 for key in keys}
    positions = tuple(HISTORY_KEYS.index(key) for key in keys)
    for low, high, values, _state, parent in cells:
        if parent is None or not any(values[p] is not None for p in positions):
            continue
        episodes[parent] = None
        for key, position in zip(keys, positions, strict=True):
            value = values[position]
            if value is None:
                continue
            energy = ceil(value)
            if parent.state == "present":
                masks[parent, key] = masks.get((parent, key), 0) | (1 << energy)
            else:
                empty[key][energy] += high - low
            densities.setdefault((parent, key), [0.0] * 101)[energy] += high - low
    presence = {key: tuple(m for (_ep, gate), m in masks.items() if gate == key) for key in keys}
    empty = {key: [s / 6 for s in histogram] for key, histogram in empty.items()}
    return tuple(episodes), presence, empty, densities


def test_gate_readings_are_each_rows_reading_of_the_gate():
    rnd = random.Random(2)
    layout = {key: position for position, key in enumerate(HISTORY_KEYS)}
    for _ in range(20):
        rows = []
        for i in range(300):
            values = bytes(rnd.choice((255, 0, 7, 40, 100)) for _ in HISTORY_KEYS)
            row = (float(i), evidence.Readings(layout, values), "present", 0.5)
            rows.append((*row, rnd.choice((1, 0.5, 2.25))) if rnd.random() < 0.8 else row[:3])
        for key in ("g0_move", "g8_still"):
            assert separation._readings(rows, key) == reference_readings(rows, key)
        assert separation._readings([], "g0_move") == []
        mixed = [(0.0, {"g0_move": 3}, "present"), *rows[:5]]  # Readings of another kind.
        assert separation._readings(mixed, "g0_move") == reference_readings(mixed, "g0_move")


def test_human_evidence_is_each_cells_reading():
    rnd = random.Random(4)
    for _ in range(30):
        parents = [
            Episode(i * 100.0, i * 100.0 + 90, rnd.choice(("present", "not_present")), "a")
            for i in range(5)
        ]
        cells, t = [], 0.0
        for _ in range(400):
            parent = rnd.choice([*parents, None, parents[0]])
            if parent is not None and rnd.random() < 0.2:  # Equal, but another object.
                parent = Episode(parent.start, parent.end, parent.state, parent.owner)
            values = tuple(rnd.choice((None, 0, 3.5, 40, 99)) for _ in HISTORY_KEYS)
            cells.append((t, t + rnd.choice((1, 6)), values, "x", parent))
            t += 6
        # The learner passes its gates as a mapping (gate -> threshold), not a sequence.
        for keys in (KEYS, dict.fromkeys(KEYS, 50)):
            found = event_quality._human(cells, keys)
            expected = reference_human(cells, keys)
            assert found[0] == expected[0]
            assert found[1] == expected[1]
            assert found[2] == expected[2]
            assert list(found[3].items()) == list(expected[3].items())
