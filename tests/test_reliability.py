"""Outlier exclusion must be consistent, independent of candidate errors and auditable."""

import copy
import unittest
from unittest.mock import patch

import test_tuner as harness
from tuner_under_test.calibration import fitting
from tuner_under_test.calibration.metrics import metrics
from tuner_under_test.calibration.reliability import (
    filter_groups,
    fit_outlier_model,
    prepare_evidence,
)

KEYS = ["g0_move", "g1_still"]


def observations(dip=range(100, 105), count=1000):
    return {
        "present": [
            (i * 6, dict.fromkeys(KEYS, 5 if i in dip else 40), "present") for i in range(count)
        ],
        "not_present": [
            (10000 + i * 6, dict.fromkeys(KEYS, 5 if i % 20 == 0 else 2), "not_present")
            for i in range(count)
        ],
    }


def filtered(groups, keys=KEYS):
    return filter_groups(groups, fit_outlier_model(groups, keys))


def fit(groups, automatic=()):
    return harness.fit(groups["present"] + groups["not_present"], KEYS, automatic=automatic)


class ReliabilityTests(unittest.TestCase):
    def test_outliers_leave_all_calculations_and_remain_in_separate_audit(self):
        groups = observations()
        original = copy.deepcopy(groups)
        result = fit(groups)
        self.assertEqual(result["outlier_filter"]["human"]["excluded"]["present"], 5)
        for measurement in (result["training"], *result["proposals"].values()):
            self.assertEqual(measurement["false_positives"], 0)
            self.assertEqual(measurement["false_negatives"], 0)
            self.assertEqual(measurement["present_samples"], 995)
            self.assertEqual(measurement["not_present_samples"], 1000)
            self.assertEqual(measurement["longest_missed_run_samples"], 0)
        self.assertEqual(result["counts"]["present"], 995)
        self.assertEqual(result["feasibility"]["windows"][0]["present_samples"], 995)
        self.assertEqual(result["feasibility"]["status"], "not_ruled_out")
        self.assertEqual(result["raw_audit"]["false_negatives"], 5)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(groups, original)

    def test_sustained_quiet_presence_whole_episodes_and_boundaries_stay(self):
        for dip in (range(100, 107), range(0, 5), range(995, 1000), range(1000)):
            groups = observations(dip)
            retained, report = filtered(groups)
            self.assertEqual(retained, groups)
            self.assertEqual(report["excluded"]["present"], 0)
            result = fit(groups)
            self.assertEqual(result["training"]["false_negatives"], 0)
            self.assertEqual(result["status"], "unsafe")

    def test_low_but_distinct_quiet_signal_stays(self):
        groups = observations()
        for row in groups["not_present"]:
            row[1].update(dict.fromkeys(KEYS, 2))
        self.assertEqual(filtered(groups)[0], groups)
        result = fit(groups)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["training"]["false_negatives"], 0)
        self.assertTrue(any(p["threshold"] < 5 for p in result["proposals"].values()))

    def test_other_gate_presence_vetoes_exclusion(self):
        groups = observations()
        for row in groups["present"][100:105]:
            row[1][KEYS[1]] = 12
        # The second gate independently distinguishes this quiet location from noise.
        self.assertEqual(filtered(groups)[0], groups)
        self.assertEqual(fit(groups)["training"]["false_negatives"], 0)

    def test_gaps_label_changes_and_insufficient_context_stay(self):
        for variant in ("gap", "label", "missing", "duration", "context"):
            groups = observations()
            if variant == "gap":
                groups["present"] = [
                    (t + (60 if i >= 102 else 0), v, label)
                    for i, (t, v, label) in enumerate(groups["present"])
                ]
            if variant == "label":
                groups["not_present"].append((595, dict.fromkeys(KEYS, 2), "not_present"))
            if variant == "missing":
                groups["present"][102][1].clear()
            if variant == "duration":
                groups["present"] = [(i * 10, r[1], r[2]) for i, r in enumerate(groups["present"])]
            if variant == "context":
                groups["present"][98][1].update(dict.fromkeys(KEYS, 5))
            self.assertEqual(filtered(groups)[0], groups, variant)

    def test_partial_recording_does_not_hide_corroborated_outlier(self):
        groups = observations()
        del groups["present"][102][1][KEYS[0]]
        retained, report = filtered(groups)
        self.assertEqual(report["excluded"]["present"], 5)
        self.assertEqual(len(retained["present"]), 995)

    def test_insufficient_data_does_not_invent_outliers(self):
        groups = observations()
        for keys, negatives in (
            (KEYS, []),
            (KEYS, groups["not_present"][:49]),
            ([], groups["not_present"]),
        ):
            limited = {**groups, "not_present": negatives}
            self.assertEqual(filtered(limited, keys)[0], limited)

    def test_detector_does_not_trim_a_fixed_fraction_or_continuous_tail(self):
        groups = observations(dip=range(100, 120))
        self.assertEqual(filtered(groups)[0], groups)
        groups = observations()
        for i, row in enumerate(groups["present"]):
            row[1].update(dict.fromkeys(KEYS, i % 41))
        self.assertEqual(filtered(groups)[0], groups)

    def test_negative_spikes_remain_training_evidence(self):
        groups = observations(dip=())
        groups["not_present"][100][1].update(dict.fromkeys(KEYS, 90))
        retained, report = filtered(groups)
        self.assertEqual(retained["not_present"], groups["not_present"])
        self.assertEqual(report["excluded"]["not_present"], 0)
        self.assertEqual(fit(groups)["training"]["false_positives"], 1)

    def test_automatic_outliers_are_also_excluded_from_counts_and_metrics(self):
        groups = observations()
        automatic = [(*row, 0.8) for group in groups.values() for row in group]
        result = harness.fit([], KEYS, automatic=automatic)
        self.assertEqual(result["outlier_filter"]["automatic"]["excluded"]["present"], 5)
        self.assertEqual(result["automatic_evidence"]["samples"]["present"], 995)
        self.assertEqual(result["estimated_training"]["present_samples"], 995)
        self.assertEqual(result["estimated_training"]["false_negatives"], 0)

    def test_holdout_uses_only_earlier_detector_and_thresholds(self):
        groups = observations(dip=range(850, 855))
        with patch.object(fitting, "prepare_evidence", wraps=prepare_evidence) as prepare:
            result = fit(groups)
        full, earlier = prepare.call_args_list
        self.assertEqual(len(full.args[0]["present"]), 1000)
        self.assertEqual(len(earlier.args[0]["present"]), 800)
        self.assertEqual(result["outlier_filter"]["human"]["excluded"]["present"], 5)
        self.assertEqual(result["validation_outliers"]["excluded"]["present"], 0)
        self.assertEqual(result["validation"]["false_negatives"], 5)
        self.assertEqual(result["recent_training"]["false_negatives"], 0)

    def test_frozen_detector_filters_matching_holdout_and_caps_details(self):
        groups = observations(dip=range(100, 105))
        model = fit_outlier_model(groups, KEYS)
        later = observations(dip=range(200, 205))
        self.assertEqual(filter_groups(later, model)[1]["excluded"]["present"], 5)
        many = observations(dip=range(10, 390, 20), count=3000)
        _, report = filtered(many)
        self.assertEqual(report["excluded"]["present"], 19)
        self.assertEqual(report["period_count"], 19)
        self.assertEqual(len(report["periods"]), 12)
        # A new frequent behaviour must not be removed using an older detector.
        frequent = observations(dip=range(10, 390, 20), count=500)
        self.assertEqual(filter_groups(frequent, model)[0], frequent)

    def test_known_label_corruption_improves_separate_ground_truth_session(self):
        groups = observations()
        # Teach a genuine quiet location as well as the stronger common location.
        for row in groups["present"][300:400]:
            row[1].update(dict.fromkeys(KEYS, 12))
        result = fit(groups)
        truth = [(20000 + i * 6, dict.fromkeys(KEYS, 12), "present") for i in range(100)]
        truth += [
            (21000 + i * 6, dict.fromkeys(KEYS, 5 if i % 20 == 0 else 2), "not_present")
            for i in range(100)
        ]
        measured = metrics(truth, {k: v["threshold"] for k, v in result["proposals"].items()})
        self.assertEqual(measured["false_positives"], 0)
        self.assertEqual(measured["false_negatives"], 0)

    def test_automatic_labels_cannot_reintroduce_excluded_human_timestamps(self):
        groups = observations()
        guesses = [(*row, 0.99) for row in groups["present"][100:105]]
        result = fit(groups, guesses)
        self.assertEqual(result["outlier_filter"]["human"]["excluded"]["present"], 5)
        self.assertEqual(result["automatic_evidence"]["samples"]["present"], 0)
        self.assertEqual(result["training"]["present_samples"], 995)
