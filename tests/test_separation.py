"""Gate separation must reduce hidden false triggers without losing real presence."""

import unittest

import test_tuner as harness
from tuner_under_test.calibration.separation import gate_preference

EMPTY = {"present": [], "not_present": []}


class SeparationTests(unittest.TestCase):
    def test_clear_gap_gets_a_data_derived_midpoint(self):
        rows = harness.row_samples({"g0_still": 80}, {"g0_still": 20})
        result = harness.fit(rows, ["g0_still"])
        proposal = result["proposals"]["g0_still"]
        self.assertGreater(proposal["threshold"], 20)
        self.assertLess(proposal["threshold"], 80)
        self.assertEqual(proposal["noise_ceiling"], 20)
        self.assertEqual(proposal["safety_margin"], proposal["threshold"] - 20)
        self.assertEqual(proposal["separation"]["presence_reference"], 80)
        self.assertEqual(result["status"], "ok")

    def test_gate_noise_is_not_hidden_by_another_gates_false_trigger(self):
        keys = ["g0_still", "g1_still"]
        rows = [(i * 6, {keys[0]: 80, keys[1]: 80}, "present") for i in range(1000)]
        rows += [
            (
                10000 + i * 6,
                {keys[0]: 21 if i == 100 else 3, keys[1]: 100 if i == 100 else 3},
                "not_present",
            )
            for i in range(1000)
        ]
        guesses = [(30000 + i * 6, {keys[0]: 7, keys[1]: 0}, "present", 0.99) for i in range(1000)]
        result = harness.fit(rows, keys, automatic=guesses)
        self.assertEqual(result["training"]["false_negatives"], 0)
        self.assertEqual(result["training"]["false_positives"], 0)
        self.assertEqual(result["proposals"][keys[0]]["false_positives"], 0)
        self.assertGreaterEqual(result["proposals"][keys[0]]["threshold"], 21)
        self.assertLess(result["proposals"][keys[0]]["threshold"], 80)

    def test_a_single_false_trigger_is_not_paid_for_with_five_percent_of_presence(self):
        keys = ["g0_still", "g1_still"]
        rows = [
            (i * 6, {keys[0]: 0 if i < 50 else 80, keys[1]: 80}, "present") for i in range(1000)
        ]
        rows += [
            (
                10000 + i * 6,
                {keys[0]: 21 if i == 100 else 3, keys[1]: 100 if i == 100 else 3},
                "not_present",
            )
            for i in range(1000)
        ]
        result = harness.fit(rows, keys)
        self.assertEqual(result["training"]["false_negatives"], 0)

    def test_automatic_share_can_spend_margin_without_sacrificing_human_labels(self):
        rows = harness.row_samples({"g0_still": 80}, {"g0_still": 20})
        guesses = [(300 + i, {"g0_still": 25}, "present", 0.99) for i in range(500)]
        manual = harness.fit(rows, ["g0_still"], automatic=guesses, automatic_weight=0)
        result = harness.fit(rows, ["g0_still"], automatic=guesses, automatic_weight=0.2)
        self.assertGreater(manual["proposals"]["g0_still"]["threshold"], 20)
        self.assertLess(manual["proposals"]["g0_still"]["threshold"], 80)
        self.assertGreater(result["proposals"]["g0_still"]["threshold"], 20)
        self.assertLess(result["proposals"]["g0_still"]["threshold"], 25)
        self.assertTrue(result["automatic_evidence"]["used"])
        self.assertEqual(result["training"]["false_negatives"], 0)
        self.assertEqual(result["training"]["false_positives"], 0)

    def test_actual_quiet_presence_overrides_the_preferred_margin(self):
        rows = harness.row_samples({"g0_still": 80}, {"g0_still": 20}, count=1000)
        # A 30% quiet stretch exceeds the two-episode 10.5-point evidence band.
        rows[100:400] = [(i, {"g0_still": 24}, "present") for i in range(100, 400)]
        result = harness.fit(rows, ["g0_still"])
        self.assertGreater(result["proposals"]["g0_still"]["separation"]["preferred_threshold"], 20)
        self.assertLess(result["proposals"]["g0_still"]["separation"]["preferred_threshold"], 24)
        self.assertLess(result["proposals"]["g0_still"]["threshold"], 24)
        self.assertEqual(result["training"]["false_negatives"], 0)
        self.assertEqual(result["outlier_filter"]["excluded"], False)

    def test_no_fixed_floor_on_a_legitimate_low_energy_gate(self):
        result = harness.fit(harness.row_samples({"g0_still": 5}, {"g0_still": 1}), ["g0_still"])
        self.assertGreater(result["proposals"]["g0_still"]["threshold"], 1)
        self.assertLess(result["proposals"]["g0_still"]["threshold"], 5)
        self.assertEqual(result["status"], "ok")

    def test_refinement_continues_after_targets_pass(self):
        keys = ["g0_still", "g1_still"]
        rows = [(i * 6, dict.fromkeys(keys, 30), "present") for i in range(1000)]
        rows += [
            (10000 + i * 6, dict.fromkeys(keys, 20 if i < 4 else 5), "not_present")
            for i in range(1000)
        ]
        result = harness.fit(rows, keys)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["training"]["false_positives"], 0)
        self.assertTrue(all(p["false_positives"] == 0 for p in result["proposals"].values()))

    def test_unobserved_gate_and_single_class_use_explicit_fallback(self):
        self.assertEqual(gate_preference("g0_still", EMPTY, EMPTY, 42)["preferred_threshold"], 42)
        groups = {"present": [], "not_present": [(0, {"g0_still": 10}, "not_present")]}
        preference = gate_preference("g0_still", groups, EMPTY)
        self.assertEqual(preference["preferred_threshold"], 12)
        self.assertFalse(preference["separated"])

    def test_automatic_only_separation_remains_estimated(self):
        groups = {
            "present": [(0, {"g0_still": 80}, "present", 0.8)],
            "not_present": [(6, {"g0_still": 20}, "not_present", 0.8)],
        }
        preference = gate_preference("g0_still", EMPTY, groups)
        self.assertEqual(preference["preferred_threshold"], 22)
        self.assertFalse(preference["human_supported"])

    def test_pair_repair_selects_support_with_the_better_remaining_margin(self):
        keys = ["g0_still", "g1_still", "g2_still"]
        positives = [
            (
                i * 6,
                dict(zip(keys, (30, 30, 30) if i < 400 else (10, 8, 9), strict=True)),
                "present",
            )
            for i in range(500)
        ]
        negatives = [
            (10000 + i * 6, dict(zip(keys, (20, 5, 5), strict=True)), "not_present")
            for i in range(500)
        ]
        result = harness.fit(positives + negatives, keys)
        thresholds = {key: value["threshold"] for key, value in result["proposals"].items()}
        self.assertEqual(result["training"]["false_positives"], 0)
        self.assertEqual(result["training"]["false_negatives"], 0)
        self.assertLess(thresholds[keys[2]], 9)
        self.assertGreater(thresholds[keys[1]] - 5, thresholds[keys[2]] - 5)
        constrained = harness.fit(
            positives + negatives, keys, constraints={keys[2]: {"min": 10, "max": 100, "step": 1}}
        )
        self.assertGreaterEqual(constrained["proposals"][keys[2]]["threshold"], 10)
        self.assertLess(constrained["proposals"][keys[1]]["threshold"], 8)
        self.assertEqual(constrained["training"]["false_positives"], 0)
        self.assertEqual(constrained["training"]["false_negatives"], 0)

    def test_single_human_class_keeps_automatic_presence_as_primary(self):
        negatives = [(i, {"g0_still": 20}, "not_present") for i in range(100)]
        automatic = [(200 + i, {"g0_still": 30}, "present", 0.9) for i in range(100)]
        result = harness.fit(negatives, ["g0_still"], automatic=automatic, automatic_weight=0)
        self.assertEqual(result["training"]["false_positives"], 0)
        self.assertEqual(result["estimated_training"]["false_negatives"], 0)
        self.assertTrue(result["automatic_evidence"]["used"])


def test_room_preference_construction_has_bounded_row_overhead(tmp_path):
    import tracemalloc

    from tuner_under_test.engine.hybrid.candidates import channel_statistics, preferred
    from tuner_under_test.engine.hybrid.training import TrainingStore
    from tuner_under_test.engine.hybrid.types import TruthSpan

    count = 30000
    tracemalloc.start()
    try:
        with TrainingStore(tmp_path, budget=0) as training:
            for i in range(count):
                state, energy = ("present", 80) if i % 2 == 0 else ("not_present", 20)
                span = TruthSpan(i, i + 1, state, "human", 1, state, ())
                training.observe("zone", "r", i, i + 1, span, bytes([energy] + [255] * 17))
            bins = training.histograms("zone", "r", 0)
            statistics = training.statistics("zone", "r")
            view = channel_statistics(bins["present"], bins["not_present"])
            assert preferred(tuple(range(101)), 42, view) == 50
            assert statistics["present"]["episodes"] == 1
            assert sum(bins["present"]) == count / 2
            assert sum(bins["not_present"]) == count / 2
            _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 5_000_000


def test_compact_preferences_preserve_sorted_fractional_weights_and_support(tmp_path):
    import pytest
    from tuner_under_test.engine.hybrid.training import TrainingStore
    from tuner_under_test.engine.hybrid.types import TruthSpan

    expected = {state: [0.0] * 101 for state in EMPTY}
    weighted = dict.fromkeys(EMPTY, 0.0)
    seconds = dict.fromkeys(EMPTY, 0.0)
    with TrainingStore(tmp_path, budget=0) as training:
        for i, energy in enumerate([0, 20, 80, 255, 80, 20, 90] * 20):
            if energy == 255:
                continue
            state = "present" if i % 2 else "not_present"
            width = (0.1, 0.125, 0.3)[i % 3]
            confidence = (0.6, 0.7, 0.9)[i % 3]
            span = TruthSpan(i, i + width, state, "automatic", confidence, state, ())
            training.observe("zone", "r", i, i + width, span, bytes([energy] + [255] * 17))
            actual_width = i + width - i
            expected[state][energy] += actual_width
            seconds[state] += actual_width
            weighted[state] += actual_width * confidence
        assert training.histograms("zone", "r", 0) == expected
        statistics = training.statistics("zone", "r")
    for state in EMPTY:
        assert statistics[state]["seconds"] == pytest.approx(seconds[state])
        assert statistics[state]["weighted_seconds"] == pytest.approx(weighted[state])
        assert statistics[state]["episodes"] == 1
        assert statistics[state]["episode_mass"] == pytest.approx(weighted[state] / seconds[state])
