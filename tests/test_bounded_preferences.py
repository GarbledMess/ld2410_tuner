"""Duration histograms remain exact when training is spilled and fragmented."""

import random
from collections import Counter

import test_tuner  # noqa: F401
from tuner_under_test.engine.hybrid.candidates import channel_statistics
from tuner_under_test.engine.hybrid.training import TrainingStore
from tuner_under_test.engine.hybrid.types import TruthSpan


def observe(store, cells):
    for low, high, value, state, source, confidence in cells:
        span = TruthSpan(low, high, state, source, confidence, f"{state}/{source}", ())
        store.observe("zone", "radar", low, high, span, [value] + [255] * 17)


def test_seeded_scaled_preferences_match_unit_repetition_and_ignore_confidence(tmp_path):
    rng = random.Random(61931)
    cells = [
        (
            i * 3,
            i * 3 + rng.randrange(1, 4),
            rng.randrange(101),
            "present" if i % 2 else "not_present",
            "human",
            rng.random(),
        )
        for i in range(160)
    ]
    expected = {state: Counter() for state in ("present", "not_present")}
    for low, high, value, state, _, _ in cells:
        expected[state].update([value] * (high - low))
    with TrainingStore(tmp_path, budget=0) as store:
        observe(store, cells)
        bins = store.histograms("zone", "radar", 0)
        assert bins == {
            state: [counter[v] for v in range(101)] for state, counter in expected.items()
        }
        stats = channel_statistics(bins["present"], bins["not_present"])
        assert stats["ceiling"] == max(expected["not_present"])
        values = sorted(expected["present"].elements())
        assert stats["q25"] == values[(len(values) - 1) // 4]


def test_zero_mass_halo_cannot_raise_noise_ceiling_or_change_weighted_quantiles(tmp_path):
    cells = [
        (0, 100, 10, "not_present", "human", 1),
        (100, 101, 90, "not_present", "human", 1),
        (101, 102, 30, "present", "human", 1),
        (102, 112, 80, "present", "human", 1),
        (112, 112, 100, "not_present", "human", 1),
    ]
    with TrainingStore(tmp_path, budget=0) as store:
        observe(store, cells)
        bins = store.histograms("zone", "radar", 0)
        found = channel_statistics(bins["present"], bins["not_present"])
        assert found["ceiling"] == 90
        assert found["q25"] == 80
        assert bins["not_present"][100] == 0


def test_prepared_masses_are_fixed_and_do_not_leak_to_other_calls(tmp_path):
    with TrainingStore(tmp_path) as store:
        observe(
            store, [(0, 2, 10, "not_present", "human", 1), (2, 4, 30, "present", "human", 0.01)]
        )
        before = store.histograms("zone", "radar", 0)
        observe(store, [(4, 6, 99, "not_present", "human", 1)])
        after = store.histograms("zone", "radar", 0)
        assert before["not_present"][99] == 0
        assert after["not_present"][99] == 2
        assert before["present"] == after["present"]


def test_default_histogram_path_preserves_original_elapsed_mass_exactly(tmp_path):
    cells = [
        (0.1, 0.3, 12, "not_present", "human", 0.1),
        (0.3, 0.7, 12, "not_present", "human", 0.9),
        (0.7, 1.1, 40, "present", "automatic", 0.2),
    ]
    with TrainingStore(tmp_path) as store:
        observe(store, cells)
        bins = store.histograms("zone", "radar", 0)
        assert bins["not_present"][12] == (0.3 - 0.1) + (0.7 - 0.3)
        assert bins["present"][40] == 1.1 - 0.7
