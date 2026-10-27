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

    def test_automatic_share_can_spend_margin_without_sacrificing_human_labels(self):
        rows = harness.row_samples({"g0_still": 80}, {"g0_still": 20})
        guesses = [(300 + i, {"g0_still": 25}, "present", 0.99) for i in range(500)]
        manual = harness.fit(rows, ["g0_still"], automatic=guesses, automatic_weight=0)
        result = harness.fit(rows, ["g0_still"], automatic=guesses, automatic_weight=0.2)
        self.assertEqual(manual["proposals"]["g0_still"]["threshold"], 50)
        self.assertEqual(result["proposals"]["g0_still"]["threshold"], 24)
        self.assertTrue(result["automatic_evidence"]["used"])
        self.assertEqual(result["training"]["false_negatives"], 0)
        self.assertEqual(result["training"]["false_positives"], 0)

    def test_actual_quiet_presence_overrides_the_preferred_margin(self):
        rows = harness.row_samples({"g0_still": 80}, {"g0_still": 20}, count=1000)
        rows[100:110] = [(i, {"g0_still": 24}, "present") for i in range(100, 110)]
        result = harness.fit(rows, ["g0_still"])
        self.assertEqual(result["proposals"]["g0_still"]["separation"]["preferred_threshold"], 50)
        self.assertLess(result["proposals"]["g0_still"]["threshold"], 24)
        self.assertEqual(result["training"]["false_negatives"], 0)
        self.assertEqual(result["outlier_filter"]["excluded"], False)

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


def test_room_preference_construction_has_bounded_row_overhead():
    import tracemalloc
    from array import array

    from tuner_under_test.rooms.evidence import Rows
    from tuner_under_test.rooms.objective import Objective

    count = 30000
    rows = Rows(
        array("d", range(count)),
        [bytes(20 if i % 2 else 80 for i in range(count))],
        {"g0_move": 0},
        ends=array("d", range(1, count + 1)),
    )
    objective = Objective.__new__(Objective)
    objective.inputs = {"synthetic": {"rows": rows}}
    objective.current = {"synthetic": {"g0_move": 42}}
    objective.quality = None

    def label(t):
        return "not_present" if int(t) % 2 else "present", "human", 1.0

    objective.truth = {
        "own": label,
        "parent": None,
        "zones": [],
        "members": {},
        "sensor": lambda t: None,
        "estimate": None,
    }
    expected = {
        "g0_move": {
            "preferred_threshold": 50,
            "noise_ceiling": 20,
            "presence_reference": 80,
            "separated": True,
            "human_supported": True,
        }
    }
    assert objective._preferences("synthetic", label) == expected
    tracemalloc.start()
    try:
        actual = objective._preferences("synthetic", label)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert actual == expected
    assert peak < 5_000_000


def test_compact_preferences_preserve_sorted_fractional_weights_and_support():
    from array import array

    from tuner_under_test.rooms.evidence import Rows
    from tuner_under_test.rooms.preferences import PreferenceRows

    values = bytes([0, 20, 80, 255, 80, 20, 90] * 20)
    rows = Rows(array("d", range(len(values))), [values], {"g0_move": 0})
    groups = {state: PreferenceRows(rows) for state in EMPTY}
    ordinary = {state: [] for state in EMPTY}
    for index, energy in enumerate(values):
        state = "present" if index % 2 else "not_present"
        weight = (0.1, 0.125, 0.3)[index % 3]
        groups[state].add(index, weight)
        ordinary[state].append(
            (index, {} if energy == 255 else {"g0_move": energy}, state, 0.7, weight)
        )
    for allowed in (None, frozenset(), frozenset((20, 80))):
        compact = {state: group.restricted("g0_move", allowed) for state, group in groups.items()}
        filtered = {
            state: [row for row in group if allowed is None or row[1].get("g0_move") in allowed]
            for state, group in ordinary.items()
        }
        for key in ("g0_move", "g1_move"):
            assert gate_preference(key, EMPTY, compact, 42) == gate_preference(
                key, EMPTY, filtered, 42
            )
            assert gate_preference(key, compact, EMPTY, 42) == gate_preference(
                key, filtered, EMPTY, 42
            )
