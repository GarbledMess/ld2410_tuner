"""A detection is the OR of every enabled gate, never an average of gate scores."""

import unittest

import test_tuner as harness
from tuner_under_test.calibration.metrics import metrics


class HolisticTests(unittest.TestCase):
    def test_disjoint_presence_coverage_is_valid(self):
        keys = ["g2_still", "g3_still"]
        rows = [
            (i * 6, {keys[0]: 80 if i < 100 else 3, keys[1]: 3 if i < 100 else 75}, "present")
            for i in range(200)
        ]
        rows += [(2000 + i * 6, dict.fromkeys(keys, 5), "not_present") for i in range(200)]
        result = harness.fit(rows, keys)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["training"]["false_negatives"], 0)
        self.assertTrue(
            all(proposal["false_negatives"] == 100 for proposal in result["proposals"].values())
        )

    def test_saturated_empty_gate_can_be_disabled_if_other_gate_covers_presence(self):
        keys = ["g2_still", "g4_still"]
        rows = harness.row_samples({keys[0]: 70, keys[1]: 80}, {keys[0]: 100, keys[1]: 5}, 200)
        result = harness.fit(rows, keys)
        self.assertEqual(result["proposals"][keys[0]]["threshold"], 100)
        self.assertLess(result["proposals"][keys[1]]["threshold"], 80)
        self.assertEqual(result["training"]["false_negatives"], 0)
        self.assertEqual(result["training"]["false_positives"], 0)
        self.assertEqual(result["status"], "ok")

    def test_one_false_gate_is_a_device_false_positive_without_double_counting(self):
        rows = [
            (0, {"g0_still": 70, "g1_still": 0}, "not_present"),
            (6, {"g0_still": 70, "g1_still": 80}, "not_present"),
        ]
        self.assertEqual(metrics(rows, {"g0_still": 50, "g1_still": 50})["false_positives"], 2)
