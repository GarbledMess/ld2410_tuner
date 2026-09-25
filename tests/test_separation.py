"""Gate separation must reduce hidden false triggers without losing real presence."""

import unittest

import test_tuner as harness
from tuner_under_test.calibration.search import _ThresholdSearch, _union_masks
from tuner_under_test.calibration.separation import gate_preference

EMPTY = {"present": [], "not_present": []}


class SeparationTests(unittest.TestCase):
    def test_clear_gap_gets_a_data_derived_midpoint(self):
        rows = harness.row_samples({"g0_still": 80}, {"g0_still": 20})
        result = harness.fit(rows, ["g0_still"])
        proposal = result["proposals"]["g0_still"]
        self.assertEqual(proposal["threshold"], 50)
        self.assertEqual(proposal["noise_ceiling"], 20)
        self.assertEqual(proposal["safety_margin"], 30)
        self.assertEqual(proposal["separation"]["presence_reference"], 80)
        self.assertEqual(result["status"], "ok")

    def test_gate_noise_is_not_hidden_by_another_gates_false_trigger(self):
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
        guesses = [(30000 + i * 6, {keys[0]: 7, keys[1]: 0}, "present", 0.99) for i in range(1000)]
        result = harness.fit(rows, keys, automatic=guesses)
        self.assertEqual(result["training"]["false_positives"], 1)
        self.assertEqual(result["proposals"][keys[0]]["false_positives"], 0)
        self.assertEqual(result["proposals"][keys[0]]["threshold"], 50)

    def test_human_separation_outranks_weak_guesses_inside_the_gap(self):
        rows = harness.row_samples({"g0_still": 80}, {"g0_still": 20})
        guesses = [(300 + i, {"g0_still": 25}, "present", 0.99) for i in range(500)]
        result = harness.fit(rows, ["g0_still"], automatic=guesses)
        self.assertEqual(result["proposals"]["g0_still"]["threshold"], 50)
        self.assertTrue(result["automatic_evidence"]["used"])

    def test_actual_quiet_presence_overrides_the_preferred_margin(self):
        rows = harness.row_samples({"g0_still": 80}, {"g0_still": 20}, count=1000)
        rows[100:110] = [(i, {"g0_still": 24}, "present") for i in range(100, 110)]
        result = harness.fit(rows, ["g0_still"])
        self.assertEqual(result["proposals"]["g0_still"]["separation"]["preferred_threshold"], 50)
        self.assertLess(result["proposals"]["g0_still"]["threshold"], 24)
        self.assertEqual(result["training"]["false_negatives"], 0)
        self.assertEqual(result["outlier_filter"]["human"]["excluded"]["present"], 0)

    def test_no_fixed_floor_on_a_legitimate_low_energy_gate(self):
        result = harness.fit(harness.row_samples({"g0_still": 5}, {"g0_still": 1}), ["g0_still"])
        self.assertEqual(result["proposals"]["g0_still"]["threshold"], 3)
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
        context = _ThresholdSearch(positives, negatives, EMPTY, keys, None)
        seed = dict(zip(keys, (9, 17, 17), strict=True))
        selected = {key: context.tables[key][value] for key, value in seed.items()}
        score = context.rank(_union_masks(selected), 500, context._distance(seed))
        repaired = context.repair_pair(seed, score)
        # Either support gate can replace the noisy detector. Gate 2 detects the
        # quiet location at 8, retaining more separation than Gate 1 at 7.
        self.assertEqual(repaired, dict(zip(keys, (25, 17, 8), strict=True)))

    def test_single_human_class_does_not_gain_extra_sensitivity_penalties(self):
        negatives = [(i, {"g0_still": 20}, "not_present") for i in range(100)]
        automatic = {
            "present": [(200 + i, {"g0_still": 30}, "present", 0.9) for i in range(100)],
            "not_present": [],
        }
        context = _ThresholdSearch([], negatives, automatic, ["g0_still"], None)
        bits = context.tables["g0_still"][22]
        self.assertEqual(context.preferred["g0_still"], 22)
        self.assertEqual(context.rank(bits, gate_false=0), context.rank(bits, gate_false=20))
        self.assertFalse(context._needs_refinement((0, 0, 0, 0, 0, 0, 4)))
