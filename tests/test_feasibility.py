"""Gate dependency counts, and the fit's feasibility field, which is never a guarantee."""

import unittest

import test_tuner as harness
from tuner_under_test.calibration.feasibility import exclusive_presence


class FeasibilityTests(unittest.TestCase):
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
        self.assertEqual(result["feasibility"]["status"], "not_assessed")
        self.assertEqual(result["proposals"]["g0_move"]["exclusive_presence_samples"], 100)

    def test_fit_reports_undetectable_presence_against_the_targets(self):
        # Zero energy never exceeds a threshold: every occupied observation is missed.
        # Several episodes, so the later ones are held out (whole-episode chronology).
        rows = harness.episode_samples({"g0_move": 0}, {"g0_move": 0})
        result = harness.fit(rows, ["g0_move"])
        self.assertEqual(result["status"], "unsafe")
        # Five occupied episodes in all; the newest fifth of the presence is the last one.
        for prefix, missed in (("", 5), ("Recent human labels: ", 1)):
            self.assertIn(
                f"{prefix}Estimated presence-time recall 0.000%; target 99.9%", result["warnings"]
            )
            self.assertIn(
                f"{prefix}{missed} occupied periods have no detection", result["warnings"]
            )
        self.assertTrue(
            any(w.startswith("Earlier-data backtest missed") for w in result["warnings"])
        )
