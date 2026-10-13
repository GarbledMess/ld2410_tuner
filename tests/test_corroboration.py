"""Which automatic guesses count as evidence (P6b), in one place: ``history.trust`` and
``guesses.evidence``.

A radar's recorded confidence is a reference-age cap, not a probability (P6a: recorded not_present
guesses were right every time on four radars, recorded present guesses 6-88 %). Evidence is the
guess with its calibrated confidence: not_present counts as it is; present counts only when its
calibrated confidence reaches ``MIN_AUTO_CONFIDENCE`` and the radar's quiet baseline does not
veto it (every gate within the ceiling of its Empty-labelled readings). A room's present estimate
also needs two members agreeing."""

import math
import sys
import types
import unittest

import test_tuner as harness
from tuner_under_test.calibration import constants
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history import guesses, trust
from tuner_under_test.rooms import evidence as room_evidence
from tuner_under_test.rooms import truth

references = sys.modules["tuner_under_test.presence.references"]
STEP = constants.GUESS_CONFIDENCE_STEP
BASE = 1_700_000_000.0
REFERENCE, EXTERNAL = guesses.REFERENCE, guesses.EXTERNAL
QUIET_BIN = 8  # The Empty readings of the radars below.


def level(confidence):
    return round(confidence / STEP)


def run(start, end, state, confidence, basis=REFERENCE):
    return (BASE + start, BASE + end, state, level(confidence), basis)


def histograms(count=200, at=QUIET_BIN):
    """Empty-labelled readings of every gate, all at ``at``."""
    return {
        key: {"present": [0] * 101, "not_present": [count if i == at else 0 for i in range(101)]}
        for key in HISTORY_KEYS
    }


def row(**gates):
    """A history row: the gates given (``g0_move=20``), the rest unread."""
    return bytes(gates.get(key, 255) for key in HISTORY_KEYS)


def runtime_of(runs, samples=(), hist=None):
    """The part of a runtime ``guesses.evidence`` reads: stored chunks and history rows."""
    device = {"guess_runs": guesses.encode(runs)}
    if hist is not None:
        device["histograms"] = hist
    ordered = sorted(samples)
    return types.SimpleNamespace(
        data={"devices": {"a": device}},
        _history_runtime={},
        _iter_history_samples=lambda device, since=0, until=math.inf: iter(
            [sample for sample in ordered if sample[0] >= since]
        ),
    )


class CalibratedConfidenceTests(unittest.TestCase):
    def test_not_present_is_trusted_whatever_its_recorded_confidence(self):
        for confidence in (0.55, 0.65, 0.8, 0.9, 0.975, 1.0):
            self.assertEqual(
                trust.trusted("not_present", confidence), constants.TRUST_NOT_PRESENT, confidence
            )

    def test_present_is_trusted_by_the_age_class_of_its_reference(self):
        """No reference (0.65) and a fresh one (above 0.975) were right; the decaying middle was
        not (0.1-0.2 right on three radars)."""
        trusted = {c: trust.trusted("present", c) for c in (0.55, 0.65, 0.7, 0.9, 0.95, 0.975, 1)}
        self.assertEqual(trusted[0.55], trusted[0.65])
        self.assertGreaterEqual(trusted[0.65], constants.MIN_AUTO_CONFIDENCE)
        for middle in (0.7, 0.9, 0.95):
            self.assertLess(trusted[middle], constants.MIN_AUTO_CONFIDENCE, middle)
        self.assertGreaterEqual(trusted[0.975], constants.MIN_AUTO_CONFIDENCE)
        self.assertEqual(trusted[1], trusted[0.975])

    def test_the_calibration_table_is_a_step_function_of_probabilities(self):
        bounds = [bound for bound, _ in constants.TRUST_PRESENT]
        self.assertEqual(bounds, sorted(bounds))
        self.assertEqual(bounds[0], 0.0)
        for _, probability in constants.TRUST_PRESENT:
            self.assertTrue(0 < probability <= 1)
        self.assertTrue(0 < constants.TRUST_NOT_PRESENT <= 1)

    def test_every_trusted_confidence_is_a_whole_number_of_steps(self):
        for value in (
            constants.TRUST_NOT_PRESENT,
            *(probability for _, probability in constants.TRUST_PRESENT),
        ):
            self.assertAlmostEqual(value / STEP, round(value / STEP), places=9)


class EvidenceTests(unittest.TestCase):
    def runs(self, *runs, **kwargs):
        return guesses.evidence(runtime_of(runs, **kwargs), "a").runs

    def test_a_not_present_guess_counts_at_its_calibrated_confidence(self):
        level_ = level(constants.TRUST_NOT_PRESENT)
        for confidence in (0.65, 0.9, 1.0):
            self.assertEqual(
                self.runs(run(0, 60, "not_present", confidence)),
                [(BASE, BASE + 60, "not_present", level_, REFERENCE)],
                confidence,
            )

    def test_neighbouring_not_present_runs_that_calibrate_alike_are_one_run(self):
        merged = self.runs(
            run(0, 30, "not_present", 0.65),
            run(30, 60, "not_present", 0.9),
            run(60, 90, "not_present", 1.0),
        )
        self.assertEqual([(r[0], r[1]) for r in merged], [(BASE, BASE + 90)])

    def test_a_gap_keeps_runs_apart(self):
        parted = self.runs(run(0, 30, "not_present", 0.9), run(40, 60, "not_present", 0.9))
        self.assertEqual(len(parted), 2)

    def test_a_present_guess_from_a_stale_reference_does_not_count(self):
        for confidence in (0.7, 0.85, 0.95):
            self.assertEqual(self.runs(run(0, 60, "present", confidence)), [], confidence)

    def test_a_present_guess_from_a_fresh_or_no_reference_counts(self):
        for confidence in (0.65, 1.0):
            (found,) = self.runs(run(0, 60, "present", confidence))
            self.assertEqual(found[2], "present")
            self.assertGreaterEqual(found[3] * STEP, constants.MIN_AUTO_CONFIDENCE)

    def test_an_external_guess_keeps_its_own_confidence(self):
        given = run(0, 60, "present", 0.9, EXTERNAL)
        self.assertEqual(self.runs(given), [given])
        low = runtime_of([run(0, 60, "present", 0.5, EXTERNAL)])
        self.assertIsNone(guesses.evidence(low, "a").at(BASE + 30), "still under the floor")

    def test_runs_the_radar_has_no_baseline_for_pass_the_veto(self):
        sure = run(0, 60, "present", 1.0)
        self.assertEqual(len(self.runs(sure, samples=[(BASE + 6, row(g0_move=3))])), 1)
        empty_hist = histograms(count=0)
        self.assertEqual(
            len(self.runs(sure, samples=[(BASE + 6, row(g0_move=3))], hist=empty_hist)), 1
        )


class QuietVetoTests(unittest.TestCase):
    """A present guess does not count where every gate reads within its Empty ceiling."""

    CEILING = QUIET_BIN + constants.QUIET_MARGIN

    def evidence(self, run_, readings, hist=None):
        samples = [(BASE + 6 * i, row(g0_move=value)) for i, value in enumerate(readings)]
        runtime = runtime_of([run_], samples, histograms() if hist is None else hist)
        return guesses.evidence(runtime, "a")

    def test_ceilings_are_the_quantile_of_each_gate_plus_the_margin(self):
        found = trust.ceilings(histograms())
        self.assertEqual(found, (self.CEILING,) * len(HISTORY_KEYS))
        loud = histograms(count=2000)
        loud["g0_move"]["not_present"][30] = 1
        self.assertEqual(trust.ceilings(loud)[0], self.CEILING, "one loud reading in 2000")
        loud["g0_move"]["not_present"][30] = 50
        self.assertEqual(trust.ceilings(loud)[0], 30 + constants.QUIET_MARGIN)

    def test_a_gate_with_too_few_empty_readings_has_no_ceiling(self):
        sparse = histograms()
        sparse["g1_move"]["not_present"] = [1 if i == 5 else 0 for i in range(101)]
        found = trust.ceilings(sparse)
        self.assertEqual(found[HISTORY_KEYS.index("g1_move")], 100)
        self.assertEqual(trust.ceilings(histograms(count=0)), None)
        self.assertIsNone(trust.ceilings(None))
        self.assertIsNone(trust.ceilings({}))

    def test_quiet_is_no_gate_above_its_ceiling_and_unread_gates_never_exceed(self):
        found = trust.ceilings(histograms())
        self.assertTrue(trust.quiet(row(g0_move=self.CEILING), found))
        self.assertFalse(trust.quiet(row(g0_move=self.CEILING + 1), found))
        self.assertFalse(trust.quiet(row(g0_move=1, g3_still=self.CEILING + 1), found))
        self.assertTrue(trust.quiet(row(), found), "nothing read, nothing above")

    def test_quiet_samples_of_a_present_run_are_cut_out_of_it(self):
        readings = [20, 20, 3, 3, 3, 20, 20]  # Samples 6 s apart.
        found = self.evidence(run(0, 36, "present", 1.0), readings)
        self.assertEqual(
            [(r[0] - BASE, r[1] - BASE) for r in found.runs],
            [(0, 9.0), (27.0, 36)],
            "cut at the midpoints with the quiet samples",
        )

    def test_a_present_run_that_is_quiet_throughout_is_gone(self):
        self.assertEqual(self.evidence(run(0, 36, "present", 1.0), [3] * 7).runs, [])

    def test_a_loud_present_run_is_whole(self):
        found = self.evidence(run(0, 36, "present", 1.0), [20] * 7)
        self.assertEqual([(r[0] - BASE, r[1] - BASE) for r in found.runs], [(0, 36)])

    def test_quiet_does_not_veto_not_present_or_external_guesses(self):
        quiet = [3] * 7
        self.assertEqual(len(self.evidence(run(0, 36, "not_present", 1.0), quiet).runs), 1)
        self.assertEqual(len(self.evidence(run(0, 36, "present", 0.9, EXTERNAL), quiet).runs), 1)

    def test_a_run_between_two_vetoed_ones_keeps_only_what_is_loud(self):
        found = self.evidence(run(0, 18, "present", 1.0), [3, 20, 3, 20])
        self.assertEqual([(r[0] - BASE, r[1] - BASE) for r in found.runs], [(3.0, 9.0), (15.0, 18)])

    def test_the_veto_reads_only_samples_inside_present_runs(self):
        seen = []
        runtime = runtime_of(
            [run(0, 12, "present", 1.0), run(100, 160, "not_present", 1.0)],
            [(BASE + 6 * i, row(g0_move=20)) for i in range(40)],
            histograms(),
        )
        original = runtime._iter_history_samples

        def spy(device, since=0, until=math.inf):
            for sample in original(device, since, until):
                seen.append(sample[0])
                yield sample

        runtime._iter_history_samples = spy
        guesses.evidence(runtime, "a")
        self.assertTrue(seen, "the samples were read")


class LiveCountTests(unittest.TestCase):
    """The same rules, applied to a sample as it is recorded (``guesses.counted``)."""

    def test_the_calibrated_confidence_of_a_counted_sample(self):
        found = guesses.counted(("not_present", 65, REFERENCE), row(g0_move=3), histograms())
        self.assertEqual(found, constants.TRUST_NOT_PRESENT)
        loud = guesses.counted(("present", 100, REFERENCE), row(g0_move=30), histograms())
        self.assertGreaterEqual(loud, constants.MIN_AUTO_CONFIDENCE)

    def test_a_quiet_present_sample_or_a_stale_one_does_not_count(self):
        self.assertIsNone(
            guesses.counted(("present", 100, REFERENCE), row(g0_move=3), histograms())
        )
        self.assertIsNone(
            guesses.counted(("present", 90, REFERENCE), row(g0_move=30), histograms())
        )
        self.assertIsNone(guesses.counted(None, row(g0_move=30), histograms()))

    def test_an_external_sample_counts_as_it_was_given(self):
        found = guesses.counted(("present", 90, EXTERNAL), row(g0_move=3), histograms())
        self.assertEqual(found, 0.9)

    def test_a_radar_without_a_baseline_cannot_veto(self):
        self.assertIsNotNone(guesses.counted(("present", 100, REFERENCE), row(g0_move=3), None))


class RoomAgreementTests(unittest.TestCase):
    def test_a_present_estimate_needs_two_members_agreeing(self):
        one = truth.estimate([("present", 0.95), None])
        self.assertIsNone(one)
        self.assertIsNone(truth.estimate([("present", 0.95), ("not_present", 0.95)]))
        both = truth.estimate([("present", 0.9), ("present", 0.9)])
        self.assertEqual(both[:2], ("present", "automatic"))
        self.assertAlmostEqual(both[2], 0.99)

    def test_a_group_of_one_needs_only_that_member(self):
        self.assertEqual(truth.estimate([("present", 0.9)]), ("present", "automatic", 0.9))

    def test_a_not_present_estimate_is_unchanged(self):
        self.assertEqual(
            truth.estimate([("not_present", 0.95), ("not_present", 0.9)]),
            ("not_present", "automatic", 0.9),
        )

    def test_the_room_label_of_automatic_members_needs_two_present_too(self):
        def member(state, source="automatic"):
            return {"manual": lambda _t: None, "automatic": _timeline(state, source)}

        def label(*states):
            return room_evidence.room_label({f"m{i}": member(s) for i, s in enumerate(states)}, 50)

        self.assertIsNone(label("present", "not_present"))
        self.assertEqual(label("present", "present")[:2], ("present", "automatic"))
        self.assertEqual(label("present")[:2], ("present", "automatic"))
        self.assertEqual(label("not_present", "not_present")[:2], ("not_present", "automatic"))

    def test_one_human_present_member_still_makes_the_room_present(self):
        human = {"manual": lambda _t: "present", "automatic": _timeline(None, "automatic")}
        other = {"manual": lambda _t: None, "automatic": _timeline("not_present", "automatic")}
        found = room_evidence.room_label({"a": human, "b": other}, 50)
        self.assertEqual(found, ("present", "human", 1.0))


def _timeline(state, _source):
    from tuner_under_test.rooms.intervals import Timeline

    return Timeline([(0, 100, (state, "automatic", 0.9))] if state else [])


class HistoryViewTests(unittest.IsolatedAsyncioTestCase):
    """An executor view carries the quiet baseline, so its evidence is the live one."""

    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def setUp(self):
        harness.RuntimeTests.setUp(self)

    def test_a_view_vetoes_with_a_copy_of_the_labelled_histograms(self):
        from tuner_under_test.history import recording

        self.device["histograms"] = histograms()
        rows = [(self.now + 6 * i, row(g0_move=3)) for i in range(6)]
        self.runtime._history_runtime["a"] = {
            "samples": rows,
            "runs": [[self.now, self.now + 30, "present", 20, REFERENCE]],
        }
        view = recording._history_view(self.runtime, "a")
        self.assertEqual(guesses.evidence(view, "a").runs, [])
        self.assertIsNot(view.data["devices"]["a"]["histograms"], self.device["histograms"])


class RecordedGuessesAreStillRawTests(unittest.TestCase):
    def test_of_is_the_recorded_runs_and_evidence_the_trusted_ones(self):
        runtime = runtime_of([run(0, 60, "present", 0.9)])
        self.assertEqual(len(guesses.of(runtime, "a").runs), 1)
        self.assertEqual(guesses.evidence(runtime, "a").runs, [])


class LiveRecordingTests(unittest.IsolatedAsyncioTestCase):
    """Recording a sample trains the references only with a counted guess."""

    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def setUp(self):
        harness.RuntimeTests.setUp(self)
        profile = self.device["reference_profile"] = references.empty()
        for index in range(5):  # A human reference: bootstrap guesses cannot train alone.
            references.observe(profile, {"g0_move": 8}, "not_present", "human", 1.0, index)
        self.device["histograms"] = histograms()

    def classify(self, state, confidence, at):
        self.device.setdefault("auto", {})["last_classification"] = {
            "state": state,
            "confidence": confidence,
            "timestamp": at,
            "basis": "adaptive-guided",
        }

    def record(self, offset, state, confidence, energy):
        when = self.now + offset
        self.classify(state, confidence, when)
        self.runtime._record_history_sample("a", {"g0_move": energy}, when)

    def radar_periods(self, state):
        return [
            p
            for p in self.device["reference_profile"]["periods"]
            if (p["source"], p["state"]) == ("radar", state)
        ]

    def test_a_quiet_present_guess_does_not_train_the_radars_own_references(self):
        for index in range(20):
            self.record(index * 6, "present", 0.99, 5)
        self.assertEqual(self.radar_periods("present"), [])

    def test_a_loud_present_guess_from_a_fresh_reference_does(self):
        for index in range(20):
            self.record(index * 6, "present", 0.99, 40)
        self.assertEqual(sum(p["count"] for p in self.radar_periods("present")), 20)

    def test_a_present_guess_from_a_stale_reference_does_not(self):
        for index in range(20):
            self.record(index * 6, "present", 0.9, 40)
        self.assertEqual(self.radar_periods("present"), [])

    def test_a_not_present_guess_trains_at_its_calibrated_confidence(self):
        for index in range(20):
            self.record(index * 6, "not_present", 0.65, 5)
        periods = self.radar_periods("not_present")
        self.assertEqual(sum(p["count"] for p in periods), 20)
        self.assertAlmostEqual(periods[0]["confidence"], constants.TRUST_NOT_PRESENT)

    def test_the_guess_itself_is_recorded_raw_whatever_it_trains(self):
        for index in range(5):
            self.record(index * 6, "present", 0.9, 5)
        (recorded,) = self.runtime._history_runtime["a"]["runs"]
        self.assertEqual(tuple(recorded[2:4]), ("present", level(0.9)))


if __name__ == "__main__":
    unittest.main()
