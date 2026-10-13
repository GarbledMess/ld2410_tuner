"""Attribution reads a radar's history in a few passes and keeps a few bytes per sample, and finds
what it always found: each fast path is checked against the plain computation it replaced
(``old_background``) on random radars, labels, guesses and room truth."""

import random
import tracemalloc
import unittest
from array import array

import old_background as old
import test_tuner  # noqa: F401  (registers tuner_under_test)
from sample_rows import attribute, samples_of
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history import guesses
from tuner_under_test.rooms import attribution, truth

NOW = 1_800_000_000.0
KEYS = ("g0_move", "g0_still", "g1_move")


def random_times(rng, count):
    """Sample times in order with recording gaps and the odd tie."""
    t, times = NOW + rng.choice((0, 0.25)), []
    for _ in range(count):
        t += rng.choice((6, 6, 6, 6, 6, 7, 0, 13, 40, 500))
        times.append(t)
    return times


def random_gates(rng, strong):
    """One sample's readings of the gates ``KEYS``: quiet, or loud on the gates that see."""
    gates = {}
    for key in KEYS:
        if rng.random() < 0.05:
            continue  # This gate did not read.
        gates[key] = rng.randint(30, 100) if strong and rng.random() < 0.6 else rng.randint(0, 9)
    return gates


def random_rows(rng, count=300):
    """Rows ``(time, gates, label)`` of a radar that is loud in stretches."""
    times, loud, rows = random_times(rng, count), False, []
    for t in times:
        if rng.random() < 0.05:
            loud = not loud
        rows.append((t, random_gates(rng, loud), "unknown"))
    return rows


def steps(rng, times, answers):
    """A reader that is constant between random change points, like every room reader."""
    edges = sorted(rng.choice(times) + rng.choice((0, 0.5, -0.5)) for _ in range(len(times) // 12))
    values = [rng.choice(answers) for _ in range(len(edges) + 1)]

    def read(t):
        return values[sum(1 for edge in edges if edge <= t)]

    return read


TRUTHS = (
    None,
    ("present", "human", 1.0),
    ("present", "automatic", 0.8),
    ("not_present", "human", 1.0),
    ("not_present", "estimate", 0.7),
    ("present", "human", 1.0),
    ("not_present", "human", 1.0),
)
LABELS = (None, None, None, "present", "not_present", "unknown")


def scenario(rng, count=300, groups=None):
    rows = random_rows(rng, count)
    times = [row[0] for row in rows]
    own = steps(rng, times, LABELS)
    truths = {
        name: steps(rng, times, TRUTHS) for name in (groups or rng.choice((("g",), ("g", "h"))))
    }
    return rows, times, own, truths


class SamplesTests(unittest.TestCase):
    def test_recorded_samples_read_like_the_rows_they_replace(self):
        rng = random.Random(1)
        for _ in range(60):
            count = rng.choice((0, 1, 5, 200))
            rows = [
                (
                    NOW + i * 6 + rng.choice((0, 0, 1)),
                    bytes(rng.choice((rng.randrange(101), 101, 255)) for _ in HISTORY_KEYS),
                )
                for i in range(count)
            ]
            shuffled = list(rows)
            if rng.random() < 0.3:
                rng.shuffle(shuffled)
            samples = attribution.Samples.recorded(shuffled)
            ordered = sorted(shuffled, key=lambda row: row[0])  # Ties stay in the order read.
            self.assertEqual(list(samples.times), [t for t, _ in ordered])
            self.assertEqual(len(samples), len(ordered))
            for index, key in enumerate(HISTORY_KEYS):
                self.assertEqual(samples.column(key), bytes(row[index] for _, row in ordered))

    def test_a_row_of_another_length_or_kind_is_read_as_missing_gates(self):
        rows = [(NOW, bytes(range(20))), (NOW + 6, [5] * 10), (NOW + 12, bytes([7] * 18))]
        samples = attribution.Samples.recorded(rows)
        self.assertEqual(samples.column("g0_move"), bytes([0, 5, 7]))
        self.assertEqual(samples.column("g8_still"), bytes([17, 255, 7]))

    def test_dictionary_rows_become_samples(self):
        rows = [(NOW, {"a": 5, "b": 1}, "unknown"), (NOW + 6, {"a": 100}, "unknown")]
        samples = samples_of(rows)
        self.assertEqual(samples.column("a"), bytes([5, 100]))
        self.assertEqual(samples.column("b"), bytes([1, 255]))
        with self.assertRaises(ValueError):
            samples_of([(NOW, {"a": 101}, "unknown")])

    def test_a_gate_that_was_never_read_reads_as_missing(self):
        samples = samples_of([(NOW, {"a": 5}, "unknown"), (NOW + 6, {"a": 6}, "unknown")])
        self.assertEqual(samples.column("zz"), bytes([255, 255]))
        self.assertFalse(attribution._sustained(samples, "zz", 3))

    def test_slices_keep_times_and_gates_together(self):
        rows = random_rows(random.Random(2), 50)
        samples = samples_of(rows)
        part = samples[10:20]
        self.assertEqual(list(part.times), [row[0] for row in rows[10:20]])
        for key in KEYS:
            self.assertEqual(part.column(key), samples.column(key)[10:20])
        self.assertEqual(
            list(samples.between(rows[5][0], rows[9][0]).times),
            [row[0] for row in rows if rows[5][0] <= row[0] <= rows[9][0]],
        )

    def test_a_sample_costs_a_few_bytes_not_a_dictionary(self):
        """Regression guard: 30 days of one radar (430k samples) were half a gigabyte of rows."""
        raws = [bytes((i + k) % 101 for k in range(len(HISTORY_KEYS))) for i in range(20_000)]
        tracemalloc.start()
        try:
            samples = attribution.Samples.recorded((NOW + i, raw) for i, raw in enumerate(raws))
            kept = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        self.assertEqual(len(samples), 20_000)
        self.assertLess(kept / len(samples), 40, "bytes kept per sample")


def quiet_by_label(own, truths):
    return attribution.quiet_reader(own, truths)


class BaselineTests(unittest.TestCase):
    def test_the_baseline_is_what_it_was(self):
        rng = random.Random(3)
        for _ in range(60):
            rows, _times, own, truths = scenario(rng)
            quiet = attribution.quiet_reader(own, truths)
            expected = old.baseline(rows, old.quiet_reader(own, truths))
            self.assertEqual(attribution.baseline(samples_of(rows), quiet), expected)

    def test_a_baseline_of_nothing(self):
        self.assertEqual(attribution.baseline(samples_of([]), lambda _t: True), {})
        rows = random_rows(random.Random(4), 30)
        self.assertEqual(attribution.baseline(samples_of(rows), lambda _t: False), {})

    def test_quiet_means_the_radars_own_empty_label_or_every_group_empty(self):
        rng = random.Random(5)
        for _ in range(60):
            _rows, times, own, truths = scenario(rng)
            for t in times:
                self.assertEqual(
                    attribution.quiet_reader(own, truths)(t), old.quiet_reader(own, truths)(t)
                )
        self.assertTrue(attribution.quiet_reader(lambda _t: None, {})(0))


class EpisodesTests(unittest.TestCase):
    def test_episodes_are_what_they_were(self):
        rng = random.Random(6)
        for _ in range(100):
            _rows, times, _own, truths = scenario(rng, rng.choice((0, 1, 40, 400)))
            for read in truths.values():
                self.assertEqual(attribution.episodes(read, times), old.episodes(read, times))
                self.assertEqual(
                    attribution.episodes(read, array("d", times), 7), old.episodes(read, times, 7)
                )


class AttributeTests(unittest.TestCase):
    def test_a_radars_decision_is_what_it_was(self):
        rng = random.Random(7)
        for _ in range(60):
            rows, times, own, truths = scenario(rng)
            base = old.baseline(rows, old.quiet_reader(own, truths))
            for read in truths.values():
                for span in old.episodes(read, times):
                    expected = old.attribute(rows, span, base, own)
                    self.assertEqual(attribute(rows, span, base, own), expected)

    def test_a_run_of_loud_samples_is_sustained_across_no_gap(self):
        rng = random.Random(8)
        for _ in range(200):
            count = rng.randrange(1, 60)
            times = random_times(rng, count)
            ceiling = rng.choice((9, 12, 25, 59))
            rows = [
                (t, {"g0_move": rng.choice((0, ceiling, ceiling + 1, 60))}, "unknown")
                for t in times
            ]
            self.assertEqual(
                attribution._sustained(samples_of(rows), "g0_move", ceiling),
                old._sustained(rows, "g0_move", ceiling),
            )


class AttributionsTests(unittest.TestCase):
    def test_the_attributions_of_a_radar_are_what_they_were(self):
        rng = random.Random(9)
        for _ in range(60):
            rows, _times, own, truths = scenario(rng, rng.choice((20, 300)))
            expected = old.attributions("r", rows, own, truths)
            self.assertEqual(
                attribution._attributions("r", samples_of(rows), own, truths), expected
            )
            samples = samples_of(rows)
            self.assertEqual(attribution._attributions("r", samples, own, truths), expected)


def random_runs(rng, times, step=6):
    """Guess runs over the span of ``times``: stretches of one guess with gaps between."""
    runs, t, end = [], times[0], times[-1]
    while t < end:
        length = rng.choice((6, 30, 120, 600))
        if rng.random() < 0.8:
            level = rng.choice((8, 10, 11, 12, 16, 20))  # Steps of 5 %: under 55 % is no guess.
            runs.append(
                (t, t + length, rng.choice(("present", "not_present")), level, guesses.REFERENCE)
            )
        t += length + rng.choice((0, 0, 6, 60))
    return runs


def random_label(rng, times, source):
    start = rng.choice(times) + rng.choice((0, 0.5))
    state = rng.choice(("present", "not_present", "unknown"))
    label = {
        "start": start,
        "end": start + rng.choice((12, 90, 700)),
        "state": state,
        "source": source,
    }
    if source == "sensor":
        label["confidence"] = rng.choice((0.4, 0.6, 0.9))
    return label


class FillTests(unittest.TestCase):
    """A memo filled stretch by stretch between the times its readers can change answers
    exactly as it would be filled one sample at a time."""

    def scene(self, rng):
        times = random_times(rng, rng.choice((30, 300)))
        members = ["a", "b", "c"][: rng.choice((1, 2, 3))]
        devices = {}
        for name in members:
            device = {}
            if rng.random() < 0.5:
                device["history_labels"] = [
                    random_label(rng, times, "manual") for _ in range(rng.randrange(1, 4))
                ]
            if rng.random() < 0.3:
                device["training_state"] = rng.choice(("present", "not_present"))
                device["training_label_start"] = rng.choice(times)
                device["training_expires_at"] = rng.choice((None, rng.choice(times) + 100))
            devices[name] = device
        zone = {"name": "Zone", "area_id": "x", "kind": "zone", "device_ids": members[:1]}
        area = {"name": "Area", "area_id": "x", "kind": "area", "device_ids": members}
        labels = {
            "zone:z": [
                random_label(rng, times, rng.choice(("manual", "sensor"))) for _ in range(3)
            ],
            "area:x": [
                random_label(rng, times, rng.choice(("manual", "sensor"))) for _ in range(3)
            ],
        }
        saved = {
            "device_id": members[0],
            "own": ["zone:z", "area:x"],
            "groups": {"zone:z": zone, "area:x": area},
            "room_labels": labels,
            "members": {name: attribution.label_fields(device) for name, device in devices.items()},
        }
        others = {name: guesses.Guesses(random_runs(rng, times)) for name in members}
        return times, saved, others

    def test_answering_by_stretches_is_reading_every_sample(self):
        rng = random.Random(10)
        for _ in range(40):
            times, saved, others = self.scene(rng)
            for with_estimate in (True, False):
                contexts = attribution._contexts(saved, others if with_estimate else None)
                for group, context in contexts.items():
                    reads = []

                    def read(t, context=context, reads=reads):
                        reads.append(t)
                        return truth.room_truth(context, t)

                    memo = attribution.Memo(read, array("d", times), truth.edges(context))
                    queried = rng.sample(times, len(times) // 2) + times
                    self.assertEqual(
                        [memo(t) for t in queried],
                        [truth.room_truth(context, t) for t in queried],
                        group,
                    )
                    self.assertLessEqual(len(reads), len(times))
                    self.assertEqual(len(reads), len(set(reads)), "no sample is read twice")

    def test_a_memo_reads_once_for_each_stretch_it_is_asked_about(self):
        times = [NOW + i * 6 for i in range(1000)]
        label = ("present", "human", 1.0)
        reads = []
        memo = attribution.Memo(
            lambda t: reads.append(t) or label, array("d", times), [times[100], times[500]]
        )
        for t in times[10:50] + times[20:30]:
            memo(t)
        self.assertEqual(len(reads), 1, "one stretch asked about")
        self.assertEqual({memo(t) for t in times}, {label})
        self.assertEqual(len(reads), 3, "three stretches in all")

    def test_edges_before_after_and_between_the_samples(self):
        times = [10.0, 20.0, 30.0, 40.0]
        seen = []
        memo = attribution.Memo(
            lambda t: seen.append(t) or ("present", "human", t),
            array("d", times),
            [5.0, 20.0, 25.0, 99.0],
        )
        # Stretches with samples: [10], [20], [30, 40]; the changes at 5 and 99 add none.
        self.assertEqual([memo(t)[2] for t in times[::-1]], [40.0, 40.0, 20.0, 10.0])
        self.assertEqual(seen, [40.0, 20.0, 10.0])

    def test_without_edges_every_sample_is_read_for_itself(self):
        times = [10.0, 20.0, 30.0]
        memo = attribution.Memo(lambda t: ("present", "human", t), array("d", times))
        self.assertEqual([memo(t)[2] for t in times], times)
