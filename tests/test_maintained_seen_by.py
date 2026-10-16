"""Seen-by ownership reads source changes rather than every retained timestamp."""

import unittest
from math import inf, nextafter

import test_tuner  # noqa: F401
from tuner_under_test.rooms import attribution, seen_by


class OwnershipTests(unittest.TestCase):
    def test_fixed_member_tail_cannot_preserve_greedy_merge_ownership(self):
        members = {
            key: [
                (
                    (12 * number + offset, 12 * number + offset),
                    {"state": "yes", "episode": number},
                    False,
                )
                for number in range(count)
            ]
            for key, count, offset in (("a", 1000, 0), ("b", 999, 6))
        }
        expected = seen_by._merge(members)[-seen_by.EPISODE_LIMIT :]
        # The missing predecessor changes which member enters each row first. This
        # phase shift persists to the newest row even with an extra guard per member.
        for guard in (0, 1, 10):
            bounded = {
                key: items[-seen_by.EPISODE_LIMIT - guard :] for key, items in members.items()
            }
            self.assertNotEqual(seen_by._merge(bounded)[-seen_by.EPISODE_LIMIT :], expected)

    def test_human_episode_index_is_read_once_for_all_full_episodes(self):
        class CountedEpisodes(list):
            visited = 0

            def __iter__(self):
                for item in super().__iter__():
                    self.visited += 1
                    yield item

        result = {"state": "yes", "confidence": 1.0}
        human = CountedEpisodes(
            [((i * 60, nextafter(i * 60 + 30, -inf)), result) for i in range(100)]
        )

        def read(t):
            return "present", "human", 1.0

        read.edges = []
        full = {
            "truths": {"g": read},
            "times": list(range(0, 6000, 6)),
            "found": {"g": list(human)},
            "contexts": {"g": {"boundaries": (), "members": {}, "estimate": None}},
        }
        human.visited = 0
        found = seen_by._native_episodes(human, full, "g")
        self.assertEqual(len(found), 100)
        self.assertTrue(all(nextafter(span[1], inf) - span[0] == 30 for span, _, _ in found))
        self.assertLessEqual(
            human.visited, 100, "every human episode reread for every full episode"
        )

    def test_rebasing_a_small_subset_does_not_visit_full_history_cuts(self):
        class CountedCuts(list):
            visited = 0

            def __iter__(self):
                for value in super().__iter__():
                    self.visited += 1
                    yield value

        def read(t):
            return "present", "human", 1.0

        read.times = list(range(20000))
        read.cuts = CountedCuts(range(1, 20000))
        found = attribution.change_points(list(range(15000, 15010)), read)
        self.assertEqual(list(found), list(range(11)))
        self.assertLess(read.cuts.visited, 50, "every historical cut scanned for one episode")

    def test_a_constant_owner_is_read_once_for_the_episode(self):
        calls = []

        def read(t):
            calls.append(t)
            return "present", "human", 1.0

        read.edges = []
        times = list(range(0, 6000, 6))
        result = {"state": "yes", "confidence": 1.0}
        found = seen_by._pieces(times, [((0, 5994), result)], read)
        self.assertEqual(found, [((0, 5994), result)])
        self.assertEqual(len(calls), 1, "Seen-by still reads every sample")

    def test_owner_boundaries_inside_full_episodes_are_preserved(self):
        def read(t):
            return "present", "human" if t < 18 or t >= 42 else "estimate", 1.0

        read.edges = [18, 42]
        one, two = {"state": "yes"}, {"state": "no"}
        self.assertEqual(
            seen_by._pieces(list(range(0, 60, 6)), [((0, 12), one), ((42, 54), two)], read),
            [((0, 12), one), ((18, 36), None), ((42, 54), two)],
        )

    def test_owner_change_points_are_rebased_for_an_episode_subset(self):
        def read(t):
            return "present", "human" if t < 42 else "estimate", 1.0

        read.edges = [42]
        one = {"state": "yes"}
        self.assertEqual(
            seen_by._pieces(list(range(30, 60, 6)), [((30, 36), one)], read),
            [((30, 36), one), ((42, 54), None)],
        )


if __name__ == "__main__":
    unittest.main()
