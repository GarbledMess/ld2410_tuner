"""Prove conflicts conservatively; never treat an inconclusive bound as feasibility."""

import itertools
import random
import unittest

import test_tuner as harness
from tuner_under_test.calibration.feasibility import assess_feasibility, exclusive_presence
from tuner_under_test.calibration.fitting import _human_failures, metrics


class FeasibilityTests(unittest.TestCase):
    def test_weak_presence_proves_required_false_samples_exceed_budget(self):
        positives = [
            (i * 6, {"g4_still": 0 if i == 10 else 5 if i in (100, 101, 102) else 30}, "present")
            for i in range(2000)
        ]
        negatives = [
            (20000 + i * 6, {"g4_still": 5 if i < 62 else 2}, "not_present") for i in range(5000)
        ]
        result = assess_feasibility(positives, negatives, ["g4_still"])
        self.assertEqual(result["status"], "conflict")
        window = result["windows"][0]
        self.assertEqual(window["allowed_missed_samples"], 2)
        self.assertEqual(window["allowed_false_samples"], 25)
        self.assertEqual(window["minimum_false_samples_for_recall"], 62)
        self.assertEqual(window["unavoidable_missed_samples"], 4)
        self.assertEqual(window["unavoidable_longest_missed_run"], 3)
        self.assertEqual([period["samples"] for period in window["periods"]], [1, 3])
        self.assertFalse(result["windows"][1]["conflict"])

    def test_nonconflicting_bound_is_not_a_guarantee(self):
        positives = [(i, {"g0_move": 20}, "present") for i in range(60)]
        negatives = [(100 + i, {"g0_move": 10}, "not_present") for i in range(60)]
        result = assess_feasibility(positives, negatives, ["g0_move"])
        self.assertEqual(result["status"], "not_ruled_out")
        self.assertEqual(result["windows"][0]["minimum_false_samples_for_recall"], 0)
        self.assertEqual(result["windows"][0]["periods"], [])

    def test_automatic_only_and_single_class_data_are_not_claimed_impossible(self):
        for present, absent in [([], []), ([(0, {"g0_move": 0}, "present")], [])]:
            result = assess_feasibility(present, absent, ["g0_move"])
            self.assertEqual(result, {"status": "not_assessed", "windows": []})

    def test_zero_energy_and_missing_gates_have_no_detecting_threshold(self):
        positives = [(0, {"g0_move": 0}, "present")]
        negatives = [(20, {"g0_move": 0}, "not_present")]
        window = assess_feasibility(positives, negatives, ["g0_move", "g1_still"])["windows"][0]
        self.assertTrue(window["conflict"])
        self.assertIsNone(window["minimum_false_samples_for_recall"])
        self.assertEqual(window["unavoidable_missed_episodes"], 1)

    def test_bounds_never_reject_an_exhaustively_feasible_configuration(self):
        rng = random.Random(81)
        keys = ["g0_move", "g1_still"]
        for _ in range(40):
            rows = [
                (
                    i * 6,
                    {key: rng.randrange(5) for key in keys},
                    "present" if i < 8 else "not_present",
                )
                for i in range(16)
            ]
            result = assess_feasibility(rows[:8], rows[8:], keys)
            lower_bound = result["windows"][0]["minimum_false_samples_for_recall"]
            for values in itertools.product(range(5), repeat=2):
                measured = metrics(rows, dict(zip(keys, values, strict=True)))
                if measured["false_negatives"] == 0:
                    self.assertIsNotNone(lower_bound)
                    self.assertGreaterEqual(measured["false_positives"], lower_bound)
                failures = _human_failures(measured)
                if not failures:
                    self.assertEqual(result["status"], "not_ruled_out")

    def test_dependency_counts_use_the_combined_recommendation(self):
        rows = [
            (0, {"g0_move": 20, "g1_still": 20}, "present"),
            (6, {"g0_move": 5, "g1_still": 8}, "present"),
            (12, {"g0_move": 4, "g1_still": 5}, "present"),
            (18, {"g0_move": 0, "g1_still": 0}, "present"),
        ]
        result = exclusive_presence(rows, {"g0_move": 10, "g1_still": 4})
        self.assertEqual(result["g0_move"]["exclusive_presence_samples"], 0)
        self.assertIsNone(result["g0_move"]["weakest_exclusive_presence_energy"])
        self.assertEqual(result["g1_still"]["exclusive_presence_samples"], 2)
        self.assertEqual(result["g1_still"]["weakest_exclusive_presence_energy"], 5)

    def test_fit_includes_explanation_without_weakening_safety(self):
        rows = harness.row_samples({"g0_move": 5}, {"g0_move": 5})
        result = harness.fit(rows, ["g0_move"])
        self.assertEqual(result["status"], "unsafe")
        self.assertEqual(result["feasibility"]["status"], "conflict")
        self.assertEqual(result["proposals"]["g0_move"]["exclusive_presence_samples"], 100)
