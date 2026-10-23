"""Native guess runs: held readings, ingestion, queries and learner evidence."""

import random
import sys
import tempfile
import time
import unittest
from math import ceil
from pathlib import Path
from unittest.mock import patch

import test_tuner as harness
from codex_history import objective, sample_guesses
from tuner_under_test.calibration.constants import (
    GUESS_CONFIDENCE_DELTA,
    GUESS_CONFIDENCE_STEP,
    MIN_AUTO_CONFIDENCE,
)
from tuner_under_test.history import guesses, recording, spans
from tuner_under_test.history.convert import _from_code
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
        if not _from_code(code, percent):
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
    """Independent held-reading oracle: gap endings and terminal readings last 3 seconds."""
    result = []
    for index, (timestamp, code, percent) in enumerate(rows):
        after = rows[index + 1][0] if index + 1 < len(rows) else timestamp + 3
        end = after if after - timestamp <= GAP else timestamp + 3
        state = {1: "present", 2: "not_present"}.get(code)
        if state and percent / 100 >= MIN_AUTO_CONFIDENCE:
            result.append((timestamp, end, (state, "automatic", percent / 100)))
    return result


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
    return sample_guesses((ts, _from_code(code, percent)) for ts, code, percent in rows)


GAPS = (6, 6, 6, 5.5, 6.5, 11.9, 12.1, 13, 30, 3600)


def random_rows(rng, count, start=BASE, gaps=GAPS):
    """Samples with gaps, long stretches of one guess, drifting confidence and unguessed rows."""
    rows, ts, code, percent = [], start, 1, 80
    for _ in range(count):
        ts = round(ts + rng.choice(gaps), 3)
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
            known = guesses.Guesses(sample_guesses(samples))
            self.assertEqual(known.sample_guess(6) is not None, trains, percent)
            self.assertEqual(known.at(7) is not None, trains, percent)
            self.assertEqual(bool(known), trains, percent)

    def test_every_level_reads_back_as_the_percent_it_stands_for(self):
        for level in range(21):
            self.assertEqual(guesses.confidence(level), level * STEP / 100)

    def test_old_row_codes_become_guesses_only_when_valid(self):
        self.assertEqual(_from_code(1, 83), ("present", 83, guesses.UNKNOWN))
        self.assertEqual(_from_code(2, 100), ("not_present", 100, guesses.UNKNOWN))
        for code, percent in ((0, 90), (3, 90), (255, 90), (1, 0), (1, 101), (1, 255), (2, 1)):
            self.assertIsNone(_from_code(code, percent), (code, percent))

    def test_identical_consecutive_guesses_are_one_run_covering_their_cells(self):
        runs = runs_of([(BASE + 6 * i, 1, 80) for i in range(5)])
        # The last reading holds for its terminal half sample.
        self.assertEqual(runs, [(BASE, BASE + 27, "present", 16, guesses.UNKNOWN)])

    def test_a_change_of_state_or_basis_splits_at_the_delivered_reading(self):
        present, absent = guesses.guess("present", 80), guesses.guess("not_present", 80)
        for first, second in (
            (present, absent),
            (present, guesses.guess("present", 80, guesses.EXTERNAL)),
        ):
            runs = sample_guesses([(0, first), (6, first), (12, second), (18, second)])
            expected = [(0, 12, first[0], 16, first[2]), (12, 21, second[0], 16, second[2])]
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
            runs = sample_guesses(samples)
            self.assertEqual(len(runs), count, percents)
            self.assertEqual(runs[0][3], guesses.level(percents[0]), "the first sample's level")

    def test_a_confidence_flapping_across_a_step_edge_is_one_run(self):
        """Regression: a run ended whenever the quantised level changed, so a confidence
        hovering on the edge between two steps made one run per sample."""
        edge = [72, 73] * 40  # 72.5 is the edge between the steps of 70 % and 75 %.
        samples = [(6 * i, guesses.guess("present", p)) for i, p in enumerate(edge)]
        runs = sample_guesses(samples)
        self.assertEqual(runs, [(0, 6 * 79 + 3, "present", 14, guesses.UNKNOWN)])
        wide = [58, 62, 57, 63, 60, 56, 64] * 10
        samples = [(6 * i, guesses.guess("not_present", p)) for i, p in enumerate(wide)]
        self.assertEqual(len(sample_guesses(samples)), 1)

    def test_a_confidence_across_the_floor_by_less_than_the_delta_stays_one_run_at_its_first_level(
        self,
    ):
        """The floor is applied to a run's first sample, which gives the run its level."""
        under = sample_guesses(
            [(6 * i, guesses.guess("present", p)) for i, p in enumerate((54, 58, 56))]
        )
        self.assertEqual([r[3] for r in under], [10])
        self.assertFalse(guesses.Guesses(under))
        over = sample_guesses(
            [(6 * i, guesses.guess("present", p)) for i, p in enumerate((56, 54, 55))]
        )
        self.assertEqual([r[3] for r in over], [11])
        self.assertTrue(guesses.Guesses(over))

    def test_a_recording_gap_ends_the_run_at_its_last_sample_and_opens_a_new_one(self):
        guess = guesses.guess("present", 90)
        level = guesses.level(90)
        runs = sample_guesses([(0, guess), (6, guess), (6 + GAP + 0.5, guess), (30, guess)])
        self.assertEqual(runs, [(0, 9, "present", level, 0), (18.5, 33, "present", level, 0)])
        self.assertEqual(
            sample_guesses([(0, guess), (6, guess), (6 + GAP, guess)]),
            [(0, 9 + GAP, "present", level, 0)],
            "exactly the gap apart is still one stretch",
        )

    def test_an_unguessed_reading_closes_a_run_and_the_next_guess_starts_at_its_reading(self):
        guess = guesses.guess("not_present", 70)
        runs = sample_guesses([(0, guess), (6, None), (12, guess), (18, None)])
        self.assertEqual(runs, [(0, 6, "not_present", 14, 0), (12, 18, "not_present", 14, 0)])

    def test_an_isolated_sample_covers_its_terminal_half_sample(self):
        guess = guesses.guess("present", 90)
        known = guesses.Guesses(sample_guesses([(0, guess), (100, guess)]))
        self.assertEqual(len(known.runs), 2)
        self.assertEqual(known.at(100), ("present", "automatic", 0.9))
        self.assertIsNone(known.at(103), "coverage ends after the terminal half sample")
        self.assertEqual(known.of_sample(100)[2:], ("present", 18, guesses.UNKNOWN))

    def test_confidences_are_read_in_one_walk_over_the_runs(self):
        runs = [
            (0, 10, "present", 16, 0),
            (10, 20, "not_present", 12, 0),
            (40, 50, "present", 8, 0),
        ]
        known = guesses.Guesses(runs)
        times = [-5, 0, 5, 10, 15, 20, 21, 39, 40, 50, 51, 99]
        walk = list(known.percents(times))
        self.assertEqual(walk, [0, 80, 80, 60, 60, 0, 0, 0, 40, 0, 0, 0])
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
                    self.assertEqual(
                        (run[2], run[3]), ({1: "present", 2: "not_present"}[code], percent // STEP)
                    )
                else:
                    self.assertIsNone(run)
                floor = percent / 100 >= MIN_AUTO_CONFIDENCE and code
                expected = (
                    ({1: "present", 2: "not_present"}[code], "automatic", percent / 100)
                    if floor
                    else None
                )
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


class StoreTests(unittest.TestCase):
    def test_a_live_guess_at_its_start_is_readable_without_claiming_elapsed_coverage(self):
        run = (6, 6, "present", 15, 0)
        known = guesses.Guesses([run])
        self.assertEqual(known.sample_guess(6), ("present", "automatic", 0.75))
        self.assertEqual(known.of_sample(6), run)
        self.assertEqual(list(known.percents([6])), [75])
        self.assertIsNone(known.at(6))

    def test_a_recent_read_loads_only_days_reaching_the_window(self):
        from types import SimpleNamespace

        from tuner_under_test.history import dayfile, daystore

        runtime = SimpleNamespace(data={"devices": {"a": {}}})
        days = harness._store(runtime)
        start = daystore.bounds(daystore.date_of(BASE))[0]
        for index in range(3):
            low = start + index * 86400
            days.append("a", dayfile.Change(low, {0: 30}))
            days.append("a", dayfile.Guess(low, "present", 16, 0))
            days.append("a", dayfile.End(low + 60))
        runtime._history_until = start + 2 * 86400 + 60
        cutoff = start + 2 * 86400
        with patch.object(days, "day", wraps=days.day) as read:
            recent = guesses.of(runtime, "a", cutoff)
        self.assertEqual(recent.runs, [(cutoff, cutoff + 60, "present", 16, 0)])
        self.assertEqual([call.args[1] for call in read.call_args_list], [daystore.date_of(cutoff)])

    def test_an_unguessed_reading_at_a_run_end_has_no_previous_guess(self):
        known = guesses.Guesses(sample_guesses([(0, ("present", 80, 0)), (6, None)]))
        self.assertIsNone(known.of_sample(6))
        self.assertIsNone(known.sample_guess(6))
        self.assertEqual(list(known.percents([5, 6, 7])), [80, 0, 0])

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
        self.assertIsNone(known.of_sample(20), "native coverage is half-open")
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

    def test_guesses_compare_by_their_runs(self):
        runs = [(0, 10, "present", 16, 0)]
        self.assertEqual(guesses.Guesses(runs), guesses.Guesses(list(runs)))
        self.assertNotEqual(guesses.Guesses(runs), guesses.Guesses())
        self.assertNotEqual(guesses.Guesses(runs), runs)
        self.assertFalse(guesses.EMPTY)
        self.assertFalse(guesses.Guesses([(0, 10, "present", 8, 0)]), "under the floor")


# --- Recording and storing. --------------------------------------------------------------


class RecordingTests(unittest.IsolatedAsyncioTestCase):
    """Guess runs recorded into the day store by the poll (history/live.py, history/ingest.py).
    Store mechanics (append-only logs, crash recovery ending the open run, no duplicate across
    flushes) are pinned by tests/test_daystore.py; retention and size by the thinning tests."""

    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def setUp(self):
        harness.RuntimeTests.setUp(self)
        # Noon UTC on the harness's day: the runs here span minutes and must not cross into
        # the next day file. Recorded times are whole milliseconds.
        self.now = self.clock = float(int(self.now) // 86400 * 86400 + 43200)
        clock = patch.object(time, "time", side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)

    def classify(self, state, confidence, basis="adaptive-guided", at=None, **extra):
        self.device.setdefault("auto", {})["last_classification"] = {
            "state": state,
            "confidence": confidence,
            "timestamp": at,
            "basis": basis,
            **extra,
        }

    def record(self, offset, state="present", confidence=0.9, **extra):
        when = self.clock = self.now + offset  # The poll runs now.
        self.classify(state, confidence, at=when, **extra)
        self.runtime._record_history_sample("a", {"g0_move": 20}, when)

    def test_a_steady_guess_is_one_run_and_unchanged_readings_store_one_row(self):
        for i in range(130):
            self.record(i * 6)
        self.assertEqual(
            guesses.of(self.runtime, "a").runs,
            [(self.now, self.now + 129 * 6, "present", 18, guesses.REFERENCE)],
        )
        cells = list(spans.events(self.runtime, "a"))
        self.assertEqual(
            [(c.start, c.end, c.values[0]) for c in cells], [(self.now, self.now + 129 * 6, 20)]
        )

    def test_a_confidence_flapping_across_a_step_edge_records_one_run(self):
        for i in range(80):
            self.record(i * 6, confidence=0.72 if i % 2 else 0.73)
        self.assertEqual(len(guesses.of(self.runtime, "a").runs), 1)

    def test_the_floor_applies_to_the_raw_confidence_not_the_rounded_one(self):
        for i, confidence in enumerate((0.53, 0.53, 0.54, 0.549)):
            self.record(i * 6, confidence=confidence)
        self.record(24, "unknown", 0.0)
        self.record(30, confidence=0.55)
        self.record(36, confidence=0.55)
        low, floor = guesses.of(self.runtime, "a").runs
        self.assertEqual((low[3], floor[3]), (10, 11))
        known = guesses.of(self.runtime, "a")
        self.assertIsNone(known.sample_guess(self.now + 6), "0.53 does not train")
        self.assertIsNone(known.sample_guess(self.now + 18), "0.549 does not train")
        self.assertEqual(known.sample_guess(self.now + 30), ("present", "automatic", 0.55))

    def test_a_change_of_state_or_basis_starts_a_run_and_an_unguessed_poll_ends_one(self):
        self.record(0)
        self.record(6)
        self.record(12, confidence=0.5)
        self.record(18, confidence=0.5, basis="external")
        self.record(24, "not_present", 0.5, basis="external")
        self.record(30, "unknown", 0.0)
        self.record(36, "not_present", 0.5, basis="external")
        runs = guesses.of(self.runtime, "a").runs
        self.assertEqual(
            [r[:3] for r in runs],
            [
                (self.now, self.now + 12, "present"),
                (self.now + 12, self.now + 18, "present"),
                (self.now + 18, self.now + 24, "present"),
                (self.now + 24, self.now + 30, "not_present"),
                (self.now + 36, self.now + 36, "not_present"),
            ],
        )

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

    def test_pausing_recording_ends_the_run_and_resuming_starts_a_new_one(self):
        self.record(0)
        self.record(6)
        self.clock = self.now + 9
        self.runtime.set_recording("a", False)
        self.runtime.set_recording("a", True)
        self.record(12)
        self.record(18)
        runs = guesses.of(self.runtime, "a").runs
        self.assertEqual(
            [run[:2] for run in runs], [(self.now, self.now + 9), (self.now + 12, self.now + 18)]
        )

    async def test_a_save_writes_the_recorded_changes_and_runs_to_the_day_files(self):
        for i in range(70):
            self.record(i * 6)
        await self.runtime.async_save()
        folder = self.runtime._days.folder("a")
        self.assertTrue(any(folder.glob("*.log")))
        saved = self.runtime.store.async_save.call_args.args[0]["devices"]["a"]
        self.assertNotIn("guess_runs", saved)
        self.assertNotIn("history", saved)

    def test_clearing_a_radar_forgets_its_runs_and_its_recorded_history(self):
        for i in range(70):
            self.record(i * 6)
        self.runtime.clear_samples("a")
        self.assertEqual(guesses.of(self.runtime, "a").runs, [])
        self.assertEqual(list(spans.events(self.runtime, "a")), [])
        self.assertEqual(self.runtime._days.days("a"), [])

    def test_views_see_the_recorded_history_as_of_when_they_were_taken(self):
        for i in range(130):
            self.record(i * 6, "present" if i < 70 else "not_present")
        view = self.runtime._history_view("a")
        before = guesses.of(view, "a").runs
        self.assertEqual(len(before), 2)
        self.assertEqual(before[1][1], self.now + 129 * 6, "the view ends where it was taken")
        self.record(800, "present")
        self.assertEqual(guesses.of(view, "a").runs, before, "recording does not reach the view")

    def test_newest_observation_is_open_coverage_or_where_it_ended(self):
        self.assertIsNone(recording.newest(self.runtime, "a", 0))
        for i in range(61):
            self.record(i * 6)
        self.assertEqual(recording.newest(self.runtime, "a", 0), self.now + 360)
        self.clock = self.now + 500
        self.runtime.set_recording("a", False)
        self.assertEqual(recording.newest(self.runtime, "a", 0), self.now + 500)
        self.assertIsNone(recording.newest(self.runtime, "a", self.now + 501))
        self.assertIsNone(recording.newest(self.runtime, "missing", 0))


# --- Confirming buffered external presence. ----------------------------------------------


class ConfirmEquivalenceTests(unittest.TestCase):
    """``confirm_presence`` fills only the unguessed time in ``[start, end)``; earlier guesses
    and earlier confirmations are never replaced (an interval oracle checks it)."""

    def runtime(self):
        from tuner_under_test.history import daystore

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        runtime = harness.mod.TunerRuntime(None, None, {"devices": {"a": {}}})
        runtime._schedule_save = lambda: None
        runtime._days = daystore.DayStore(Path(folder.name))
        return runtime

    def test_random_confirmations_fill_only_unguessed_time(self):
        from tuner_under_test.history import dayfile

        rng = random.Random(7005)
        for case in range(25):
            runtime, start = self.runtime(), float(BASE + 1000 * case)
            runtime._days.append("a", dayfile.Change(start, {0: 40}))
            oracle, at = [], start  # (start, end, state, level, basis) in the order they win.
            for _ in range(rng.randrange(1, 8)):
                state = rng.choice(("present", "not_present", None))
                length = rng.choice((6, 30, 120))
                runtime._days.append("a", dayfile.Guess(at, state, 18, guesses.REFERENCE))
                if state:
                    oracle.append((at, at + length, state, 18, guesses.REFERENCE))
                at += length
            runtime._days.append("a", dayfile.Guess(at, None, 0, 0))
            for _ in range(3):
                low = rng.uniform(start - 20, at)
                high, percent = low + rng.choice((10, 60, 400)), rng.choice((50, 77, 90, 100))
                with patch.object(time, "time", return_value=at + 500):
                    source_labels.confirm_presence(runtime, "a", low, high, percent)
                oracle.append((low, high, "present", guesses.level(percent), guesses.EXTERNAL))
            runs = guesses.of(runtime, "a").runs
            for _ in range(200):
                t = round(rng.uniform(start - 30, at + 450), 3)
                expected = next((run[2:] for run in oracle if run[0] <= t < run[1]), None)
                found = next((run[2:] for run in runs if run[0] <= t < run[1]), None)
                if all(abs(t - edge) > 0.002 for run in oracle for edge in run[:2]):
                    self.assertEqual(found, expected, (case, t))

    def test_progressive_confirmation_keeps_one_growing_run(self):
        from tuner_under_test.history import dayfile

        runtime = self.runtime()
        runtime._days.append("a", dayfile.Change(BASE, {0: 40}))
        with patch.object(time, "time", return_value=BASE + 1300):
            for end in range(6, 1200, 6):
                source_labels.confirm_presence(runtime, "a", BASE, BASE + end, 90)
        runs = guesses.of(runtime, "a").runs
        self.assertEqual(runs, [(BASE, BASE + 1194, "present", 18, guesses.EXTERNAL)])


# --- The learner reads snippets of stretches, bounded, not every sample. ----------------------


class LearningEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clock = patch.object(time, "time", return_value=1_800_000_000.0)
        clock.start()
        self.addCleanup(clock.stop)
        harness.RuntimeTests.setUp(self)

    asyncTearDown = harness.RuntimeTests.asyncTearDown
    constants = sys.modules["tuner_under_test.calibration.constants"]

    def history(self, samples, labels=()):
        self.device["history_labels"] = list(labels)
        harness.record_rows(self.runtime, "a", samples)
        return objective(self.runtime, start=0)

    def sample(self, ts, code, value=30, percent=100):
        return ts, bytes([value] + [255] * 17) + bytes((code, percent))

    def stood_for(self, regions):
        return {
            state: sum((b - a) / 6 for a, b, _, label, *_ in regions if label == state)
            for state in ("present", "not_present")
        }

    def test_a_history_that_fits_the_cap_preserves_all_observed_duration(self):
        samples = [self.sample(self.now - 86400 + 6 * i, 1 if i < 3000 else 2) for i in range(6000)]
        regions = self.history(samples).regions
        self.assertEqual(self.stood_for(regions), {"present": 3000, "not_present": 2999.5})
        self.assertTrue(all(row[4:] == ("automatic", 1.0) for row in regions))

    def test_a_long_history_is_compact_and_preserves_all_observed_duration(self):
        count = 40000
        samples = [
            self.sample(self.now - 6 * (count - i), 1 if i < count // 2 else 2)
            for i in range(count)
        ]
        regions = self.history(samples).regions
        self.assertLess(len(regions), self.constants.MAX_CLASS_SAMPLES)
        self.assertEqual(self.stood_for(regions), {"present": 20000, "not_present": 19999.5})

    def test_a_stretch_counts_for_its_duration_and_a_recording_gap_for_nothing(self):
        samples = [self.sample(self.now - 100000 + 6 * i, 1) for i in range(250)]
        samples += [self.sample(self.now - 50000 + 6 * i, 1) for i in range(130)]
        regions = self.history(samples).regions
        self.assertEqual(self.stood_for(regions)["present"], 379)
        self.assertTrue(
            all(
                b <= self.now - 100000 + 249 * 6 + 3 or a >= self.now - 50000
                for a, b, *_ in regions
            )
        )

    def test_the_newest_samples_of_each_class_are_kept_when_over_the_cap(self):
        from tuner_under_test.calibration import event_fitting

        samples = [self.sample(self.now - 6 * (400 - i), 1) for i in range(400)]
        target = self.history(samples)
        with patch.object(event_fitting, "MAX_CLASS_SAMPLES", 50):
            groups = event_fitting._sample_groups(target, False)
        expected = [
            step * 6
            for step in range(
                (ceil((samples[-1][0] + 3) / 6) - 1) - 49, (ceil((samples[-1][0] + 3) / 6) - 1) + 1
            )
        ]
        self.assertEqual([row[0] for row in groups["present"]], expected)
        self.assertEqual(groups["not_present"], [])

    def test_a_change_of_label_ends_a_stretch(self):
        samples = [
            self.sample(self.now - 6 * (200 - i), 1 if i % 100 < 50 else 2) for i in range(200)
        ]
        regions = self.history(samples).regions
        self.assertEqual(self.stood_for(regions), {"present": 100, "not_present": 99.5})
        self.assertEqual(
            [row[3] for row in regions], ["present", "not_present", "present", "not_present"]
        )

    def test_human_samples_keep_their_count_cap_and_all_their_age(self):
        from tuner_under_test.calibration.event_fitting import _sample_groups

        old = self.now - 10 * 86400
        samples = [self.sample(old + 6 * i, 0) for i in range(5200)]
        samples += [self.sample(self.now - 6 * 5200 + 6 * i, 0, value=3) for i in range(5200)]
        labels = [
            {"start": old - 1, "end": old + 6 * 5200, "state": "present"},
            {"start": self.now - 6 * 5200 - 1, "end": self.now + 1, "state": "not_present"},
        ]
        target = self.history(samples, labels)
        groups = _sample_groups(target, True)
        self.assertEqual(
            {state: len(rows) for state, rows in groups.items()},
            {"present": 5000, "not_present": 5000},
        )
        self.assertTrue(all(row[4] == "human" for row in target.regions))
        self.assertLess(groups["present"][-1][0], self.now - 9 * 86400)

    def test_a_radar_with_no_history_has_no_evidence(self):
        self.assertEqual(objective(self.runtime, end=self.now).regions, [])


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

    def test_automatic_share_counts_independent_episodes_instead_of_duration(self):
        from types import SimpleNamespace

        from tuner_under_test.history import dayfile

        def share(duration, episodes=1):
            view = SimpleNamespace(
                data={
                    "devices": {
                        "a": {
                            "history_labels": [
                                {"start": 0, "end": 600, "state": "present"},
                                {"start": 600, "end": 1200, "state": "not_present"},
                            ]
                        }
                    },
                    "room_snapshot": None,
                },
                hass=None,
            )
            days = harness._store(view)
            days.append("a", dayfile.Change(0, {0: 50}))
            for index in range(episodes):
                start = 1200 + index * (duration + 60)
                days.append("a", dayfile.Guess(start, "present", 20, 2))
                days.append("a", dayfile.Guess(start + duration, None, 0, 0))
            end = start + duration
            days.append("a", dayfile.End(end))
            view._history_until = end
            return objective(view, weight=1.0).shares[0]

        # Approved adaptive plan: cap * independent auto mass / (human episodes + auto mass).
        self.assertEqual(share(60), share(300))
        self.assertAlmostEqual(share(60), 0.25 * 1 / (1 + 1))
        self.assertAlmostEqual(share(60, episodes=2), 0.25 * 2 / (1 + 2))
