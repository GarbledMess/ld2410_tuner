"""Synthetic exactness and bounded-work guards for native quality accumulation."""

import random
from math import ceil
from unittest.mock import patch

import test_tuner  # noqa: F401
from tuner_under_test.calibration import event_quality
from tuner_under_test.calibration.constants import SAMPLE_SECONDS
from tuner_under_test.calibration.episodes import Episode
from tuner_under_test.const import HISTORY_KEYS


class IndexedKeys(list):
    def __init__(self, values):
        super().__init__(values)
        self.lookups = 0

    def index(self, value):
        self.lookups += 1
        return super().index(value)


def test_gate_positions_are_resolved_once_per_collection():
    keys = [HISTORY_KEYS[3], HISTORY_KEYS[0], HISTORY_KEYS[17]]
    parent = Episode(0, 100, "present", ("synthetic",))
    cells = [(i, i + 1, (20,) * 18, "present", parent) for i in range(100)]
    positions = IndexedKeys(HISTORY_KEYS)
    with patch.object(event_quality, "HISTORY_KEYS", positions):
        result = event_quality._human(cells, keys)
    assert result[0] == (parent,)
    assert positions.lookups <= len(keys)


def test_accumulation_matches_independent_seeded_duration_oracle():
    rng = random.Random(93671)
    keys = [HISTORY_KEYS[17], HISTORY_KEYS[0], HISTORY_KEYS[6]]
    parents = [
        Episode(i * 10, (i + 1) * 10, state, ("synthetic", str(i)))
        for i, state in enumerate(("present", "not_present", "present", "not_present"))
    ]
    cells = []
    expected_episodes, expected_masks, expected_empty, expected_density = {}, {}, {}, {}
    for key in keys:
        expected_empty[key] = [0.0] * 101
    for i in range(400):
        parent = parents[i // 100] if i % 13 else None
        values = tuple(
            None if rng.randrange(4) == 0 else rng.randrange(10001) / 100 for _ in HISTORY_KEYS
        )
        low = i / 10
        high = low + rng.randrange(1, 20) / 100
        cells.append((low, high, values, None if parent is None else parent.state, parent))
        if parent is None:
            continue
        observed = [
            (key, values[HISTORY_KEYS.index(key)])
            for key in keys
            if values[HISTORY_KEYS.index(key)] is not None
        ]
        if not observed:
            continue
        expected_episodes[parent] = None
        for key, value in observed:
            energy = ceil(value)
            token = (parent, key)
            if parent.state == "present":
                expected_masks[token] = expected_masks.get(token, 0) | (1 << energy)
            else:
                expected_empty[key][energy] += high - low
            if token not in expected_density:
                expected_density[token] = [0.0] * 101
            expected_density[token][energy] += high - low
    expected_presence = {
        key: tuple(mask for (ep, gate), mask in expected_masks.items() if gate == key)
        for key in keys
    }
    expected_empty = {
        key: [v / SAMPLE_SECONDS for v in hist] for key, hist in expected_empty.items()
    }
    assert event_quality._human(cells, keys) == (
        tuple(expected_episodes),
        expected_presence,
        expected_empty,
        expected_density,
    )


def test_objective_preference_switches_reuse_only_their_own_histograms():
    from types import SimpleNamespace

    from tuner_under_test.calibration import event_fitting, event_preferences

    class CountedCells(list):
        def __init__(self, cells):
            super().__init__(cells)
            self.walks = 0

        def __iter__(self):
            self.walks += 1
            return super().__iter__()

    rng = random.Random(82395)
    keys = [HISTORY_KEYS[0], HISTORY_KEYS[7]]
    cells = [
        (
            i / 10,
            i / 10 + rng.randrange(1, 20) / 100,
            {key: rng.randrange(101) for key in keys if rng.randrange(5)},
            "present" if i % 3 else "not_present",
            "automatic" if i % 2 else "human",
            0.7,
        )
        for i in range(300)
    ]
    model = SimpleNamespace(supports=lambda key, value: value > 35)
    for source_cells in (cells, [(0, 10, {keys[0]: 2}, "not_present", "human", 1)]):
        objective = event_fitting._DeviceObjective.__new__(event_fitting._DeviceObjective)
        objective.device_id = "synthetic"
        objective.current = {"synthetic": dict.fromkeys(keys, 50)}
        objective.quality = {"synthetic": model}
        objective.regions = CountedCells(source_cells)
        objective._preference_histograms = None
        objective.totals = {("human", state): 1 for state in ("present", "not_present")}
        for reference in (False, True, False):
            objective.reference = reference
            suppressed = ("present", "not_present") if reference else ()
            expected = event_preferences.preferences(
                source_cells, objective.current["synthetic"], quality=model, reference=suppressed
            )
            actual = objective._member_preferences({}, None)
            assert actual == {"synthetic": expected}
        assert objective.regions.walks <= len(keys)
