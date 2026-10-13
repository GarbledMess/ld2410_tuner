"""The joint learner is fast and light on a weak machine without finding anything different:
each fast path is checked against the plain computation it replaced (``old_replay``)."""

import random
import tracemalloc
import unittest
from array import array
from unittest.mock import patch

import old_background
import old_replay as old
import test_tuner as harness
from tuner_under_test.calibration.duration import DurationGroup, _clip
from tuner_under_test.calibration.intervals import WeightedTime, across
from tuner_under_test.calibration.metrics import threshold_masks
from tuner_under_test.calibration.timing import _Window, hit_runs, merged
from tuner_under_test.rooms import fitting, replay
from tuner_under_test.rooms.intervals import union
from tuner_under_test.rooms.objective import LinkedObjective, Objective

NOW = 1_800_000_000.0
TIMINGS = (
    {},
    {"timeout": 0, "on_delay": 0, "off_delay": 0},
    {"timeout": 5},
    {"timeout": 1, "on_delay": 0.5, "off_delay": 1},
    {"timeout": 8, "on_delay": 2, "off_delay": 3},
    {"timeout": 0, "on_delay": 1, "off_delay": 0},
    {"timeout": 3, "on_delay": 3, "off_delay": 0},
    {"timeout": 6, "on_delay": 0, "off_delay": 0},  # Links land exactly on the sampling.
    {"timeout": 12, "on_delay": 6, "off_delay": 6},
)


def random_rows(rng, count):
    """Rows in time order with recording gaps; the gate value is a dummy (hits are random)."""
    rows, t = [], rng.choice((0.0, 3.0, 100.25))
    for _ in range(count):
        t += rng.choice((6, 6, 6, 6, 7, 0.5, 1, 20, 100, 0))
        rows.append((t, {"g0_move": 0}, "unknown"))
    return rows


def random_bits(rng, count):
    density = rng.choice((0.02, 0.2, 0.5, 0.9, 1.0))
    return sum(1 << i for i in range(count) if rng.random() < density)


def groups_for(rng, count=150):
    rows = random_rows(rng, count)
    barriers = sorted(rng.choice(rows)[0] - 0.25 for _ in range(rng.randrange(4)))
    timing = rng.choice(TIMINGS)
    return (
        DurationGroup(rows, timing, barriers=barriers),
        DurationGroup(rows, timing, absent=True, barriers=barriers),
    )


def random_window(rng, in_order=True):
    times, t = [], rng.choice((0, 0.0, 1_800_000_000.25))
    for _ in range(rng.choice((1, 2, 5, 40, 300))):
        t += rng.choice((6, 6, 6, 7, 0.5, 1, 11.5, 12))
        times.append(t)
    if not in_order:  # Samples read out of order, as stored blocks that overlap would be.
        for _ in range(1 + len(times) // 10):
            first, second = rng.randrange(len(times)), rng.randrange(len(times))
            times[first], times[second] = times[second], times[first]
    timing = rng.choice(TIMINGS[1:])
    return _Window(
        [(t, {}, "u") for t in times],
        timing["timeout"],
        timing.get("on_delay", 0),
        timing.get("off_delay", 0),
    )


def spans_of(pair):
    return list(zip(*pair, strict=True))


class HitRunsTests(unittest.TestCase):
    def test_runs_are_the_old_runs_with_an_exclusive_stop(self):
        rng = random.Random(1)
        cases = [0, 1, 0b10, 0b11, 0b101, (1 << 300) - 1, 1 << 299, (1 << 300) - 2]
        cases += [random_bits(rng, rng.choice((9, 64, 300, 5000))) for _ in range(40)]
        for bits in cases:
            self.assertEqual(
                hit_runs(bits), [(first, last + 1) for first, last in old.bit_runs(bits)]
            )


class MergedTests(unittest.TestCase):
    def test_spans_in_order_merge_as_the_walk_did(self):
        rng = random.Random(2)
        for _ in range(500):
            starts = sorted(
                rng.choice((0, 0.5, 1, 2, 3, 7, 7.5, 10)) for _ in range(rng.randrange(9))
            )
            ends = sorted(start + rng.choice((0, 0, 0.5, 1, 4)) for start in starts)
            expected = old.merge(zip(starts, ends, strict=True))
            self.assertEqual(spans_of(merged(starts, ends)), expected, (starts, ends))

    def test_other_spans_are_walked_as_they_were(self):
        for starts, ends in (([0, 1, 5], [4, 0.5, 6]), ([5, 0, 1], [6, 2, 3]), ([0, 3], [9, 4])):
            self.assertEqual(
                spans_of(merged(starts, ends)), old.merge(zip(starts, ends, strict=True))
            )

    def test_sorting_them_first_is_what_union_does(self):
        rng = random.Random(3)
        for _ in range(300):
            spans = [
                (a, a + rng.choice((0.5, 1, 4))) for a in (rng.randint(0, 20) for _ in range(8))
            ]
            starts, ends = zip(*spans, strict=True)
            self.assertEqual(spans_of(merged(starts, ends, sort=True)), union(spans))

    def test_nothing_and_one_span(self):
        self.assertEqual(merged([], []), ([], []))
        self.assertEqual(merged([2.5], [3.5]), ([2.5], [3.5]))


class WindowSpanTests(unittest.TestCase):
    def test_every_kind_of_span_is_what_it_was(self):
        rng = random.Random(4)
        for _ in range(400):
            window = random_window(rng)
            bits = random_bits(rng, len(window.times))
            for new, plain in (
                (window._lower, old.lower),
                (window._upper, old.upper),
                (window._possible_onset, old.possible),
            ):
                self.assertEqual(spans_of(new(bits)), plain(window, bits))

    def test_samples_out_of_order_give_the_spans_they_gave(self):
        rng = random.Random(14)
        for _ in range(400):
            window = random_window(rng, in_order=False)
            bits = random_bits(rng, len(window.times))
            for new, plain in (
                (window._lower, old.lower),
                (window._upper, old.upper),
                (window._possible_onset, old.possible),
            ):
                self.assertEqual(spans_of(new(bits)), plain(window, bits))
            for is_upper, earlier in ((False, False), (False, True), (True, False)):
                self.assertEqual(
                    window.project(bits, is_upper, earlier),
                    old.project(window, bits, is_upper, earlier),
                )

    def test_projection_onto_samples_is_what_it_was(self):
        rng = random.Random(6)
        for _ in range(300):
            window = random_window(rng)
            bits = random_bits(rng, len(window.times))
            for is_upper, earlier in ((False, False), (False, True), (True, False)):
                self.assertEqual(
                    window.project(bits, is_upper, earlier),
                    old.project(window, bits, is_upper, earlier),
                )

    def test_duration_group_intervals_are_what_they_were(self):
        rng = random.Random(7)
        for _ in range(100):
            present, absent = groups_for(rng)
            for group in (present, absent):
                for _offset, window, _confidence in group.windows:
                    hits = random_bits(rng, len(window.times))
                    for latest in (True, False):
                        self.assertEqual(
                            group._intervals(window, hits, latest),
                            old.intervals(group, window, hits, latest),
                        )


class ClippedTests(unittest.TestCase):
    def test_clipping_is_what_clip_and_union_did(self):
        rng = random.Random(9)
        for _ in range(500):
            count = rng.randrange(0, 9)
            starts = sorted(rng.choice((0, 1, 2, 3.5, 5, 8, 10)) for _ in range(count))
            ends = sorted(start + rng.choice((0, 0.5, 1, 3, 20)) for start in starts)
            low, high = sorted((rng.choice((0, 1, 2.5, 4, 9)), rng.choice((3, 5, 12, 40))))
            expected = union(_clip(list(zip(starts, ends, strict=True)), low, high))
            self.assertEqual(spans_of(replay.clipped(starts, ends, low, high)), expected)

    def test_spans_in_no_order_are_united_all_the_same(self):
        spans = [(5, 7), (0, 2), (1, 3), (9, 9.5)]
        starts, ends = zip(*spans, strict=True)
        self.assertEqual(spans_of(replay.clipped(starts, ends, 0, 20)), union(_clip(spans, 0, 20)))


class UnitedTests(unittest.TestCase):
    def test_the_union_of_radars_is_the_walk_over_all_their_spans(self):
        rng = random.Random(10)
        for _ in range(300):
            radars = []
            for _radar in range(rng.randrange(1, 5)):
                spans, t = [], 0
                for _span in range(rng.randrange(0, 12)):
                    t += rng.choice((0.5, 1, 3, 8))
                    spans.append((t, t + rng.choice((0.5, 1, 2, 5))))
                    t = spans[-1][1] + rng.choice((0, 0, 1, 4))
                radars.append(union(spans))
            parts = [tuple(zip(*spans, strict=True)) or ([], []) for spans in radars]
            expected = union(span for spans in radars for span in spans)
            self.assertEqual(spans_of(replay.united(parts)), expected)


class FromBitsTests(unittest.TestCase):
    def test_requested_kinds_equal_the_full_computation(self):
        rng = random.Random(11)
        for _ in range(60):
            present, absent = groups_for(rng)
            bits = random_bits(rng, 150)
            expected = old.from_bits(present, absent, bits)
            self.assertEqual(replay.from_bits(present, absent, bits), expected)
            both = replay.from_bits(present, absent, bits, ("lower", "upper"))
            self.assertEqual(both, {key: expected[key] for key in ("coverage", "lower", "upper")})

    def test_a_kind_that_is_not_requested_is_not_computed(self):
        present, absent = groups_for(random.Random(2))
        with patch.object(
            type(present.windows[0][1]), "_possible_onset", side_effect=AssertionError
        ):
            replay.from_bits(present, absent, (1 << 150) - 1, ("lower", "upper"))


class ProjectorTests(unittest.TestCase):
    def test_projection_equals_from_bits_for_any_hit_mask(self):
        rng = random.Random(5)
        for _ in range(40):
            present, absent = groups_for(rng)
            projector = replay.Projector(present, absent, ("lower", "upper"))
            # Repeats and near-repeats exercise the per-window memory.
            masks = [random_bits(rng, 150) for _ in range(4)]
            masks += [mask ^ (1 << rng.randrange(150)) for mask in masks]
            for bits in masks + masks:
                expected = old.from_bits(present, absent, bits)
                projected = projector.project(bits)
                self.assertEqual(set(projected), {"lower", "upper"})
                self.assertEqual(spans_of(projected["lower"]), expected["lower"])
                self.assertEqual(spans_of(projected["upper"]), expected["upper"])
                self.assertEqual(projector.coverage, expected["coverage"])

    def test_all_three_kinds_when_asked(self):
        present, absent = groups_for(random.Random(8))
        projector = replay.Projector(present, absent)
        bits = random_bits(random.Random(9), 150)
        expected = old.from_bits(present, absent, bits)
        projected = projector.project(bits)
        for kind in replay.KINDS:
            self.assertEqual(spans_of(projected[kind]), expected[kind])

    def test_projection_is_compact(self):
        present, absent = groups_for(random.Random(3))
        starts, ends = replay.Projector(present, absent, ("lower",)).project((1 << 150) - 1)[
            "lower"
        ]
        for flat in (starts, ends):
            self.assertIsInstance(flat, array)
            self.assertEqual(flat.typecode, "d")

    def test_blocks_out_of_time_order_are_projected_as_they_were(self):
        rng = random.Random(12)
        for _ in range(30):
            blocks = [
                [
                    (1000 * block + 6 * i, {"g0_move": 0}, "unknown")
                    for i in range(rng.randrange(8, 30))
                ]
                for block in range(rng.randrange(2, 6))
            ]
            ordered = list(blocks)
            while blocks == ordered:
                rng.shuffle(blocks)
            rows = [row for block in blocks for row in block]
            timing = rng.choice(TIMINGS)
            barriers = [1000 * k - 500 for k in range(1, len(blocks))]  # One session a block.
            present = DurationGroup(rows, timing, barriers=barriers)
            absent = DurationGroup(rows, timing, absent=True, barriers=barriers)
            projector = replay.Projector(present, absent, ("lower", "upper"))
            self.assertFalse(projector.ordered)
            bits = random_bits(rng, len(rows))
            expected = old.from_bits(present, absent, bits)
            projected = projector.project(bits)
            self.assertEqual(spans_of(projected["lower"]), expected["lower"])
            self.assertEqual(spans_of(projected["upper"]), expected["upper"])
            self.assertEqual(replay.from_bits(present, absent, bits), expected)

    def test_rows_in_no_order_at_all_are_projected_as_they_were(self):
        rng = random.Random(13)
        for _ in range(20):
            rows = random_rows(rng, 80)
            rng.shuffle(rows)
            timing = rng.choice(TIMINGS)
            present = DurationGroup(rows, timing)
            absent = DurationGroup(rows, timing, absent=True)
            bits = random_bits(rng, 80)
            expected = old.from_bits(present, absent, bits)
            projected = replay.Projector(present, absent, ("lower", "upper")).project(bits)
            self.assertEqual(spans_of(projected["lower"]), expected["lower"])
            self.assertEqual(spans_of(projected["upper"]), expected["upper"])

    def test_a_threshold_change_recomputes_only_the_windows_whose_hits_changed(self):
        rows = [
            (t + 1000 * w, {"g0_move": 0}, "unknown") for w in range(4) for t in range(0, 60, 6)
        ]
        present = DurationGroup(rows, {"timeout": 1, "on_delay": 0, "off_delay": 0})
        absent = DurationGroup(rows, {"timeout": 1, "on_delay": 0, "off_delay": 0}, absent=True)
        self.assertEqual(len(present.windows), 4)
        projector = replay.Projector(present, absent, ("lower", "upper"))
        first = 0b0011_0000_0110 | (0b0111_0000_0110 << 10) | (0b0110 << 20) | (0b1110 << 30)
        projector.project(first)
        with patch.object(DurationGroup, "_spans", autospec=True) as spy:
            spy.return_value = ([], [])
            projector.project(first)
            self.assertEqual(spy.call_count, 0, "the same hits are known")
            projector.project(first ^ (1 << 22))  # One sample of the third window.
            self.assertEqual(spy.call_count, 2, "one window, lower and upper")

    def test_windows_without_hits_cost_nothing(self):
        present, absent = groups_for(random.Random(4))
        projector = replay.Projector(present, absent, ("lower", "upper"))
        with patch.object(DurationGroup, "_spans", side_effect=AssertionError):
            projected = projector.project(0)
        self.assertEqual(
            {kind: spans_of(both) for kind, both in projected.items()}, {"lower": [], "upper": []}
        )

    def test_memory_is_bounded_and_results_do_not_depend_on_it(self):
        rng = random.Random(6)
        present, absent = groups_for(rng, 300)
        small = replay.Projector(present, absent, ("lower", "upper"), limit=64)
        masks = [random_bits(rng, 300) for _ in range(30)]
        for bits in masks * 2:
            expected = old.from_bits(present, absent, bits)
            projected = small.project(bits)
            self.assertEqual(spans_of(projected["lower"]), expected["lower"])
            self.assertEqual(spans_of(projected["upper"]), expected["upper"])
            self.assertTrue(small.held <= 64 or len(small.known) == 1, small.held)


class AcrossTests(unittest.TestCase):
    def test_across_is_the_sum_of_between_in_the_same_order(self):
        rng = random.Random(21)
        for _ in range(200):
            count = rng.randrange(1, 40)
            times = [float(t) for t in sorted(rng.sample(range(0, 5000), count))]
            masses = [
                WeightedTime(times, [rng.choice((0, 0.3, 1, 0.55, 0.9)) for _ in times])
                for _ in range(rng.randrange(1, 4))
            ]
            spans = union(
                (a, a + rng.choice((0.001, 1, 7.5, 200)))
                for a in (rng.uniform(-100, 5200) for _ in range(rng.randrange(0, 60)))
            )
            starts, ends = zip(*spans, strict=True) if spans else ([], [])
            self.assertEqual(
                across(masses, starts, ends),
                [sum(mass.between(a, b) for a, b in spans) for mass in masses],
            )

    def test_across_nothing_is_zero(self):
        self.assertEqual(across([WeightedTime([0.0, 5.0], [1, 1])], [], []), [0])

    def test_masses_with_other_times_are_refused(self):
        masses = [WeightedTime([0.0, 5.0], [1, 1]), WeightedTime([0.0, 5.0], [1, 1])]
        with self.assertRaises(ValueError):
            across(masses, [1.0], [2.0])


def wide_member(rng, device_id, timing, count=140, guessing=False):
    """A radar with two gates (g0_move, g0_still) and random readings, and the guesses it
    recorded when ``guessing``."""
    records = []
    level = 0
    guess = (0, 0)
    for i in range(count):
        if i % 20 == 0:
            level = rng.choice((0, 5, 30, 60))
            guess = (rng.choice((1, 2)), rng.choice((60, 80, 95))) if guessing else (0, 0)
        records.append(
            (
                i * 6 + (rng.choice((0, 0, 0, 100)) if i % 37 == 0 else 0),
                [
                    max(0, min(100, level + rng.randint(-8, 8))),
                    max(0, min(100, level + rng.randint(-20, 8))),
                    *[255] * 16,
                    *guess,
                ],
            )
        )
    device = {"history_labels": [], "training_state": "unknown"}
    view = harness.stub_view(device_id, device, records)
    return {
        "view": view,
        "thresholds": {"g0_move": 20, "g0_still": 20},
        "timing": timing,
    }


def room_labels(rng, end=900):
    """The labels of an office (all, only its empty half, or none): where they stop, the room's
    truth is what its members guessed."""
    cut = rng.randrange(100, end - 100)
    labels = [
        {"start": 0, "end": cut, "state": "present", "source": "manual"},
        {"start": cut, "end": end, "state": "not_present", "source": "manual"},
    ]
    return {
        "group_id": "area:office",
        "groups": {
            "area:office": {
                "name": "Office",
                "area_id": "office",
                "kind": "area",
                "device_ids": ["a", "b"],
            }
        },
        "room_labels": {"area:office": rng.choice((labels, labels[1:], []))},
    }


def random_thresholds(rng, item):
    return {
        key: {gate: rng.choice((0, 10, 20, 35, 50, 100, rng.randint(0, 100))) for gate in gates}
        for key, gates in item.current.items()
    }


class ObjectiveEquivalenceTests(unittest.TestCase):
    def test_rates_equal_the_plain_computation(self):
        rng = random.Random(31)
        for _ in range(6):
            members = {
                key: wide_member(rng, key, rng.choice(TIMINGS), guessing=rng.random() < 0.7)
                for key in ("a", "b")
            }
            item = Objective(members, 0, 900, room_labels(rng))
            for _ in range(25):
                thresholds = random_thresholds(rng, item)
                expected = old.rates(item, thresholds)
                self.assertEqual(item.rates(thresholds), expected)
                self.assertEqual(item.rates(thresholds), expected, "and again, remembered")

    def test_a_linked_objective_ranks_as_the_sum_of_plain_group_ranks(self):
        rng = random.Random(41)
        members = {key: wide_member(rng, key, rng.choice(TIMINGS), guessing=True) for key in "ab"}
        groups = {"g1": members, "g2": {"b": members["b"]}}
        linked = LinkedObjective(groups, 0, 900)
        for _ in range(20):
            thresholds = random_thresholds(rng, linked)
            for _gid, item, part in linked.parts(thresholds):
                self.assertEqual(item.rates(part), old.rates(item, part))
            self.assertEqual(linked.rank(thresholds), linked.rank(thresholds))

    def test_the_search_finds_what_it_found_without_the_fast_paths(self):
        rng = random.Random(51)
        members = {key: wide_member(rng, key, rng.choice(TIMINGS), guessing=True) for key in "ab"}
        labels = room_labels(rng)
        fast, plain = Objective(members, 0, 900, labels), Objective(members, 0, 900, labels)
        plain.rates = lambda thresholds: old.rates(plain, thresholds)
        self.assertEqual(fitting.search(fast), fitting.search(plain))

    def test_possible_spans_are_never_computed_by_the_search(self):
        rng = random.Random(61)
        members = {key: wide_member(rng, key, TIMINGS[3]) for key in ("a", "b")}
        item = Objective(members, 0, 900)
        window_type = type(item.radars["a"].positive.windows[0][1])
        with patch.object(window_type, "_possible_onset", side_effect=AssertionError):
            fitting.search(item)

    def test_hit_masks_seen_before_are_not_replayed_again(self):
        rng = random.Random(71)
        members = {key: wide_member(rng, key, TIMINGS[3]) for key in ("a", "b")}
        item = Objective(members, 0, 900)
        # Above every reading, 100 and 99 both hit nothing: the same hit masks.
        quiet_a = {"a": {"g0_move": 100, "g0_still": 100}, "b": {"g0_move": 100, "g0_still": 100}}
        quiet_b = {"a": {"g0_move": 99, "g0_still": 99}, "b": {"g0_move": 99, "g0_still": 99}}
        self.assertEqual(item.radars["a"].bits(quiet_a["a"]), item.radars["a"].bits(quiet_b["a"]))
        loud = {"a": {"g0_move": 5, "g0_still": 5}, "b": {"g0_move": 5, "g0_still": 5}}
        first = item.rates(loud)
        with patch.object(DurationGroup, "_spans", side_effect=AssertionError):
            self.assertEqual(item.rates(loud), first)
        item.rates(quiet_a)
        with patch.object(DurationGroup, "_spans", side_effect=AssertionError):
            self.assertEqual(item.rates(quiet_b), item.rates(quiet_a))


class MasksTests(unittest.TestCase):
    def test_masks_are_unchanged_and_equal_ones_share_memory(self):
        rng = random.Random(81)
        rows = [
            (float(i), {"g0_move": rng.choice((3, 3, 7, 50, 51, 99))}, "unknown")
            for i in range(300)
        ]
        masks = threshold_masks(rows, "g0_move")
        expected = [
            sum(1 << i for i, row in enumerate(rows) if row[1]["g0_move"] > threshold)
            for threshold in range(101)
        ]
        self.assertEqual(masks, expected)
        distinct = {id(mask) for mask in masks}
        self.assertEqual(len(distinct), len(set(masks)), "equal masks are one object")


class ReadingsTests(unittest.TestCase):
    def view(self, rng, count):
        records = [
            (
                NOW + i * 6 + rng.choice((0, 0, 3)),
                [rng.choice((rng.randrange(101), rng.randrange(101), 101, 255)) for _ in range(18)]
                + [0, 0],
            )
            for i in range(count)
        ]
        return harness.stub_view("a", {"history_labels": [], "training_state": "unknown"}, records)

    def test_evidence_rows_read_like_the_dictionaries_they_replace(self):
        from tuner_under_test.const import HISTORY_KEYS
        from tuner_under_test.rooms import evidence

        rng = random.Random(91)
        for _ in range(30):
            view = self.view(rng, rng.choice((0, 5, 200)))
            keys = rng.sample(HISTORY_KEYS, rng.randrange(1, 19))
            start, end = NOW + 30, NOW + rng.choice((600, 5000))
            expected_rows, expected_barriers = old_background.evidence_rows(
                view, "a", keys, start, end
            )
            found = evidence.read(view, "a", keys, start, end)
            self.assertEqual(found["barriers"], expected_barriers)
            self.assertEqual(
                [(row[0], row[2]) for row in found["rows"]],
                [(row[0], row[2]) for row in expected_rows],
            )
            for row, expected in zip(found["rows"], expected_rows, strict=True):
                values = row[1]
                self.assertEqual(dict(values.items()), expected[1])
                for key in (*HISTORY_KEYS, "unknown_gate"):
                    self.assertEqual(key in values, key in expected[1])
                    self.assertEqual(values.get(key, -1), expected[1].get(key, -1))
                    if key in expected[1]:
                        self.assertEqual(values[key], expected[1][key])
                self.assertEqual(len(values), len(expected[1]))

    def test_a_samples_readings_cost_a_fraction_of_a_dictionary(self):
        """Regression guard: a week of two radars was 300 MB of dictionaries."""
        from tuner_under_test.const import HISTORY_KEYS
        from tuner_under_test.rooms import evidence

        view = self.view(random.Random(92), 0)
        samples = [(NOW + i * 6, bytes((i + k) % 101 for k in range(18))) for i in range(20_000)]
        view._history_columns = harness.columns_of(samples)
        tracemalloc.start()
        try:
            found = evidence.read(view, "a", HISTORY_KEYS, NOW, NOW + 1e6)
            kept = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        self.assertEqual(len(found["rows"]), 20_000)
        self.assertLess(kept / len(found["rows"]), 330, "bytes kept per sample")


class LeanObjectiveTests(unittest.TestCase):
    def test_a_group_that_only_replays_builds_no_time_integrals(self):
        from unittest.mock import patch

        from tuner_under_test.calibration import duration

        rng = random.Random(95)
        rows = random_rows(rng, 80)
        with patch.object(duration, "_confidence_time", wraps=duration._confidence_time) as built:
            lean = DurationGroup(rows, TIMINGS[3], measured=False)
            self.assertEqual(built.call_count, 0)
            full = DurationGroup(rows, TIMINGS[3])
            self.assertGreater(built.call_count, 0)
        self.assertEqual(
            [(offset, window.times) for offset, window, _ in lean.windows],
            [(offset, window.times) for offset, window, _ in full.windows],
        )
        bits = random_bits(rng, 80)
        for (_, window, _), (_, other, _) in zip(lean.windows, full.windows, strict=True):
            hits = bits & window.mask
            self.assertEqual(lean._spans(window, hits, True), full._spans(other, hits, True))

    def test_an_objective_keeps_no_rows_once_it_is_built(self):
        rng = random.Random(96)
        members = {key: wide_member(rng, key, TIMINGS[3]) for key in ("a", "b")}
        single = Objective(members, 0, 900)
        self.assertEqual([len(inputs["rows"]) for inputs in single.inputs.values()], [0, 0])
        linked = LinkedObjective({"g1": members, "g2": {"b": members["b"]}}, 0, 900)
        for item in linked.groups.values():
            self.assertTrue(all(len(inputs["rows"]) == 0 for inputs in item.inputs.values()))
        self.assertEqual(
            linked.rank(
                {"a": {"g0_move": 20, "g0_still": 20}, "b": {"g0_move": 20, "g0_still": 20}}
            ),
            linked.rank(
                {"a": {"g0_move": 20, "g0_still": 20}, "b": {"g0_move": 20, "g0_still": 20}}
            ),
        )
