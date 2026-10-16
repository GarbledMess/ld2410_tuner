"""Learning reads and replays only what it scores, and finds what it found when it read and
replayed everything."""

import random
import tracemalloc
import types
import unittest
from array import array

import old_truth
import room_scenes
import test_tuner  # noqa: F401  (registers tuner_under_test)
from tuner_under_test.calibration.metrics import threshold_masks
from tuner_under_test.calibration.timing_metrics import hit_mask
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history import guesses
from tuner_under_test.history.label_lookup import index_reader, label_index
from tuner_under_test.rooms import attribution, evidence, truth

NOW = 1_800_000_000.0


def stepped(rng, times, answers, calls=None, changes=None):
    """A reader that is constant between random change points and says where they are
    (``edges``), like the label and room readers; ``calls`` counts its reads."""
    spread = (times[0], times[-1] + 1) if times else (0, 1)
    edges = sorted(
        rng.choice((*times, rng.uniform(*spread), spread[0] - 5, spread[1] + 5))
        for _ in range(
            rng.choice((0, 1, 4, max(1, len(times) // 12), len(times)))
            if changes is None
            else changes
        )
    )
    values = [rng.choice(answers) for _ in range(len(edges) + 1)]

    def read(t):
        if calls is not None:
            calls.append(t)
        return values[sum(1 for edge in edges if edge <= t)]

    read.edges = edges
    return read


class RoomTruthTests(unittest.TestCase):
    """Room truth reads each member's labels once and answers as it did reading them at every
    level."""

    def test_an_empty_estimate_batch_answers_like_individual_reads(self):
        read = truth.estimate_reader({})
        self.assertEqual(read.bulk([1.0, 2.0]), [read(1.0), read(2.0)])
        self.assertEqual(read.bulk([]), [])

    def test_room_truth_is_what_it_was(self):
        rng = random.Random(21)
        for _ in range(60):
            times, saved, others = room_scenes.scene(rng)
            probes = [*times, *(t + 0.25 for t in times[::7]), times[0] - 1, times[-1] + 700]
            contexts = list(attribution._contexts(saved, others).values())
            inputs = {
                name: {
                    "manual": index_reader(label_index(device)),
                    "automatic": guesses.Guesses(others[name].runs if name in others else ()),
                }
                for name, device in saved["members"].items()
            }
            view = {
                "group_id": "area:x",
                "groups": saved["groups"],
                "room_labels": saved["room_labels"],
            }
            contexts.append(truth.for_view(view, inputs))
            for context in contexts:
                for t in probes:
                    self.assertEqual(
                        truth.room_truth(context, t), old_truth.room_truth(context, t), t
                    )

    def test_room_truths_are_room_truth_at_each_time(self):
        rng = random.Random(22)
        for _ in range(60):
            times, saved, others = room_scenes.scene(rng)
            probes = [*times, *(t + 0.25 for t in times[::7]), times[0] - 1, times[-1] + 700]
            contexts = list(attribution._contexts(saved, others).values())
            inputs = {
                name: {
                    "manual": index_reader(label_index(device)),
                    "automatic": guesses.Guesses(others[name].runs if name in others else ()),
                }
                for name, device in saved["members"].items()
            }
            view = {
                "group_id": "area:x",
                "groups": saved["groups"],
                "room_labels": saved["room_labels"],
            }
            contexts.append(truth.for_view(view, inputs))
            contexts.append(truth.context(view, "area:x", inputs, None))
            for context in contexts:
                self.assertEqual(
                    truth.room_truths(context, probes),
                    [truth.room_truth(context, t) for t in probes],
                )
        self.assertEqual(truth.room_truths(contexts[0], []), [])

    def test_a_member_is_read_once_for_each_sample(self):
        calls = []

        def manual(t):
            calls.append(t)

        context = truth.context(
            None, None, {"a": {"manual": manual, "automatic": guesses.EMPTY}}, None
        )
        truth.room_truth(context, 5.0)
        self.assertEqual(len(calls), 1)


STORE_NOW = 2_000_000_000.0


def random_rows_object(rng, keys, count):
    columns = [
        bytes(rng.choice((0, 3, 20, 50, 51, 99, 100, rng.randrange(101))) for _ in range(count))
        for _ in keys
    ]
    times = array("d", [1_800_000_000.0 + 6 * i + rng.choice((0, 0, 0.25)) for i in range(count)])
    return evidence.Rows(times, columns, {key: i for i, key in enumerate(keys)})


def listed(rows):
    """The same readings as a list of ``(time, {gate: energy}, "unknown")``."""
    return [(row[0], dict(row[1].items()), row[2]) for row in rows]


class RowsTests(unittest.TestCase):
    """A radar's readings are kept as a column of bytes for each gate, and read like the list
    of ``(time, readings, "unknown")`` rows they replace."""

    def test_rows_slice_index_and_iterate_like_a_list(self):
        rng = random.Random(42)
        rows = random_rows_object(rng, ("a", "b"), 50)
        expected = listed(rows)
        self.assertEqual(listed(rows[10:20]), expected[10:20])
        self.assertEqual(listed(rows[::5]), expected[::5])
        self.assertEqual(listed([rows[-1]]), expected[-1:])
        self.assertEqual(listed(list(rows)), expected)
        self.assertEqual(list(rows[10:20].times), [row[0] for row in expected[10:20]])
        with self.assertRaises(IndexError):
            rows[50]

    def test_rows_in_time_order_are_the_rows_sorted_by_time(self):
        rng = random.Random(43)
        rows = random_rows_object(rng, ("a",), 60)
        self.assertIs(rows.in_time_order(), rows)
        order = list(range(60))
        rng.shuffle(order)
        shuffled = evidence.Rows(
            array("d", [rows.times[i] for i in order]),
            [bytes(rows.columns[0][i] for i in order)],
            rows.layout,
        )
        self.assertEqual(
            listed(shuffled.in_time_order()),
            sorted(listed(shuffled), key=lambda row: row[0]),
        )

    def test_threshold_masks_of_a_column_are_those_of_its_rows(self):
        rng = random.Random(44)
        for _ in range(30):
            rows = random_rows_object(rng, ("a", "b"), rng.choice((0, 1, 7, 64, 300)))
            for key in ("a", "b"):
                masks = threshold_masks(rows, key)
                self.assertEqual(masks, threshold_masks(listed(rows), key))
                self.assertEqual(len({id(mask) for mask in masks}), len(set(masks)), "shared")

    def test_hit_masks_of_columns_are_those_of_rows(self):
        rng = random.Random(45)
        for _ in range(60):
            rows = random_rows_object(rng, ("a", "b", "c"), rng.choice((0, 1, 7, 64, 300)))
            thresholds = {
                key: rng.choice((-1, 0, 3, 20, 20.0, 20.5, 50.9, 99, 100, 255, 300))
                for key in rng.sample(("a", "b", "c", "unknown"), rng.randrange(0, 5))
            }
            self.assertEqual(
                hit_mask(rows, thresholds), hit_mask(listed(rows), thresholds), thresholds
            )

    def test_a_sample_costs_a_few_bytes_not_an_object(self):
        """Regression guard: a week of a radar's rows of objects were 25 MB."""
        view = types.SimpleNamespace(
            data={"devices": {"a": {"history_labels": []}}}, _history_runtime={}
        )
        readings = [(NOW + i * 6, bytes((i + k) % 101 for k in range(18))) for i in range(20_000)]
        test_tuner.record_rows(view, "a", readings)
        tracemalloc.start()
        try:
            found = evidence.read(view, "a", HISTORY_KEYS, NOW, NOW + 1e6)
            kept = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        self.assertEqual(len(found["rows"]), 20_000)
        self.assertLess(kept / len(found["rows"]), 40, "bytes kept per sample")


if __name__ == "__main__":
    unittest.main()
