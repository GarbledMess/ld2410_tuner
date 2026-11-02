"""Synthetic exactness and bounded-work guards for native quality accumulation."""

import random
from math import ceil
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.calibration.constants import SAMPLE_SECONDS
from tuner_under_test.calibration.episodes import Episode
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.engine.hybrid import quality


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
    with patch.object(quality, "HISTORY_KEYS", positions):
        result = quality._Densities(keys)
        for cell in cells:
            result.add(cell)
        result.finish()
    assert tuple(result.episodes) == (parent,)
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
        quantized = tuple(None if value is None else ceil(value) for value in values)
        cells.append((low, high, quantized, None if parent is None else parent.state, parent))
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
    actual = quality._Densities(keys)
    for cell in cells:
        actual.add(cell)
    actual.finish()
    assert tuple(actual.episodes) == tuple(expected_episodes)
    assert {
        key: [v / SAMPLE_SECONDS for v in hist]
        for key, hist in zip(keys, actual.empty, strict=True)
    } == expected_empty
    for key in keys:
        masks = expected_presence[key]
        assert actual.masks()[key] == tuple(
            sum(
                1 << energy
                for energy in range(101)
                if sum(bool(mask & (1 << energy)) for mask in masks) >= count
            )
            for count in (1, 2)
        )
        for state in ("present", "not_present"):
            histograms = [
                hist
                for (ep, gate), hist in expected_density.items()
                if gate == key and ep.state == state
            ]
            expected = [
                sum(hist[i] / sum(hist) for hist in histograms) / len(histograms)
                for i in range(101)
            ]
            assert actual.densities(state)[key][0] == pytest.approx(expected)


def test_objective_preference_switches_reuse_only_their_own_histograms(tmp_path):
    from dataclasses import replace

    from test_hybrid_evidence import truth
    from test_hybrid_search import problem
    from tuner_under_test.engine.hybrid.search import collect_training, coordinate_costs
    from tuner_under_test.engine.hybrid.training import TrainingStore
    from tuner_under_test.engine.hybrid.types import EvidencePolicy

    spans = [
        truth(0, 6),
        truth(6, 12, source="automatic", episode="auto"),
        truth(12, 24, "not_present", episode="empty"),
    ]
    p = problem({"a": [(80,), (20,), (30,), (30,)]}, spans=spans)
    p = replace(p, policy=EvidencePolicy(1, False, {}))
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        original = coordinate_costs(p, store, {"a": {0: 90}}, "a", 0, (10, 40, 90))
        reference = replace(p, policy=replace(p.policy, reference=True))
        suppressed = coordinate_costs(reference, store, {"a": {0: 90}}, "a", 0, (10, 40, 90))
        assert original != suppressed
        assert coordinate_costs(p, store, {"a": {0: 90}}, "a", 0, (10, 40, 90)) == original
        assert suppressed[40] == 0
        assert original[40] > 0
