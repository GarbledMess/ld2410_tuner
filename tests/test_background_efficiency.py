"""Background jobs (reference rebuilds, Seen by) are fast and light on a weak machine without
computing anything different: each fast path is checked against the plain computation it
replaced (``old_background``)."""

import math
import random
import sys
import types
import unittest
from copy import deepcopy

import old_background as old
import test_tuner as harness
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.rooms import attribution

refs = sys.modules["tuner_under_test.presence.references"]
training = sys.modules["tuner_under_test.presence.reference_training"]

NOW = 1_800_000_000.0
STATES = ("present", "not_present", "present", "not_present", "unknown")
SOURCES = ("human", "entity", "radar", "radar", "other")


def random_values(rng):
    values = {}
    for key in rng.sample(HISTORY_KEYS, rng.randrange(0, len(HISTORY_KEYS) + 1)):
        values[key] = rng.choice(
            (0, 1, 7, 50, 99, 100, 101, -1, 3.4, 99.5, 100.4, 0.5, 1.5, math.nan, math.inf, True)
        )
    if rng.random() < 0.2:
        values["unknown_gate"] = 5
    return values


def random_observations(rng, count):
    t, observations = NOW + rng.choice((0, 5.5)), []
    for _ in range(count):
        t += rng.choice((0, 1, 6, 6, 6, 6, 11, 40, 2000))
        observations.append(
            (
                t,
                random_values(rng),
                rng.choice(STATES),
                rng.choice(SOURCES),
                rng.choice((0, 0.3, 0.9, 1.0, 1.0, -1)),
            )
        )
    return observations


def random_profile(rng):
    profile = refs.empty()
    old.observe_many(profile, random_observations(rng, rng.choice((0, 30, 200))))
    if rng.random() < 0.3:  # More periods of one kind than are kept.
        copy = deepcopy(profile["periods"][:1]) or [
            {
                "source": "human",
                "state": "present",
                "start": NOW - 9e6,
                "end": NOW - 9e6,
                "confidence": 1.0,
                "count": 1,
                "data": refs.encode({}),
            }
        ]
        for index in range(refs.MAX_PERIODS + 4):
            extra = deepcopy(copy[0])
            extra["start"] = extra["end"] = NOW - 8e6 + index
            profile["periods"].insert(0, extra)
    return profile


class FoldingTests(unittest.TestCase):
    def test_observe_many_is_what_it_was_for_any_observations(self):
        rng = random.Random(1)
        for _ in range(150):
            before = random_profile(rng)
            observations = random_observations(rng, rng.choice((1, 10, 60, 400)))
            expected, folded = deepcopy(before), deepcopy(before)
            old.observe_many(expected, observations)
            refs.observe_many(folded, observations)
            self.assertEqual(folded, expected)

    def test_values_are_counted_as_they_were(self):
        rng = random.Random(2)
        for _ in range(300):
            values = random_values(rng)
            expected, counted = {}, {}
            old._add_values(expected, values)
            refs._add_values(counted, values)
            self.assertEqual(counted, expected)

    def test_one_stream_folds_into_two_profiles_as_two_streams_do(self):
        rng = random.Random(3)
        for _ in range(60):
            observations = random_observations(rng, rng.choice((20, 150)))
            profiles = {source: refs.empty() for source in ("entity", "radar")}
            expected = deepcopy(profiles)
            folds = {source: refs.Fold(profile) for source, profile in profiles.items()}
            for observation in observations:
                if observation[3] in folds:
                    folds[observation[3]].add(observation)
            for fold in folds.values():
                fold.close()
            for source, profile in expected.items():
                old.observe_many(profile, [o for o in observations if o[3] == source])
            self.assertEqual(profiles, expected)


def steps(rng, times, answers):
    """A reader that is constant between random change points."""
    edges = sorted(rng.choice(times) + rng.choice((0, 0.5, -0.5)) for _ in range(len(times) // 10))
    values = [rng.choice(answers) for _ in range(len(edges) + 1)]
    reads = []

    def read(t):
        reads.append(t)
        return values[sum(1 for edge in edges if edge <= t)]

    read.edges = sorted(set(edges))
    read.reads = reads
    return read


def observation_runtime(rows):
    runtime = types.SimpleNamespace(data={"devices": {"a": {}}})
    return harness.record_rows(runtime, "a", rows)


def folded_observations(runtime, own, room, automatic):
    profiles = {source: refs.empty() for source in (*training.ROOM_SOURCES, "human")}
    training._fold_native(runtime, "a", own, room, automatic, profiles, NOW - 1)
    return profiles


def point_observations(runtime):
    """Independent half-second grid: all synthetic label edges fall on this clock."""
    from tuner_under_test.history import spans

    for cell in spans.events(runtime, "a"):
        row = bytes(255 if value is None else int(value) for value in cell.values)
        for tick in range(round(cell.start * 2), round(cell.end * 2)):
            yield tick / 2, row


def oracle_profiles(runtime, own, room, automatic):
    points = types.SimpleNamespace(_iter_history_samples=lambda device: point_observations(runtime))
    auto = attribution.select(room, automatic)
    human = old.human_observations(points, None, own, room)
    inferred = old.room_observations(points, None, auto, own) if auto else ()
    profiles = {source: refs.empty() for source in (*training.ROOM_SOURCES, "human")}
    for observations in (human, inferred):
        folds = {source: refs.SpanFold(profile) for source, profile in profiles.items()}
        for timestamp, values, state, source, confidence in observations:
            folds[source].add(timestamp, timestamp + 0.5, values, state, source, confidence)
        for fold in folds.values():
            fold.close()
    return profiles


class RoomObservationTests(unittest.TestCase):
    def scene(self, rng):
        times = [
            NOW + i * 6 + (40 if rng.random() < 0.02 else 0) for i in range(rng.choice((0, 5, 200)))
        ]
        rows = [
            (t, bytes(rng.choice((rng.randrange(101), 101, 255)) for _ in HISTORY_KEYS))
            for t in times
        ]
        runtime = observation_runtime(rows)
        labels = [None, None, None, "present", "not_present", "unknown"]
        own = steps(rng, times or [NOW], labels)
        humans = [
            None,
            ("present", "human", 1.0),
            ("not_present", "human", 1.0),
            ("present", "attributed", 0.6),
        ]
        guesses = [
            None,
            ("present", "automatic", 0.8),
            ("not_present", "estimate", 0.7),
            ("present", "attributed", 0.9),
            ("not_present", "human", 1.0),
            ("present", "human", 1.0),
        ]
        room = steps(rng, times or [NOW], humans) if rng.random() < 0.8 else None
        automatic = steps(rng, times or [NOW], guesses) if rng.random() < 0.9 else None
        return runtime, own, room, automatic

    def test_one_pass_gives_the_observations_the_two_passes_gave(self):
        rng = random.Random(21)
        for _ in range(150):
            runtime, own, room, automatic = self.scene(rng)
            expected = oracle_profiles(runtime, own, room, automatic)
            found = folded_observations(runtime, own, room, automatic)
            for source in (*training.ROOM_SOURCES, "human"):
                actual_periods, expected_periods = (
                    found[source]["periods"],
                    expected[source]["periods"],
                )
                self.assertEqual(len(actual_periods), len(expected_periods))
                for actual, wanted in zip(actual_periods, expected_periods, strict=True):
                    self.assertAlmostEqual(actual["confidence"], wanted["confidence"], delta=1e-12)
                    self.assertEqual(refs.decode(actual["data"]), refs.decode(wanted["data"]))
                    self.assertEqual(
                        {k: v for k, v in actual.items() if k not in ("confidence", "data")},
                        {k: v for k, v in wanted.items() if k not in ("confidence", "data")},
                    )

    def test_a_sample_is_asked_about_the_room_once(self):
        rng = random.Random(22)
        times = [NOW + i * 6 for i in range(200)]
        room = steps(rng, times, [("present", "human", 1.0), None])
        automatic = steps(rng, times, [("present", "automatic", 0.8), None])
        rows = [(t, bytes(18)) for t in times]
        runtime = observation_runtime(rows)
        folded_observations(runtime, lambda t: None, room, automatic)
        self.assertEqual(len(room.reads), len(set(room.reads)), "each sample once")
        # Physical rows split at reader edges; each piece asks the room only once.
        from tuner_under_test.history import spans

        cells = list(spans.events(runtime, "a"))
        edges = training._native_edges([room, automatic])
        pieces = sum(len(list(training._native_pieces(cell, edges))) for cell in cells)
        self.assertEqual(len(room.reads), pieces)
