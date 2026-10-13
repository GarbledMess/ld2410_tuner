"""Learning reads and replays only what it scores, and finds what it found when it read and
replayed everything."""

import base64
import math
import random
import struct
import time
import tracemalloc
import types
import unittest
import zlib
from array import array
from unittest.mock import patch

import old_background as old
import old_truth
import test_tuner  # noqa: F401  (registers tuner_under_test)
from sample_rows import samples_of
from test_attribution_efficiency import LABELS, TRUTHS, FillTests, random_rows
from test_efficiency import DAY, BlockIndexTests, odd_blocks, random_blocks
from tuner_under_test.calibration.metrics import threshold_masks
from tuner_under_test.calibration.timing_metrics import hit_mask
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history import codec, guesses, recording
from tuner_under_test.history.labels import index_reader, label_index
from tuner_under_test.rooms import attribution, evidence, truth

NOW = 1_800_000_000.0


BLOCK = 50


def stored_view(count):
    """A view of ``count`` samples, 6 s apart, stored in blocks, whose reads honour ``since``
    and ``until`` the way stored history does: a block that ends before ``since`` or starts
    at or after ``until`` is never decoded (``view.decoded`` lists the samples decoded)."""
    samples = [(NOW + i * 6, bytes((i + k) % 101 for k in range(18))) for i in range(count)]
    view = types.SimpleNamespace(
        data={"devices": {"a": {"guess_runs": [], "history_labels": []}}},
        _history_runtime={},
        decoded=[],
    )

    def columns(device, since=0, until=math.inf):
        for first in range(0, count, BLOCK):
            block = samples[first : first + BLOCK]
            if block[-1][0] >= since and block[0][0] < until:
                view.decoded.extend(ts for ts, _ in block)
                yield test_tuner.columns_of(block)().__next__()

    view._history_columns = columns
    return view, samples


class EvidenceReadTests(unittest.TestCase):
    def test_only_the_window_is_read_from_history(self):
        view, samples = stored_view(2000)
        start, end = NOW + 3000, NOW + 6000
        found = evidence.read(view, "a", ["g0_move"], start, end)
        expected = [ts for ts, _ in samples if start <= ts <= end]
        self.assertEqual([row[0] for row in found["rows"]], expected)
        self.assertLessEqual(len(view.decoded), len(expected) + 2 * BLOCK, "blocks outside it")

    def test_a_sample_at_the_very_end_is_read(self):
        view, samples = stored_view(100)
        end = samples[50][0]
        found = evidence.read(view, "a", ["g0_move"], NOW, end)
        self.assertEqual(found["rows"][-1][0], end)

    def test_reading_a_window_gives_the_rows_reading_everything_gave(self):
        rng = random.Random(7)
        for _ in range(20):
            view, samples = stored_view(rng.randrange(1, 300))
            start = NOW + rng.uniform(-100, 1500)
            end = start + rng.uniform(0, 1500)
            found = evidence.read(view, "a", ["g0_move", "g1_still"], start, end)
            self.assertEqual(
                [row[0] for row in found["rows"]], [ts for ts, _ in samples if start <= ts <= end]
            )


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


def scene(rng, count, calls=None):
    rows = random_rows(rng, count)
    times = [row[0] for row in rows]
    own = stepped(rng, times, LABELS, calls)
    truths = {
        name: stepped(rng, times, TRUTHS, calls)
        for name in rng.choice((("g",), ("g", "h"), ("g", "h", "i")))
    }
    return rows, times, own, truths


class StretchTests(unittest.TestCase):
    """The attribution of a radar reads a room reader once for each stretch of samples between
    the times it can change its answer, and finds what reading it at every sample found."""

    def test_episodes_are_the_runs_of_present_truth_split_at_gaps(self):
        rng = random.Random(11)
        for _ in range(150):
            _rows, times, _own, truths = scene(rng, rng.choice((0, 1, 2, 40, 400)))
            for read in truths.values():
                for series, gap in ((times, 30), (array("d", times), 7), (times, 1e9)):
                    self.assertEqual(
                        attribution.episodes(read, series, gap), old.episodes(read, series, gap)
                    )

    def test_the_baseline_is_the_quiet_samples_gates(self):
        rng = random.Random(12)
        for _ in range(100):
            rows, _times, own, truths = scene(rng, rng.choice((0, 1, 30, 300)))
            expected = old.baseline(rows, old.quiet_reader(own, truths))
            found = attribution.baseline(samples_of(rows), attribution.quiet_reader(own, truths))
            self.assertEqual(found, expected)

    def test_the_attributions_of_a_radar_are_what_reading_every_sample_found(self):
        rng = random.Random(13)
        for _ in range(100):
            rows, _times, own, truths = scene(rng, rng.choice((1, 20, 300)))
            self.assertEqual(
                attribution._attributions("r", samples_of(rows), own, truths),
                old.attributions("r", rows, own, truths),
            )

    def test_readers_are_read_once_for_a_stretch_not_for_a_sample(self):
        rng = random.Random(14)
        calls = []
        rows = random_rows(rng, 2000)
        times = [row[0] for row in rows]
        own = stepped(rng, times, LABELS, calls, 20)
        truths = {"g": stepped(rng, times, TRUTHS, calls, 20)}
        attribution._attributions("r", samples_of(rows), own, truths)
        self.assertLess(len(calls), len(times) / 4, "reads for 2000 samples")

    def test_labels_over_samples_are_read_once_for_a_stretch(self):
        rng = random.Random(15)
        for _ in range(100):
            rows = random_rows(rng, rng.choice((1, 5, 100)))
            times = array("d", [row[0] for row in rows])
            calls = []
            own = stepped(rng, list(times), LABELS, calls)
            self.assertEqual(attribution._states(own, times), {own(t) for t in times})
            calls.clear()
            attribution._states(own, times)
            self.assertLessEqual(len(calls), len(own.edges) + 1)

    def test_a_reader_that_cannot_say_where_it_changes_is_read_at_every_sample(self):
        calls = []

        def plain(t):
            calls.append(t)
            return ("present", "human", 1.0) if t % 12 < 6 else None

        times = [float(t) for t in range(0, 120, 6)]
        self.assertEqual(attribution.episodes(plain, times), old.episodes(plain, times))
        self.assertGreaterEqual(len(calls), len(times))
        rows = random_rows(random.Random(2), 40)
        quiet = [lambda t: t % 12 < 6]
        self.assertEqual(
            attribution.baseline(samples_of(rows), quiet[0]), old.baseline(rows, quiet[0])
        )


class RoomTruthTests(unittest.TestCase):
    """Room truth reads each member's labels once and answers as it did reading them at every
    level."""

    def test_room_truth_is_what_it_was(self):
        rng = random.Random(21)
        scenes = FillTests()
        for _ in range(60):
            times, saved, others = scenes.scene(rng)
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
        scenes = FillTests()
        for _ in range(60):
            times, saved, others = scenes.scene(rng)
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

    def test_a_memo_filled_in_bulk_answers_as_room_truth_does(self):
        rng = random.Random(23)
        scenes = FillTests()
        for _ in range(40):
            times, saved, others = scenes.scene(rng)
            contexts = attribution._contexts(saved, others)
            samples = array("d", times)
            memos = attribution._full_truths(contexts, samples)
            for group, context in contexts.items():
                self.assertEqual(
                    [memos[group](t) for t in times],
                    [truth.room_truth(context, t) for t in times],
                )

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


def legacy_block(rng, version, start, count=30):
    """A stored block of an older format (plain zlib, 20 or 22 bytes a sample)."""
    stride = codec.STRIDES[version]
    raw = b"".join(
        struct.pack(">H", i * 6 + rng.choice((0, 0, 1))) + rng.randbytes(stride - 2)
        for i in range(count)
    )
    data = base64.b64encode(zlib.compress(raw, 6)).decode("ascii")
    return {
        "version": version,
        "start": start,
        "end": start + count * 6,
        "count": count,
        "data": data,
    }


def flat(batches):
    """The samples ``(time, row)`` of batches ``(times, columns)``."""
    rows = []
    for times, columns in batches:
        assert len(columns) == len(HISTORY_KEYS)
        assert all(len(column) == len(times) for column in columns)
        rows += [(t, bytes(column[i] for column in columns)) for i, t in enumerate(times)]
    return rows


class HistoryColumnsTests(unittest.TestCase):
    """Stored history is read as columns, a byte for each sample of a gate, and holds the samples
    that reading it row by row gave."""

    def test_columns_hold_the_samples_rows_read(self):
        rng = random.Random(31)
        build = BlockIndexTests().runtime
        with patch.object(time, "time", return_value=STORE_NOW):
            for _ in range(60):
                blocks = random_blocks(
                    rng, STORE_NOW - 31 * DAY + rng.random() * DAY, rng.randrange(300)
                )
                blocks += [
                    legacy_block(rng, version, STORE_NOW - rng.random() * 5 * DAY)
                    for version in rng.sample((1, 2, 3, 4), rng.randrange(5))
                ]
                history = blocks + odd_blocks(rng, blocks)
                if rng.random() < 0.5:
                    rng.shuffle(history)
                pending = [(STORE_NOW - 5 + i, rng.randbytes(18)) for i in range(rng.randrange(3))]
                runtime = build(history, pending)
                device = runtime.data["devices"]["a"]
                for _ in range(4):
                    since = rng.choice((0, STORE_NOW - 3600, STORE_NOW - rng.random() * 40 * DAY))
                    until = rng.choice(
                        (math.inf, STORE_NOW - rng.random() * 30 * DAY, STORE_NOW + 1)
                    )
                    self.assertEqual(
                        flat(runtime._history_columns(device, since, until)),
                        list(runtime._iter_history_samples(device, since, until)),
                    )

    def test_samples_read_as_columns_are_the_samples_read_as_rows(self):
        rng = random.Random(32)
        build = BlockIndexTests().runtime
        with patch.object(time, "time", return_value=STORE_NOW):
            for _ in range(30):
                blocks = random_blocks(rng, STORE_NOW - 3 * DAY, rng.randrange(1, 200))
                blocks += [
                    legacy_block(rng, version, STORE_NOW - rng.random() * DAY) for version in (1, 2)
                ]
                rng.shuffle(blocks)
                pending = [(STORE_NOW - 5 + i, rng.randbytes(18)) for i in range(rng.randrange(3))]
                runtime = build(blocks, pending)
                device = runtime.data["devices"]["a"]
                expected = attribution.Samples.recorded(runtime._iter_history_samples(device))
                found = attribution.Samples.read(runtime._history_columns(device))
                self.assertEqual(found.keys, expected.keys)
                self.assertEqual(found.times, expected.times)
                self.assertEqual(found.matrix, expected.matrix)
        self.assertEqual(len(attribution.Samples.read(iter(()))), 0)

    def test_a_block_that_cannot_be_read_gives_no_samples(self):
        good = random_blocks(random.Random(2), STORE_NOW - DAY, 5)[0]
        for block in (
            {},
            {**good, "data": "!"},
            {**good, "count": good["count"] + 1},
            {**good, "version": 9},
        ):
            self.assertIsNone(recording.block_columns(block, 0))

    def test_columns_of_a_block_are_the_codecs_columns_without_interleaving(self):
        rng = random.Random(3)
        for stride in (20, 22):
            for count in (0, 1, 7, 60):
                raw = rng.randbytes(count * stride)
                encoded = codec.encode_payload(raw, stride)
                columns = codec.decode_column_list(encoded, stride, count)
                self.assertEqual(columns, [raw[c::stride] for c in range(stride)])


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

    def test_read_gives_the_rows_and_barriers_reading_every_sample_gave(self):
        rng = random.Random(41)
        build = BlockIndexTests().runtime
        with patch.object(time, "time", return_value=STORE_NOW):
            for _ in range(40):
                blocks = random_blocks(rng, STORE_NOW - 3 * DAY, rng.randrange(1, 120))
                rng.shuffle(blocks)
                pending = [
                    (STORE_NOW - 5 + i, bytes(rng.choice((5, 100, 101, 255)) for _ in range(18)))
                    for i in range(rng.randrange(3))
                ]
                runtime = build(blocks, pending)
                keys = rng.sample(HISTORY_KEYS, rng.randrange(0, 19))
                start = STORE_NOW - rng.choice((3, 1.5, 0.5)) * DAY
                end = start + rng.choice((0.2, 1, 5)) * DAY
                found = evidence.read(runtime, "a", keys, start, end)
                rows, barriers = old.evidence_rows(runtime, "a", keys, start, end)
                self.assertEqual(found["barriers"], barriers)
                self.assertEqual(listed(found["rows"]), rows)
                self.assertEqual(len(found["rows"]), len(rows))

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
        view, _ = stored_view(0)
        samples = [(NOW + i * 6, bytes((i + k) % 101 for k in range(18))) for i in range(20_000)]
        view._history_columns = test_tuner.columns_of(samples)
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
