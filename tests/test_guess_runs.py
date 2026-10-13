"""A radar's guesses are stored as change runs, not as a code in every 5 s row (P7).

The reference throughout is the per-row layout the runs replaced: a code and a confidence on
every sample, read as cells (midpoint to midpoint, clipped at recording gaps) or as the
sample's own guess. Runs must answer like it, except that a run holds one confidence, in steps
of ``GUESS_CONFIDENCE_STEP``: its first sample's, kept by the samples after it while they stay
within ``GUESS_CONFIDENCE_DELTA`` of it (``assigned`` gives each row the confidence its run
holds, by that rule alone)."""

import random
import sys
import time
import types
import unittest
from copy import deepcopy
from unittest.mock import patch

import test_tuner as harness
from tuner_under_test.calibration.constants import (
    GUESS_CONFIDENCE_DELTA,
    GUESS_CONFIDENCE_STEP,
    MIN_AUTO_CONFIDENCE,
)
from tuner_under_test.history import budget, cleanup, guesses, recording
from tuner_under_test.rooms.intervals import Timeline

source_labels = sys.modules["tuner_under_test.history.source_labels"]
STEP = round(GUESS_CONFIDENCE_STEP * 100)
DELTA = round(GUESS_CONFIDENCE_DELTA * 100)
FLOOR = round(MIN_AUTO_CONFIDENCE * 100)
GAP = guesses.GAP
BASE = 1_700_000_000.0  # Well in the past: cleanup drops samples from the future.


# --- The reference: one (code, percent) per sample, as the rows held them. ---------------


def quantised(percent):
    """The percent a run holds for a first sample of ``percent``: whole steps of ``STEP``,
    never lifted onto the floor from under it."""
    steps = round(percent / STEP)
    return (min(steps, FLOOR // STEP - 1) if percent < FLOOR else steps) * STEP


def assigned(rows):
    """``rows`` ``(time, code, percent)`` with the percent their run holds (0 where there is no
    guess): a guess continues the run of the sample before it, if that is within a gap, has
    the same code and a percent less than ``DELTA`` from the run's; else it starts a run."""
    result, previous, run = [], None, None
    for ts, code, percent in rows:
        if not guesses.from_code(code, percent):
            run = None
            result.append((ts, 0, 0))
        else:
            near = previous is not None and ts - previous <= GAP
            if not (run and near and run[0] == code and abs(percent - run[1]) < DELTA):
                run = (code, quantised(percent))
            result.append((ts, code, run[1]))
        previous = ts
    return result


def reference_cells(rows):
    """``evidence._automatic_cells`` as it was: the cell of each guessed sample."""
    spans = []
    for index, (ts, code, percent) in enumerate(rows):
        before = rows[index - 1][0] if index else ts
        after = rows[index + 1][0] if index + 1 < len(rows) else ts
        start = (before + ts) / 2 if ts - before <= GAP else ts
        end = (ts + after) / 2 if after - ts <= GAP else ts
        label = {1: "present", 2: "not_present"}.get(code)
        confidence = percent / 100
        if label and confidence >= MIN_AUTO_CONFIDENCE and end > start:
            spans.append((start, end, (label, "automatic", confidence)))
    return spans


def joined(spans):
    """Adjacent cells with one label as the single span a run makes of them."""
    result = []
    for start, end, label in spans:
        if result and result[-1][1] == start and result[-1][2] == label:
            result[-1] = (result[-1][0], end, label)
        else:
            result.append((start, end, label))
    return result


def runs_of(rows):
    """The runs of per-row ``(time, code, percent)`` rows."""
    return guesses.from_samples(
        (ts, guesses.from_code(code, percent)) for ts, code, percent in rows
    )


GAPS = (6, 6, 6, 5.5, 6.5, 11.9, 12.1, 13, 30, 3600)


def random_rows(rng, count, start=BASE, gaps=GAPS):
    """Samples with gaps, long stretches of one guess, drifting confidence and unguessed rows."""
    rows, ts, code, percent = [], start, 1, 80
    for _ in range(count):
        ts += rng.choice(gaps)
        roll = rng.random()
        if roll < 0.08:
            code = rng.choice((0, 1, 2))
        if roll < 0.2:
            percent = max(0, min(100, percent + rng.choice((-12, -4, -1, 1, 3, 9))))
        rows.append((ts, code, percent if code else 0))
    return rows


def query_times(rng, rows, extra=300):
    """Times at, around and between samples, and outside the recording."""
    times = [ts for ts, _, _ in rows]
    found = [ts + shift for ts in times for shift in (-3, -0.001, 0, 0.001, 3)]
    found += [(a + b) / 2 for a, b in zip(times, times[1:], strict=False)]
    found += [rng.uniform(times[0] - 50, times[-1] + 50) for _ in range(extra)]
    return found


# --- Building runs. ----------------------------------------------------------------------


class RunBuildingTests(unittest.TestCase):
    def test_a_guess_is_a_state_a_percent_and_a_basis_or_none(self):
        self.assertEqual(guesses.guess("present", 80, guesses.EXTERNAL), ("present", 80, 2))
        self.assertEqual(guesses.guess("not_present", 100), ("not_present", 100, guesses.UNKNOWN))
        self.assertEqual(guesses.guess("present", 250)[1], 100, "clamped to 100 %")
        for state, percent in (("unknown", 90), (None, 90), ("present", 0), ("present", 2)):
            self.assertIsNone(guesses.guess(state, percent), (state, percent))

    def test_a_confidence_becomes_whole_steps_and_never_reaches_the_floor_by_rounding(self):
        for percent, steps in ((3, 1), (73, 15), (74, 15), (99, 20), (100, 20), (80, 16)):
            self.assertEqual(guesses.level(percent), steps, percent)
        for percent in range(0, 101):
            reaches = guesses.level(percent) * STEP >= FLOOR
            self.assertEqual(reaches, percent >= FLOOR, percent)
        self.assertEqual(
            [guesses.level(p) for p in (50, 52, 53, 54, 55, 57, 58)], [10, 10, 10, 10, 11, 11, 12]
        )
        self.assertEqual(FLOOR % STEP, 0, "the floor is a whole number of steps")

    def test_a_guess_just_under_the_floor_never_trains_and_one_on_it_does(self):
        for percent, trains in ((52, False), (54, False), (55, True), (57, True)):
            samples = [(6 * i, guesses.guess("present", percent)) for i in range(4)]
            known = guesses.Guesses(guesses.from_samples(samples))
            self.assertEqual(known.sample_guess(6) is not None, trains, percent)
            self.assertEqual(known.at(7) is not None, trains, percent)
            self.assertEqual(bool(known), trains, percent)

    def test_every_level_reads_back_as_the_percent_it_stands_for(self):
        for level in range(21):
            self.assertEqual(guesses.confidence(level), level * STEP / 100)

    def test_old_row_codes_become_guesses_only_when_valid(self):
        self.assertEqual(guesses.from_code(1, 83), ("present", 83, guesses.UNKNOWN))
        self.assertEqual(guesses.from_code(2, 100), ("not_present", 100, guesses.UNKNOWN))
        for code, percent in ((0, 90), (3, 90), (255, 90), (1, 0), (1, 101), (1, 255), (2, 1)):
            self.assertIsNone(guesses.from_code(code, percent), (code, percent))

    def test_identical_consecutive_guesses_are_one_run_covering_their_cells(self):
        runs = runs_of([(BASE + 6 * i, 1, 80) for i in range(5)])
        # The first sample has no neighbour before it, the last none after it.
        self.assertEqual(runs, [(BASE, BASE + 24, "present", 16, guesses.UNKNOWN)])

    def test_a_change_of_state_or_basis_splits_at_the_midpoint(self):
        present, absent = guesses.guess("present", 80), guesses.guess("not_present", 80)
        for first, second in (
            (present, absent),
            (present, guesses.guess("present", 80, guesses.EXTERNAL)),
        ):
            runs = guesses.from_samples([(0, first), (6, first), (12, second), (18, second)])
            expected = [(0, 9, first[0], 16, first[2]), (9, 18, second[0], 16, second[2])]
            self.assertEqual(runs, expected, (first, second))

    def test_a_confidence_less_than_the_delta_from_the_runs_stays_one_run(self):
        for percents, count in (
            ((81, 82, 80, 78), 1),
            ((80, 89, 71, 80), 1),
            ((80, 90), 2),
            ((80, 70), 2),
            ((80, 89, 90), 2),  # The run keeps its first sample's confidence, not the last's.
            ((80, 75, 70), 2),
        ):
            samples = [(6 * i, guesses.guess("present", p)) for i, p in enumerate(percents)]
            runs = guesses.from_samples(samples)
            self.assertEqual(len(runs), count, percents)
            self.assertEqual(runs[0][3], guesses.level(percents[0]), "the first sample's level")

    def test_a_confidence_flapping_across_a_step_edge_is_one_run(self):
        """Regression: a run ended whenever the quantised level changed, so a confidence
        hovering on the edge between two steps made one run per sample."""
        edge = [72, 73] * 40  # 72.5 is the edge between the steps of 70 % and 75 %.
        samples = [(6 * i, guesses.guess("present", p)) for i, p in enumerate(edge)]
        runs = guesses.from_samples(samples)
        self.assertEqual(runs, [(0, 6 * 79, "present", 14, guesses.UNKNOWN)])
        wide = [58, 62, 57, 63, 60, 56, 64] * 10
        samples = [(6 * i, guesses.guess("not_present", p)) for i, p in enumerate(wide)]
        self.assertEqual(len(guesses.from_samples(samples)), 1)

    def test_a_confidence_across_the_floor_by_less_than_the_delta_stays_one_run_at_its_first_level(
        self,
    ):
        """The floor is applied to a run's first sample, which gives the run its level."""
        under = guesses.from_samples(
            [(6 * i, guesses.guess("present", p)) for i, p in enumerate((54, 58, 56))]
        )
        self.assertEqual([r[3] for r in under], [10])
        self.assertFalse(guesses.Guesses(under))
        over = guesses.from_samples(
            [(6 * i, guesses.guess("present", p)) for i, p in enumerate((56, 54, 55))]
        )
        self.assertEqual([r[3] for r in over], [11])
        self.assertTrue(guesses.Guesses(over))

    def test_a_recording_gap_ends_the_run_at_its_last_sample_and_opens_a_new_one(self):
        guess = guesses.guess("present", 90)
        level = guesses.level(90)
        runs = guesses.from_samples([(0, guess), (6, guess), (6 + GAP + 0.5, guess), (30, guess)])
        self.assertEqual(runs, [(0, 6, "present", level, 0), (18.5, 30, "present", level, 0)])
        self.assertEqual(
            guesses.from_samples([(0, guess), (6, guess), (6 + GAP, guess)]),
            [(0, 6 + GAP, "present", level, 0)],
            "exactly the gap apart is still one stretch",
        )

    def test_an_unguessed_sample_closes_a_run_and_the_next_guess_starts_at_the_midpoint(self):
        guess = guesses.guess("not_present", 70)
        runs = guesses.from_samples([(0, guess), (6, None), (12, guess), (18, None)])
        self.assertEqual(runs, [(0, 3, "not_present", 14, 0), (9, 15, "not_present", 14, 0)])

    def test_an_isolated_sample_is_a_zero_width_run_that_only_it_belongs_to(self):
        guess = guesses.guess("present", 90)
        known = guesses.Guesses(guesses.from_samples([(0, guess), (100, guess)]))
        self.assertEqual(len(known.runs), 2)
        self.assertIsNone(known.at(100), "no time is covered")
        self.assertEqual(known.of_sample(100)[2:], ("present", 18, guesses.UNKNOWN))

    def test_the_open_run_extends_in_place_and_closes_when_the_guess_changes(self):
        runs, guess = [], guesses.guess("present", 90)
        guesses.advance(runs, None, 0, guess)
        guesses.advance(runs, 0, 6, guess)
        self.assertEqual(runs, [[0, 6, "present", 18, guesses.UNKNOWN]])
        guesses.advance(runs, 6, 12, None)
        self.assertEqual(runs, [[0, 9, "present", 18, guesses.UNKNOWN]], "closed at the midpoint")
        guesses.advance(runs, 12, 18, None)
        self.assertEqual(len(runs), 1)

    def test_the_open_run_is_the_last_one_while_it_ends_at_the_newest_sample(self):
        runs = [[0, 9, "present", 18, 0], [9, 30, "present", 18, 1]]
        self.assertEqual(guesses.split_open(runs, 30), ([runs[0]], [runs[1]]))
        self.assertEqual(guesses.split_open(runs, 36), (runs, []), "closed at the midpoint")
        self.assertEqual(guesses.split_open([], 36), ([], []))

    def test_confidences_are_read_in_one_walk_over_the_runs(self):
        runs = [
            (0, 10, "present", 16, 0),
            (10, 20, "not_present", 12, 0),
            (40, 50, "present", 8, 0),
        ]
        known = guesses.Guesses(runs)
        times = [-5, 0, 5, 10, 15, 20, 21, 39, 40, 50, 51, 99]
        walk = list(known.percents(times))
        self.assertEqual(walk, [0, 80, 80, 60, 60, 60, 0, 0, 40, 40, 0, 0])
        for t, percent in zip(times, walk, strict=True):
            run = known.of_sample(t)
            self.assertEqual(percent, run[3] * STEP if run else 0, t)
        self.assertEqual(list(guesses.EMPTY.percents([1, 2])), [0, 0])
        self.assertEqual(list(known.percents([])), [])


# --- Equivalence with per-row codes. -----------------------------------------------------


class EquivalenceTests(unittest.TestCase):
    def test_runs_answer_like_per_row_codes_at_many_times(self):
        """Seeded randomized equivalence of "guess at t": cells, the sample's own guess, and
        the spans of confident guesses, against the per-row layout."""
        rng = random.Random(7001)
        checked = 0
        for size in (0, 1, 2, 3, 20, 400, 1500):
            rows = random_rows(rng, size)
            held = assigned(rows)
            known = guesses.Guesses(runs_of(rows))
            cells = Timeline(reference_cells(held))
            self.assertEqual(known.cells(), joined(cells.spans))
            self.assertEqual(bool(known), bool(cells.spans))
            for t in query_times(rng, rows) if rows else []:
                self.assertEqual(known.at(t), cells.at(t), (size, t))
                checked += 1
            self.assertEqual(list(known.percents(ts for ts, _, _ in rows)), [p for _, _, p in held])
            for ts, code, percent in held:
                run = known.of_sample(ts)
                if code:
                    self.assertEqual((run[2], run[3]), (guesses.STATES[code], percent // STEP))
                else:
                    self.assertIsNone(run)
                floor = percent / 100 >= MIN_AUTO_CONFIDENCE and code
                expected = (guesses.STATES[code], "automatic", percent / 100) if floor else None
                self.assertEqual(known.sample_guess(ts), expected)
        self.assertGreater(checked, 10000)

    def test_runs_are_far_fewer_than_samples_and_never_overlap(self):
        rng = random.Random(7002)
        rows = random_rows(rng, 3000)
        runs = runs_of(rows)
        self.assertLess(len(runs) * 2, len([r for r in rows if r[1]]))
        self.assertEqual(runs, sorted(runs))
        for first, second in zip(runs, runs[1:], strict=False):
            self.assertLessEqual(first[1], second[0])

    def test_stored_chunks_give_back_the_runs_exactly(self):
        rng = random.Random(7003)
        for size in (0, 1, 5, guesses.CHUNK_RUNS, guesses.CHUNK_RUNS + 1, 4000):
            runs = runs_of(random_rows(rng, size))
            chunks = guesses.encode(runs)
            self.assertEqual(len(chunks), -(-len(runs) // guesses.CHUNK_RUNS))
            self.assertEqual([r for chunk in chunks for r in guesses.decode(chunk)], runs)
            for chunk in chunks:
                self.assertEqual(chunk["version"], guesses.VERSION)
                self.assertEqual(
                    (chunk["start"], chunk["end"]),
                    (guesses.decode(chunk)[0][0], max(r[1] for r in guesses.decode(chunk))),
                )

    def test_unreadable_chunks_are_empty_not_errors(self):
        good = guesses.encode(runs_of([(BASE + 6 * i, 1, 80) for i in range(3)]))[0]
        for bad in (
            {},
            None,
            "junk",
            {**good, "data": "not base64!"},
            {**good, "data": good["data"][:-8]},
            {**good, "count": 99},
            {**good, "count": "x"},
            {**good, "version": 2},
        ):
            self.assertEqual(guesses.decode(bad), (), bad)
        self.assertEqual(len(guesses.decode(good)), 1)

    def test_state_code_zero_or_a_zero_level_in_a_chunk_is_no_run(self):
        raw = guesses.RUN.pack(0.0, 5.0, 0, 10, 0) + guesses.RUN.pack(5.0, 9.0, 1, 0, 0)
        raw += guesses.RUN.pack(9.0, 12.0, 2, 4, 1)
        chunk = {
            "version": 1,
            "start": 0.0,
            "end": 12.0,
            "count": 3,
            "data": cleanup.encode_payload(raw, guesses.RUN.size),
        }
        self.assertEqual(guesses.decode(chunk), ((9.0, 12.0, "not_present", 4, 1),))


class StoreTests(unittest.TestCase):
    def runs(self, start, count, level=16):
        return [(start + 10 * i, start + 10 * i + 6, "present", level, 1) for i in range(count)]

    def test_upsert_replaces_a_run_with_the_same_start_and_keeps_the_rest_sorted(self):
        chunks = guesses.encode(self.runs(0, 600))
        updated = guesses.upsert(
            chunks, [(10, 99, "not_present", 3, 2), (5001, 5003, "present", 9, 1)]
        )
        merged = [r for chunk in updated for r in guesses.decode(chunk)]
        expected = {r[0]: r for r in self.runs(0, 600)}
        expected[10] = (10, 99, "not_present", 3, 2)
        expected[5001] = (5001, 5003, "present", 9, 1)
        self.assertEqual(merged, [expected[k] for k in sorted(expected)])

    def test_upsert_re_codes_only_the_chunks_the_runs_reach(self):
        chunks = guesses.encode(self.runs(0, 1000))
        tail = [(10_000, 10_005, "present", 16, 1)]
        updated = guesses.upsert(chunks, tail)
        self.assertEqual(updated[:-1], chunks[:-1])
        for old, new in zip(chunks[:-1], updated, strict=False):
            self.assertIs(old, new, "untouched chunks are shared, not copied")
        self.assertEqual(guesses.upsert(chunks, []), chunks)
        self.assertEqual(guesses.upsert([], tail), guesses.encode(tail))

    def test_upsert_in_the_middle_leaves_both_sides_alone(self):
        chunks = guesses.encode(self.runs(0, 1500))  # Six chunks, 2560 s apart.
        updated = guesses.upsert(chunks, [(7505, 7509, "present", 5, 1)])
        self.assertEqual(updated[:2], chunks[:2])
        self.assertEqual(updated[-3:], chunks[-3:])
        self.assertIs(updated[0], chunks[0])
        self.assertIs(updated[-1], chunks[-1])
        self.assertIsNot(updated[2], chunks[2])
        merged = [r for chunk in updated for r in guesses.decode(chunk)]
        self.assertEqual(len(merged), 1501)
        self.assertEqual(merged, sorted(merged))

    def test_join_extends_back_over_the_run_it_continues_unless_that_run_is_open(self):
        known = guesses.Guesses([(0, 10, "present", 16, 2)])
        new = [(10, 20, "present", 16, 2), (30, 40, "present", 16, 2)]
        self.assertEqual(guesses.join(known, [], new), [(0, 20, "present", 16, 2), new[1]])
        self.assertEqual(guesses.join(known, [[0, 10]], new), new, "an open run keeps its start")
        self.assertEqual(
            guesses.join(known, [], [(10, 20, "present", 17, 2)]), [(10, 20, "present", 17, 2)]
        )
        self.assertEqual(
            guesses.join(known, [], [(11, 20, "present", 16, 2)]), [(11, 20, "present", 16, 2)]
        )
        self.assertEqual(guesses.join(known, [], []), [])

    def test_kept_runs_are_those_with_a_retained_row_inside(self):
        runs = [(0, 10, "present", 1, 0), (20, 30, "present", 1, 0), (40, 40, "present", 1, 0)]
        self.assertEqual(guesses.kept(runs, [5, 40]), [runs[0], runs[2]])
        self.assertEqual(guesses.kept(runs, [10, 20]), runs[:2], "both ends are inside")
        self.assertEqual(guesses.kept(runs, [11, 19, 31]), [])
        self.assertEqual(guesses.kept(runs, []), [])

    def test_lookups_find_runs_by_bisect(self):
        runs = [
            (0, 10, "present", 16, 0),
            (10, 20, "not_present", 12, 0),
            (40, 50, "present", 8, 0),
        ]
        known = guesses.Guesses(runs)
        self.assertEqual(known.between(5, 12), runs[:2])
        self.assertEqual(known.between(20, 40), [], "half-open at both ends")
        self.assertEqual(known.between(-5, 0), [])
        self.assertEqual(known.between(49, 100), runs[2:])
        self.assertEqual(known.ending_at(20), runs[1])
        self.assertIsNone(known.ending_at(21))
        self.assertIsNone(known.ending_at(99))
        self.assertEqual(known.at(10), ("not_present", "automatic", 0.6))
        self.assertIsNone(known.at(45), "0.4 is under the floor")
        self.assertIsNone(known.at(20))
        self.assertEqual(known.of_sample(20)[2:4], ("not_present", 12), "closed at both ends")
        self.assertEqual(
            known.cells(),
            [
                (0, 10, ("present", "automatic", 0.8)),
                (10, 20, ("not_present", "automatic", 0.6)),
            ],
        )
        self.assertEqual(known.cells(15, 45), [(10, 20, ("not_present", "automatic", 0.6))])

    def test_a_runs_label_is_built_once_and_shared_by_runs_of_one_state_and_level(self):
        runs = [
            (0, 10, "present", 16, 0),
            (10, 20, "present", 16, 1),
            (20, 30, "not_present", 10, 0),
            (30, 40, "present", 11, 0),
        ]
        known = guesses.Guesses(runs)
        self.assertIsNone(known._labels, "built on first use")
        labels = known.labels()
        self.assertEqual(
            labels,
            [
                ("present", "automatic", 0.8),
                ("present", "automatic", 0.8),
                None,
                ("present", "automatic", 0.55),
            ],
        )
        self.assertIs(labels[0], labels[1])
        self.assertIs(known.labels(), labels)
        self.assertIs(known.at(5), labels[0])
        self.assertIsNone(known.at(25), "under the floor")
        self.assertEqual(known.at(25, "none"), "none")
        self.assertIs(known.sample_guess(10), labels[0], "the run a sample starts at")
        self.assertIsNone(known.sample_guess(40.5))

    def test_a_recent_read_decodes_only_the_recent_chunks(self):
        """Regression guard: the room estimate (every poll) must not decode 30 days of runs."""
        chunks = guesses.encode(self.runs(0, 2000))  # Eight chunks.
        runtime = types.SimpleNamespace(
            data={"devices": {"a": {"guess_runs": chunks}}}, _history_runtime={}
        )
        guesses._decoded.cache_clear()
        recent = guesses.of(runtime, "a", 19_000)
        self.assertEqual(guesses._decoded.cache_info().misses, 1)
        self.assertEqual(recent.runs, [r for r in self.runs(0, 2000) if r[1] >= 19_000])

    def test_a_chunk_without_an_end_is_skipped_by_a_recent_read(self):
        runtime = types.SimpleNamespace(
            data={"devices": {"a": {"guess_runs": [{"version": 1}, None]}}}, _history_runtime={}
        )
        self.assertEqual(guesses.of(runtime, "a", 0).runs, [])

    def test_copies_of_the_data_share_the_stored_chunks(self):
        chunks = guesses.encode(self.runs(0, 300))
        data = {"devices": {"a": {"guess_runs": chunks, "history": []}}}
        copy = budget.copy_data(data)
        self.assertEqual(copy, data)
        self.assertIsNot(copy["devices"]["a"]["guess_runs"], chunks)
        for old, new in zip(chunks, copy["devices"]["a"]["guess_runs"], strict=True):
            self.assertIs(old, new)

    def test_guesses_compare_by_their_runs(self):
        runs = [(0, 10, "present", 16, 0)]
        self.assertEqual(guesses.Guesses(runs), guesses.Guesses(list(runs)))
        self.assertNotEqual(guesses.Guesses(runs), guesses.Guesses())
        self.assertNotEqual(guesses.Guesses(runs), runs)
        self.assertFalse(guesses.EMPTY)
        self.assertFalse(guesses.Guesses([(0, 10, "present", 8, 0)]), "under the floor")


# --- Recording and storing. --------------------------------------------------------------


class RecordingTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def classify(self, state, confidence, basis="adaptive-guided", at=None, **extra):
        self.device.setdefault("auto", {})["last_classification"] = {
            "state": state,
            "confidence": confidence,
            "timestamp": at,
            "basis": basis,
            **extra,
        }

    def record(self, offset, state="present", confidence=0.9, **extra):
        when = self.now + offset
        self.classify(state, confidence, at=when, **extra)
        self.runtime._record_history_sample("a", {"g0_move": 20}, when)

    def stored(self):
        return [run for chunk in self.device.get("guess_runs", []) for run in guesses.decode(chunk)]

    def test_rows_hold_energies_only_and_a_steady_guess_is_one_run(self):
        for i in range(130):
            self.record(i * 6)
        self.runtime._flush_history_block("a")
        for block in self.device["history"]:
            self.assertEqual(block["version"], 4)
        rows = list(self.runtime._iter_history_samples(self.device))
        self.assertEqual({len(row) for _, row in rows}, {18})
        self.assertEqual(len(rows) + len(self.runtime._history_runtime["a"]["samples"]), 130)
        self.assertEqual(
            guesses.of(self.runtime, "a").runs,
            [(self.now, self.now + 129 * 6, "present", 18, guesses.REFERENCE)],
        )

    def test_a_flush_stores_closed_runs_only_and_never_re_codes_the_open_one(self):
        for i in range(125):
            self.record(i * 6)
        self.assertEqual(self.stored(), [], "the open run waits in memory")
        self.assertEqual(self.runtime._history_runtime["a"]["runs"][0][1], self.now + 124 * 6)
        before = list(self.device.get("guess_runs", []))
        self.record(125 * 6, "not_present", 0.9)  # Closes the run of present.
        for i in range(126, 190):
            self.record(i * 6, "not_present", 0.9)
        self.assertEqual(
            self.stored(), [(self.now, self.now + 125 * 6 - 3, "present", 18, guesses.REFERENCE)]
        )
        self.assertNotEqual(self.device["guess_runs"], before)
        stored = list(self.device["guess_runs"])
        for i in range(190, 260):  # More flushes while one run stays open.
            self.record(i * 6, "not_present", 0.9)
        self.assertEqual(self.device["guess_runs"], stored, "no chunk is re-coded or replaced")
        self.assertEqual(len(guesses.of(self.runtime, "a").runs), 2)
        self.assertEqual(
            self.runtime._history_runtime["a"]["runs"],
            [[self.now + 125 * 6 - 3, self.now + 259 * 6, "not_present", 18, 1]],
        )

    def test_no_run_is_stored_twice_across_flushes(self):
        for i in range(400):
            self.record(i * 6, "present" if i % 70 < 35 else "not_present", 0.9)
        self.runtime._flush_history_block("a")
        stored = self.stored()
        self.assertEqual(len({run[0] for run in stored}), len(stored))
        merged = guesses.of(self.runtime, "a").runs
        self.assertEqual(len(merged), len({run[0] for run in merged}))
        self.assertEqual(len(merged), 400 // 35 + 1 if 400 % 35 else 400 // 35)

    async def test_a_save_includes_the_open_run_but_the_live_chunks_do_not(self):
        for i in range(70):
            self.record(i * 6)
        live = self.device.get("guess_runs", [])
        await self.runtime.async_save()
        saved = self.runtime.store.async_save.call_args.args[0]["devices"]["a"]["guess_runs"]
        runs = [run for chunk in saved for run in guesses.decode(chunk)]
        self.assertEqual(runs, [(self.now, self.now + 69 * 6, "present", 18, guesses.REFERENCE)])
        self.assertEqual(self.device.get("guess_runs", []), live)
        self.assertEqual(self.stored(), [], "nothing was stored in the live data")
        await self.runtime.async_save()  # Again: still one run, the open one as it is now.
        again = self.runtime.store.async_save.call_args.args[0]["devices"]["a"]["guess_runs"]
        self.assertEqual(len([r for c in again for r in guesses.decode(c)]), 1)

    async def test_a_restart_keeps_what_the_last_save_held_of_the_open_run(self):
        for i in range(70):
            self.record(i * 6)
        await self.runtime.flush_history()
        await self.runtime.async_save()
        saved = self.runtime.store.async_save.call_args.args[0]
        restarted = harness.mod.TunerRuntime(None, None, saved)
        self.assertEqual(
            guesses.of(restarted, "a").runs,
            [(self.now, self.now + 69 * 6, "present", 18, guesses.REFERENCE)],
        )

    def test_a_confidence_flapping_across_a_step_edge_records_one_run(self):
        for i in range(80):
            self.record(i * 6, confidence=0.72 if i % 2 else 0.73)
        self.assertEqual(len(guesses.of(self.runtime, "a").runs), 1)

    def test_the_floor_applies_to_the_raw_confidence_not_the_rounded_one(self):
        for i, confidence in enumerate((0.53, 0.53, 0.54, 0.549)):
            self.record(i * 6, confidence=confidence)
        self.record(100, confidence=0.55)
        self.record(106, confidence=0.55)
        low, floor = guesses.of(self.runtime, "a").runs
        self.assertEqual((low[3], floor[3]), (10, 11))
        known = guesses.of(self.runtime, "a")
        self.assertIsNone(known.sample_guess(self.now + 6), "0.53 does not train")
        self.assertIsNone(known.sample_guess(self.now + 18), "0.549 does not train")
        self.assertEqual(known.sample_guess(self.now + 100), ("present", "automatic", 0.55))

    def test_a_change_of_guess_basis_or_a_gap_starts_a_new_run(self):
        self.record(0)
        self.record(6)
        self.record(12, confidence=0.5)
        self.record(18, confidence=0.5, basis="external")
        self.record(24, "not_present", 0.5, basis="external")
        self.record(30, "unknown", 0.0)
        self.record(36, "not_present", 0.5, basis="external")
        self.record(36 + GAP + 6, "not_present", 0.5, basis="external")
        runs = guesses.of(self.runtime, "a").runs
        self.assertEqual(
            [r[2:] for r in runs],
            [
                ("present", 18, 1),
                ("present", 10, 1),
                ("present", 10, 2),
                ("not_present", 10, 2),
                ("not_present", 10, 2),
                ("not_present", 10, 2),
            ],
        )
        self.assertEqual(runs[-2][1], self.now + 36)  # The gap clips the run at its sample.

    def test_buffered_external_presence_and_stale_classifications_record_no_guess(self):
        self.record(0, "present", 0.9, buffered=True)
        self.classify("present", 0.9, at=self.now - 100)
        self.runtime._record_history_sample("a", {"g0_move": 20}, self.now + 6)
        self.record(12, "unknown", 0.0)
        self.assertEqual(guesses.of(self.runtime, "a").runs, [])
        self.assertNotIn("observations", self.device["auto"])

    def test_observations_count_each_guessed_sample(self):
        for i in range(4):
            self.record(i * 6)
        self.record(24, "not_present", 0.9)
        self.assertEqual(self.device["auto"]["observations"], {"present": 4, "not_present": 1})

    async def test_pausing_recording_and_restarting_do_not_continue_the_run(self):
        self.record(0)
        self.record(6)
        self.runtime.set_recording("a", False)
        self.runtime.set_recording("a", True)
        self.record(12)
        self.record(18)
        await self.runtime.flush_history()
        await self.runtime.async_save()
        saved = self.runtime.store.async_save.call_args.args[0]["devices"]["a"]
        self.device["guess_runs"] = saved["guess_runs"]
        self.runtime._history_runtime.clear()  # A restart: only what was saved is remembered.
        self.record(24)
        runs = guesses.of(self.runtime, "a").runs
        self.assertEqual(
            len(runs), 2, "the restart opens a run; the pause did not split a near gap"
        )
        self.assertEqual(runs[0][:2], (self.now, self.now + 18))
        self.assertEqual(runs[1][:2], (self.now + 24, self.now + 24))

    def test_live_recording_builds_the_runs_of_the_same_samples_in_one_batch(self):
        """Equivalence: runs built sample by sample while recording, with flushes in between,
        equal ``from_samples`` over all of them."""
        rng = random.Random(7004)
        expected = []
        ts = self.now - 3600
        for _ in range(500):
            ts += rng.choice((5, 5, 6, 7, 20))
            state = rng.choice(("present", "present", "not_present", "unknown"))
            confidence = rng.choice((0.2, 0.55, 0.8, 0.83, 0.9, 1.0))
            self.classify(state, confidence, at=ts)
            self.runtime._record_history_sample("a", {"g0_move": 20}, ts)
            percent = round(confidence * 100)
            expected.append((ts, guesses.guess(state, percent, guesses.REFERENCE)))
        self.runtime._flush_history_block("a")
        self.assertEqual(guesses.of(self.runtime, "a").runs, guesses.from_samples(expected))

    def test_flush_drops_chunks_that_have_aged_out_with_their_blocks(self):
        old = guesses.encode(
            [(self.now - 40 * 86400, self.now - 40 * 86400 + 60, "present", 16, 1)]
        )
        recent = guesses.encode([(self.now - 3600, self.now - 3000, "present", 16, 1)])
        self.device["guess_runs"] = [*old, *recent]
        self.record(0)
        self.record(6, "not_present")  # Closes the run at 0; the one at 6 stays open.
        self.runtime._flush_history_block("a")
        starts = [run[0] for run in self.stored()]
        self.assertEqual(starts, [self.now - 3600, self.now])

    def test_clearing_a_radar_forgets_its_runs(self):
        for i in range(70):
            self.record(i * 6)
        self.record(70 * 6, "not_present")  # Closes the run of present.
        self.runtime._flush_history_block("a")
        self.assertTrue(self.device["guess_runs"])
        self.runtime.clear_samples("a")
        self.assertNotIn("guess_runs", self.device)
        self.assertEqual(guesses.of(self.runtime, "a").runs, [])

    def test_views_carry_the_stored_chunks_and_a_frozen_copy_of_the_open_run(self):
        for i in range(130):  # The first run closes and is stored; the second stays open.
            self.record(i * 6, "present" if i < 70 else "not_present")
        self.assertTrue(self.device["guess_runs"])
        view = self.runtime._history_view("a")
        self.record(800, "not_present")
        self.assertEqual(view.data["devices"]["a"]["guess_runs"], self.device["guess_runs"])
        before = guesses.of(view, "a").runs
        self.assertEqual(len(before), 2)
        self.assertEqual(before[1][1], self.now + 129 * 6, "the view ends where the copy was taken")
        self.record(806, "not_present")
        self.assertEqual(guesses.of(view, "a").runs, before, "recording does not reach the view")

    def test_newest_sample_is_the_newest_stored_or_pending_one(self):
        self.assertIsNone(recording.newest_sample(self.runtime, "a"))
        for i in range(61):
            self.record(i * 6)
        self.assertEqual(recording.newest_sample(self.runtime, "a"), self.now + 360)
        self.runtime._flush_history_block("a")
        self.assertEqual(recording.newest_sample(self.runtime, "a"), self.now + 360)
        self.assertIsNone(recording.newest_sample(self.runtime, "missing"))
        self.device["history"].append({"start": float("nan"), "end": float("nan"), "count": 1})
        self.assertEqual(recording.newest_sample(self.runtime, "a"), self.now + 360)

    def test_storage_counts_the_runs_exactly(self):
        import json

        for i in range(130):
            self.record(i * 6, "present" if i % 40 < 20 else "not_present")
        self.runtime._flush_history_block("a")
        data = deepcopy(self.runtime.data)
        envelope = {
            "version": 2,
            "minor_version": harness.constants.STORAGE_MINOR_VERSION,
            "key": "ld2410_tuner.data",
            "data": data,
        }
        self.assertEqual(budget.storage_bytes(data), len(json.dumps(envelope, indent=2).encode()))
        self.assertTrue(data["devices"]["a"]["guess_runs"])


# --- Confirming buffered external presence. ----------------------------------------------


class ConfirmEquivalenceTests(unittest.TestCase):
    """``confirm_presence`` marks the samples in ``[start, end)`` the radar left unguessed,
    exactly as it did on the per-row codes."""

    def test_random_confirmations_match_marking_the_rows(self):
        rng = random.Random(7005)
        for case in range(25):
            rows = random_rows(rng, rng.randrange(5, 120), start=BASE + 1000 * case)
            runtime = harness.mod.TunerRuntime(None, None, {"devices": {"a": {}}})
            runtime._schedule_save = lambda: None
            row = bytes([40] * 18)
            runtime._history_runtime["a"] = {
                "samples": [(ts, row) for ts, _, _ in rows],
                "runs": [list(r) for r in runs_of(rows)],
            }
            reference = assigned(rows)
            for _ in range(3):  # Progressive confirmations, like the live buffer's.
                start = rng.uniform(rows[0][0] - 20, rows[-1][0])
                end = start + rng.choice((10, 60, 400))
                percent = rng.choice((50, 77, 90, 100))
                with patch.object(time, "time", return_value=rows[-1][0] + 100):
                    source_labels.confirm_presence(runtime, "a", start, end, percent)
                reference = [
                    (ts, 1, quantised(percent)) if start <= ts < end and not code else (ts, code, p)
                    for ts, code, p in reference
                ]
            reference = [(ts, code, p) for ts, code, p in reference]
            known = guesses.of(runtime, "a")
            cells = Timeline(reference_cells(reference))
            for t in query_times(rng, rows, extra=100):
                self.assertEqual(known.at(t), cells.at(t), (case, t))
            for ts, code, percent in reference:
                run = known.of_sample(ts)
                self.assertEqual(
                    None if run is None else (run[2], run[3] * STEP),
                    (guesses.STATES[code], percent) if code and percent else None,
                )

    def test_progressive_confirmation_keeps_one_growing_run(self):
        runtime = harness.mod.TunerRuntime(None, None, {"devices": {"a": {}}})
        runtime._schedule_save = lambda: None
        row = bytes([40] * 18)
        runtime._history_runtime["a"] = {
            "samples": [(BASE + 6 * i, row) for i in range(200)],
            "runs": [],
        }
        with patch.object(time, "time", return_value=BASE + 1300):
            for end in range(6, 1200, 6):
                source_labels.confirm_presence(runtime, "a", BASE, BASE + end, 90)
        runs = guesses.of(runtime, "a").runs
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][2:], ("present", 18, guesses.EXTERNAL))
        self.assertEqual(len(runtime.data["devices"]["a"]["guess_runs"]), 1)


# --- The learner reads snippets of stretches, bounded, not every sample. ----------------------


class LearningEvidenceTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    evidence = sys.modules["tuner_under_test.calibration.evidence"]
    constants = sys.modules["tuner_under_test.calibration.constants"]

    def history(self, samples, labels=()):
        self.device["history_labels"] = list(labels)
        energies, runs = harness.split_guesses(samples)
        self.device["history"] = cleanup._encode(energies)
        self.device["guess_runs"] = guesses.encode(runs)
        return self.evidence.collect_samples(self.runtime, "a", ["g0_move"])

    def sample(self, ts, code, value=30, percent=100):
        """Present counts only from a fresh reference (``TRUST_PRESENT``), so by default full."""
        return ts, bytes([value] + [255] * 17) + bytes((code, percent))

    def stood_for(self, automatic):
        """The samples the automatic evidence stands for, per label."""
        found = {}
        for item in automatic:
            found[item[2]] = found.get(item[2], 0) + item[4]
        return found

    def test_a_history_that_fits_the_cap_is_read_in_full_with_one_sample_each(self):
        samples = [self.sample(self.now - 86400 + 6 * i, 1 if i < 3000 else 2) for i in range(6000)]
        _, automatic = self.history(samples)
        counts = {
            label: sum(1 for item in automatic if item[2] == label)
            for label in ("present", "not_present")
        }
        self.assertEqual(counts, {"present": 3000, "not_present": 3000})
        trusted = {"present": self.constants.TRUST_PRESENT[-1][1]}
        trusted["not_present"] = self.constants.TRUST_NOT_PRESENT
        self.assertTrue(all(item[3] == trusted[item[2]] and item[4] == 1 for item in automatic))

    def test_a_long_history_is_read_in_snippets_that_stand_for_every_sample(self):
        """Regression: expanding every guess of seven days made 10-20 times more evidence than
        the newest 5000 samples did; the whole history is read, bounded, weighted by duration."""
        count = 40000
        samples = [
            self.sample(self.now - 6 * (count - i), 1 if i < count // 2 else 2)
            for i in range(count)
        ]
        _, automatic = self.history(samples)
        cap = self.constants.MAX_CLASS_SAMPLES
        per_label = {
            label: [item for item in automatic if item[2] == label]
            for label in ("present", "not_present")
        }
        for label, items in per_label.items():
            self.assertLessEqual(len(items), cap, label)
            self.assertLess(len(items), count // 4)
            self.assertGreater(len(items), cap // 2)
        stood = self.stood_for(automatic)
        for label in per_label:
            self.assertAlmostEqual(stood[label], count // 2, delta=count // 20, msg=label)
        times = sorted(item[0] for item in per_label["not_present"])
        lengths, length = [], 1
        for before, after in zip(times, times[1:], strict=False):
            if after - before < 6.5:
                length += 1
            else:
                lengths.append(length)
                length = 1
        self.assertEqual(
            set(lengths[1:]), {self.constants.AUTO_SNIPPET_ROWS}, "snippets of consecutive samples"
        )

    def test_a_stretch_counts_for_its_duration_and_a_recording_gap_for_nothing(self):
        samples = [self.sample(self.now - 100000 + 6 * i, 1) for i in range(250)]
        samples += [self.sample(self.now - 50000 + 6 * i, 1) for i in range(130)]
        _, automatic = self.history(samples)
        self.assertEqual(len(automatic), 380, "short histories are read in full")
        with (
            patch.object(self.evidence, "_period", return_value=100),
            patch.object(self.constants, "AUTO_SNIPPET_ROWS", 10),
            patch.object(self.evidence, "AUTO_SNIPPET_ROWS", 10),
        ):
            automatic = self.evidence.collect_samples(self.runtime, "a", ["g0_move"])[1]
        self.assertEqual(len(automatic), 50, "10 of every 100 samples of each stretch")
        first = sum(item[4] for item in automatic if item[0] < self.now - 70000)
        second = sum(item[4] for item in automatic if item[0] >= self.now - 70000)
        self.assertAlmostEqual(first, 250)
        self.assertAlmostEqual(second, 130, msg="each stretch stands for its own samples")

    def test_the_newest_samples_of_each_class_are_kept_when_over_the_cap(self):
        samples = [self.sample(self.now - 6 * (400 - i), 1) for i in range(400)]
        with (
            patch.object(self.evidence, "MAX_CLASS_SAMPLES", 50),
            patch.object(self.evidence, "_period", return_value=10),
        ):
            _, automatic = self.history(samples)
        self.assertEqual(sorted(item[0] for item in automatic), [s[0] for s in samples[-50:]])

    def test_a_change_of_label_ends_a_stretch(self):
        samples = [
            self.sample(self.now - 6 * (200 - i), 1 if i % 100 < 50 else 2) for i in range(200)
        ]
        with patch.object(self.evidence, "_period", return_value=20):
            _, automatic = self.history(samples)
        for label, stood in self.stood_for(automatic).items():
            self.assertAlmostEqual(stood, 100, msg=label)

    def test_human_samples_keep_their_count_cap_and_all_their_age(self):
        old = self.now - 10 * 86400
        samples = [self.sample(old + 6 * i, 0) for i in range(5200)]
        samples += [self.sample(self.now - 6 * 5200 + 6 * i, 0, value=3) for i in range(5200)]
        labels = [
            {"start": old - 1, "end": old + 6 * 5200, "state": "present"},
            {"start": self.now - 6 * 5200 - 1, "end": self.now + 1, "state": "not_present"},
        ]
        rows, automatic = self.history(samples, labels)
        counts = {
            label: sum(1 for item in rows if item[2] == label)
            for label in ("present", "not_present")
        }
        self.assertEqual(counts, {"present": 5000, "not_present": 5000})
        self.assertEqual(automatic, [])

    def test_a_radar_with_no_history_has_no_evidence(self):
        self.assertEqual(self.evidence.collect_samples(self.runtime, "a", ["g0_move"]), ([], []))


class AutomaticMassTests(unittest.TestCase):
    """An automatic sample counts its confidence times the samples it stands for."""

    scoring = sys.modules["tuner_under_test.calibration.scoring"]
    duration = sys.modules["tuner_under_test.calibration.duration"]

    def test_mass_of_human_and_automatic_observations(self):
        mass = self.scoring.mass
        self.assertEqual(mass((0, {}, "present")), 1.0)
        self.assertEqual(mass((0, {}, "present", 0.8)), 0.8)
        self.assertAlmostEqual(mass((0, {}, "present", 0.8, 4.0)), 3.2)

    def test_a_weighted_sample_covers_as_much_time_as_the_samples_it_stands_for(self):
        def seconds(weight):
            rows = [(6.0 * i, {"g0_move": 5}, "present", 0.5, weight) for i in range(20)]
            group = self.duration.DurationGroup(rows)
            return group._measure(0)["weighted_seconds"]

        self.assertAlmostEqual(seconds(4.0), 4 * seconds(1.0))
        self.assertGreater(seconds(1.0), 0)

    def test_the_automatic_share_follows_the_mass_not_the_number_of_samples(self):
        search = sys.modules["tuner_under_test.calibration.search"]
        human = [(6.0 * i, {"g0_move": 50}, "present") for i in range(100)]
        quiet = [(6.0 * i, {"g0_move": 5}, "not_present") for i in range(100)]

        def share(weight):
            auto = {
                "present": [(6.0 * i, {"g0_move": 50}, "present", 1.0, weight) for i in range(10)],
                "not_present": [],
            }
            return search._ThresholdSearch(human, quiet, auto, ["g0_move"], {}, None, 1.0).shares

        self.assertAlmostEqual(share(1.0)[0], 0.25 * 10 / 100)
        self.assertAlmostEqual(share(5.0)[0], 0.25 * 50 / 100)
