"""The room gate search ranks fewer candidates without choosing differently: it is checked
against the search that ranked every candidate (``old_search``) on random rooms."""

import random
import types
import unittest
from copy import deepcopy

import old_search as old
import test_tuner  # noqa: F401  (registers tuner_under_test)
from test_learning_efficiency import TIMINGS, random_thresholds, room_labels, wide_member
from tuner_under_test.rooms import fitting
from tuner_under_test.rooms.objective import LinkedObjective, Objective


def room(rng, guessing=True, keys="ab"):
    members = {key: wide_member(rng, key, rng.choice(TIMINGS), guessing=guessing) for key in keys}
    return members, room_labels(rng)


def counted(objective):
    """``objective`` with its ranks counted in ``objective.ranked``."""
    objective.ranked = 0
    plain = objective.rank

    def rank(thresholds):
        objective.ranked += 1
        return plain(thresholds)

    objective.rank = rank
    return objective


class SearchEquivalenceTests(unittest.TestCase):
    def test_mixed_source_search_and_bounds_with_positive_automatic_weight(self):
        mixed = 0
        for seed in range(4):
            rng = random.Random(8000 + seed)
            members, labels = room(rng)
            labels["room_labels"]["area:office"] = [
                {"start": 0, "end": 100, "state": "present", "source": "manual"},
                {"start": 100, "end": 200, "state": "not_present", "source": "manual"},
            ]
            groups = {"g1": members, "g2": {"b": members["b"]}}
            for weight in (0.5, 1.0):
                for make in (
                    lambda members=members, labels=labels, weight=weight: Objective(
                        members, 0, 900, labels, automatic_weight=weight
                    ),
                    lambda groups=groups, labels=labels, weight=weight: LinkedObjective(
                        groups, 0, 900, {"g1": labels, "g2": labels}, automatic_weight=weight
                    ),
                ):
                    fast, plain = make(), make()
                    parts = [fast] if isinstance(fast, Objective) else list(fast.groups.values())
                    mixed += sum(any(item.shares) for item in parts)
                    self.assertEqual(fitting.search(fast), old.search(plain))
                    current = fast.current
                    for bound, rank in zip(
                        fast.bound(current, current), fast.rank(current)[:-3], strict=True
                    ):
                        self.assertLessEqual(bound, rank + 1e-9)
        self.assertGreater(mixed, 0, "the fixtures must exercise automatic/human blending")

    def test_tail_counts_supported_distance_and_only_changed_gates(self):
        supported = {
            "separated": True,
            "noise_ceiling": 10,
            "preferred_threshold": 40,
            "presence_reference": 70,
        }
        objective = types.SimpleNamespace(
            current={"a": {"x": 20, "y": 30}},
            preferred={"a": {"x": 40, "y": 45}},
            preferences={"a": {"x": supported, "y": {**supported, "separated": False}}},
        )
        self.assertEqual(Objective.tail(objective, {"a": {"x": 25, "y": 30}}), (15, 1, 15))
        self.assertEqual(Objective.tail(objective, {"a": {"x": 20, "y": 50}}), (20, 1, 5))

    def test_the_search_finds_what_ranking_every_candidate_found(self):
        for seed in range(12):
            rng = random.Random(1000 + seed)
            members, labels = room(rng, guessing=seed % 3 != 0)
            fast, plain = Objective(members, 0, 900, labels), Objective(members, 0, 900, labels)
            self.assertEqual(fitting.search(fast), old.search(plain), seed)

    def test_a_linked_search_finds_what_ranking_every_candidate_found(self):
        for seed in range(6):
            rng = random.Random(2000 + seed)
            members, _ = room(rng)
            groups = {"g1": members, "g2": {"b": members["b"]}}
            fast, plain = LinkedObjective(groups, 0, 900), LinkedObjective(groups, 0, 900)
            self.assertEqual(fitting.search(fast), old.search(plain), seed)

    def test_a_sweep_chooses_what_ranking_every_candidate_chose(self):
        for seed in range(10):
            rng = random.Random(3000 + seed)
            members, labels = room(rng)
            objective = Objective(members, 0, 900, labels)
            candidates = {
                (device, key): old.options(objective, device, key)
                for device, gates in objective.current.items()
                for key in gates
            }
            fast = random_thresholds(rng, objective)
            plain = deepcopy(fast)
            self.assertEqual(
                fitting._sweep(objective, fast, candidates),
                old.sweep(objective, plain, candidates),
            )
            self.assertEqual(fast, plain, seed)

    def test_a_sweep_that_cannot_improve_keeps_every_threshold(self):
        rng = random.Random(4000)
        members, labels = room(rng)
        objective = Objective(members, 0, 900, labels)
        candidates = {
            (device, key): old.options(objective, device, key)
            for device, gates in objective.current.items()
            for key in gates
        }
        thresholds = random_thresholds(rng, objective)
        while old.sweep(objective, thresholds, candidates):
            pass
        settled = deepcopy(thresholds)
        self.assertFalse(fitting._sweep(objective, thresholds, candidates))
        self.assertEqual(thresholds, settled)


class SearchEffortTests(unittest.TestCase):
    def test_fewer_candidates_are_ranked_than_there_are(self):
        rng = random.Random(5000)
        members, labels = room(rng)
        fast, plain = (counted(Objective(members, 0, 900, labels)) for _ in range(2))
        self.assertEqual(fitting.search(fast), old.search(plain))
        self.assertLess(fast.ranked, plain.ranked / 2)

    def test_the_tail_of_a_rank_needs_no_replay(self):
        rng = random.Random(6000)
        members, labels = room(rng)
        groups = {"g1": members, "g2": {"b": members["b"]}}
        for objective in (Objective(members, 0, 900, labels), LinkedObjective(groups, 0, 900)):
            for _ in range(8):
                thresholds = random_thresholds(rng, objective)
                self.assertEqual(objective.tail(thresholds), objective.rank(thresholds)[-3:])

    def test_the_bound_is_never_above_the_rank_between_its_ends(self):
        for seed in range(8):
            rng = random.Random(7000 + seed)
            members, labels = room(rng, guessing=seed % 2 == 0)
            groups = {"g1": members, "g2": {"b": members["b"]}}
            for objective in (Objective(members, 0, 900, labels), LinkedObjective(groups, 0, 900)):
                for _ in range(10):
                    thresholds = random_thresholds(rng, objective)
                    device, key = rng.choice(
                        sorted((d, k) for d, g in thresholds.items() for k in g)
                    )
                    low, mid, high = sorted(rng.sample(range(0, 101), 3))
                    at = [
                        {**thresholds, device: {**thresholds[device], key: value}}
                        for value in (low, mid, high)
                    ]
                    ranked = objective.rank(at[1])[:-3]
                    for ends in ((at[0], at[2]), (None, at[2]), (at[0], None), (None, None)):
                        for bound, entry in zip(objective.bound(*ends), ranked, strict=True):
                            self.assertLessEqual(bound, entry + 1e-9)


if __name__ == "__main__":
    unittest.main()
