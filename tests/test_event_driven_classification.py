"""A radar's readings are classified when something notably changes, not on every 2 s tick.

The last classification is reused (and stays current for the panel, the feedback gate and
segments) while the readings stay within ``CHANGE`` of the ones classified, the inputs of the
classification are unchanged and the filter is at rest, at most ``HEARTBEAT`` seconds. The
temporal filter and the confirmation integrate over the time the result was reused."""

import importlib.util
import sys
import time
import types
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import test_tuner as harness

inference = sys.modules["tuner_under_test.presence.inference"]
autolabelling = sys.modules["tuner_under_test.presence.autolabelling"]
references = sys.modules["tuner_under_test.presence.references"]
CHANGE, HEARTBEAT, SUSTAIN = inference.CHANGE, inference.HEARTBEAT, inference.SUSTAIN
TICK = harness.constants.AUTO_SAMPLE_INTERVAL
KEY = "g0_move"


class TickTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    sample = harness.RuntimeTests.sample

    def run_ticks(self, seconds, value=10, start=0.0):
        """Sample every tick for ``seconds``; ``value`` may be a function of the elapsed time."""
        base = self.now + start
        for tick in range(int(seconds / TICK) + 1):
            elapsed = tick * TICK
            self.sample(base + elapsed, value(elapsed) if callable(value) else value)

    def classify_spy(self):
        return patch.object(self.runtime, "_classify_auto", wraps=self.runtime._classify_auto)

    def test_unchanged_readings_are_classified_once_per_heartbeat_not_every_tick(self):
        with self.classify_spy() as classify:
            self.run_ticks(2 * HEARTBEAT)
        ticks = int(2 * HEARTBEAT / TICK) + 1
        self.assertEqual(classify.call_count, 3, f"first tick, then each heartbeat of {ticks}")

    def test_a_move_that_lasts_is_classified_once_it_has_lasted_a_smaller_one_never_is(self):
        self.sample(self.now, 10)
        with self.classify_spy() as classify:
            for tick in range(1, 15):  # A move just under the threshold, for half a minute.
                self.sample(self.now + tick * TICK, 10 + CHANGE - 1)
            self.assertEqual(classify.call_count, 0)
            self.sample(self.now + 30, 10 + CHANGE)
            self.assertEqual(classify.call_count, 0, "the move has not lasted yet")
            self.sample(self.now + 30 + SUSTAIN - 0.1, 10 + CHANGE + 1)
            self.assertEqual(classify.call_count, 0)
            self.sample(self.now + 30 + SUSTAIN, 10 + CHANGE)
            self.assertEqual(classify.call_count, 1)
            self.sample(self.now + 32 + SUSTAIN, 10 + CHANGE + 1)
            self.assertEqual(classify.call_count, 1, "compared with the readings classified")

    def test_a_lone_spike_is_not_a_change(self):
        self.sample(self.now, 10)
        with self.classify_spy() as classify:
            for tick in range(1, 25):
                self.sample(self.now + tick * TICK, 10 + 3 * CHANGE if tick % 3 == 0 else 10)
        self.assertEqual(classify.call_count, 0)

    def test_a_channel_appearing_or_vanishing_is_classified(self):
        self.device["entities"]["sensor.radar_g1_move_energy"] = {"gate": 1, "kind": "move"}
        self.states["sensor.radar_g1_move_energy"] = types.SimpleNamespace(state="10")
        self.sample(self.now, 10)
        with self.classify_spy() as classify:
            self.states.pop("sensor.radar_g1_move_energy")
            self.sample(self.now + TICK, 10)
        self.assertEqual(classify.call_count, 1)

    def test_the_heartbeat_is_the_longest_a_result_is_reused(self):
        self.sample(self.now, 10)
        with self.classify_spy() as classify:
            self.sample(self.now + HEARTBEAT - TICK, 10)
            self.assertEqual(classify.call_count, 0)
            self.sample(self.now + HEARTBEAT, 10)
            self.assertEqual(classify.call_count, 1)

    def test_changed_reference_periods_or_calibration_are_classified_at_once(self):
        profile = self.device["reference_profile"] = references.empty()
        self.sample(self.now, 10)
        changes = (
            lambda: references.observe(profile, {KEY: 10}, "present", "human", 1.0, self.now),
            lambda: self.device.__setitem__(
                "reference_profile", {"version": references.VERSION, "periods": []}
            ),
            lambda: self.device["auto"].__setitem__("calibration", {"present_bias": 0.2}),
            lambda: self.device["auto"].pop("filter"),
        )
        for number, change in enumerate(changes, 1):
            self.sample(self.now + number * TICK, 10)  # Reused: nothing changed.
            with self.classify_spy() as classify:
                change()
                self.sample(self.now + number * TICK + 1, 10)
            self.assertEqual(classify.call_count, 1, number)

    def test_a_present_external_source_decides_and_a_dropped_one_is_classified_again(self):
        self.sample(self.now, 10)
        sources = sys.modules["tuner_under_test.presence.sources"]
        estimate = {
            "state": "present",
            "confidence": 0.9,
            "score": 0.9,
            "presence_probability": 0.9,
            "active_gates": 0,
            "top_gates": [],
            "basis": "external",
            "model": "entity-sources-v2",
            "buffered": False,
            "sources": [],
        }
        with patch.object(sources, "estimate", return_value=estimate):
            self.sample(self.now + TICK, 10)
        self.assertEqual(self.device["auto"]["last_classification"]["basis"], "external")
        with self.classify_spy() as classify:
            self.sample(self.now + 2 * TICK, 10)
        self.assertEqual(classify.call_count, 1)
        self.assertNotEqual(self.device["auto"]["last_classification"]["basis"], "external")

    def test_a_reused_classification_stays_current_for_its_consumers(self):
        self.sample(self.now, 10)
        auto = self.device["auto"]
        classified = deepcopy(auto["last_classification"])
        segments = deepcopy(auto.get("segments", []))
        for number in range(1, 20):
            self.sample(self.now + number * TICK, 10)
        last = auto["last_classification"]
        self.assertEqual(last["timestamp"], self.now + 19 * TICK)
        self.assertEqual({**last, "timestamp": 0}, {**classified, "timestamp": 0})
        self.assertEqual(auto.get("segments", []), segments)
        with patch.object(autolabelling.time, "time", return_value=self.now + 19 * TICK + 1):
            self.assertEqual(autolabelling.auto_learning_summary(self.device)["last"], last)

    def test_the_baseline_stays_proportional_to_time_across_reused_ticks(self):
        self.run_ticks(HEARTBEAT)
        ticks = int(HEARTBEAT / TICK) + 1
        histogram = self.device["auto"]["all_histograms"][KEY]
        self.assertEqual(sum(histogram), ticks, "each tick counted, as when classified singly")

    def test_a_reused_tick_adds_its_own_reading_to_the_baseline_not_the_anchors(self):
        """Readings that wobble within ``CHANGE`` reuse one result; each still goes into the
        baseline as read (crediting the classified readings would smooth its tails)."""
        wobble = lambda elapsed: 10 if int(elapsed / TICK) % 2 == 0 else 12  # noqa: E731
        self.run_ticks(HEARTBEAT - TICK, wobble)
        histogram = self.device["auto"]["all_histograms"][KEY]
        ticks = int((HEARTBEAT - TICK) / TICK) + 1
        self.assertEqual((histogram[10], histogram[12]), (ticks - ticks // 2, ticks // 2))

    def test_a_tick_is_recorded_whether_or_not_it_was_classified(self):
        self.run_ticks(30)
        spans = sys.modules["tuner_under_test.history.spans"]
        with patch.object(time, "time", return_value=self.now + 30):
            covered = spans.coverage(spans.events(self.runtime, "a"))
        self.assertEqual(len(covered), 1)  # One unchanged reading, observed all along.
        self.assertAlmostEqual(covered[0][1] - covered[0][0], 30, places=2)
        self.assertEqual(self.runtime._live["a"], {KEY: 10.0})

    def test_nothing_is_reused_after_readings_stop_and_resume(self):
        self.sample(self.now, 10)
        self.sample(self.now + TICK, "unavailable")
        with self.classify_spy() as classify:
            self.sample(self.now + 2 * TICK, 10)
        self.assertEqual(classify.call_count, 1)

    def test_a_confirmed_state_survives_a_long_reuse(self):
        """The confirmation and the filter see no gap while the result is reused."""
        profile = self.device["reference_profile"] = references.empty()
        for tick in range(30):
            references.observe(
                profile, {KEY: 5}, "not_present", "human", 1.0, self.now - 400 + tick
            )
        for tick in range(30):
            references.observe(profile, {KEY: 70}, "present", "human", 1.0, self.now - 200 + tick)
        self.run_ticks(40, 80)
        first = self.device["auto"]["last_classification"]
        self.assertEqual(first["state"], "present")
        self.run_ticks(3 * HEARTBEAT, 80, start=40 + TICK)
        self.assertEqual(self.device["auto"]["last_classification"]["state"], "present")
        self.assertEqual(self.device["auto"]["segments"][-1]["state"], "present")
        self.assertEqual(
            len([s for s in self.device["auto"]["segments"] if s["state"] == "present"]), 1
        )


class FilterTests(unittest.TestCase):
    def estimate(self, temporal, now, seen=None):
        if seen is not None:
            temporal["seen"] = seen
        return inference.estimate_presence({KEY: 40}, {}, {KEY: [1] * 101}, temporal, now, [KEY])

    def temporal(self):
        return {"model": inference.MODEL, "timestamp": 100.0, "probability": 0.9}

    def test_a_filter_without_readings_for_longer_than_the_gap_starts_over(self):
        gapped = self.temporal()
        self.estimate(gapped, 100 + inference.MAX_GAP + 1)
        fresh = {}
        self.estimate(fresh, 1000.0)
        self.assertEqual(gapped["probability"], fresh["probability"])

    def test_time_the_result_was_reused_is_not_a_gap_and_leaves_the_filter_at_rest(self):
        held, gapped, ticked = self.temporal(), self.temporal(), self.temporal()
        self.estimate(held, 160.0, seen=158.0)
        self.estimate(gapped, 160.0)
        self.estimate(ticked, 158.0, seen=158.0)  # The reading before, then this one 2 s later.
        self.estimate(ticked, 160.0)
        self.assertEqual(held["probability"], ticked["probability"])
        self.assertNotEqual(held["probability"], gapped["probability"])
        self.assertEqual((held["timestamp"], held["seen"]), (160.0, 160.0))

    def test_holding_marks_the_filter_and_the_confirmation_as_seen(self):
        temporal, confirmation = self.temporal(), {"timestamp": 100.0}
        cadence = {}
        inference.classified(cadence, {KEY: 10.0}, 100.0, True)
        inference.hold(temporal, confirmation, 158.0)
        self.assertEqual((temporal["seen"], confirmation["seen"]), (158.0, 158.0))
        self.assertEqual(temporal["timestamp"], 100.0, "the last classification is still it")

    def test_a_confirmation_is_kept_across_reused_time_but_not_across_a_real_gap(self):
        result = {"label": "present", "score": 1.0, "model": inference.MODEL}
        kept = {
            "timestamp": 0.0,
            "model": inference.MODEL,
            "state": "present",
            "pending": "present",
        }
        kept["pending_since"] = 0.0
        inference.hold({}, kept, 58.0)
        self.assertEqual(inference.confirm_estimate(result, kept, 60.0), "present")
        lost = {"timestamp": 0.0, "model": inference.MODEL, "state": "present"}
        lost.update(pending="present", pending_since=0.0)
        self.assertEqual(inference.confirm_estimate(result, lost, 60.0), "unknown")


class CadenceTests(unittest.TestCase):
    def classified(self, values=None, now=0.0, rest=True):
        cadence = {}
        inference.classified(cadence, values or {KEY: 10.0}, now, rest)
        return cadence

    def test_nothing_classified_yet_is_due(self):
        self.assertTrue(inference.reclassify({}, {KEY: 10.0}, 0.0))

    def test_unchanged_readings_of_a_filter_at_rest_are_reused_until_the_heartbeat(self):
        cadence = self.classified()
        for now, expected in ((TICK, False), (HEARTBEAT - 0.1, False), (HEARTBEAT, True)):
            self.assertEqual(inference.reclassify(cadence, {KEY: 10.0}, now), expected, now)

    def test_a_channel_that_stays_moved_by_the_threshold_in_either_direction_is_due(self):
        for value, expected in (
            (10 + CHANGE - 0.5, False),
            (10 + CHANGE, True),
            (10 - CHANGE + 0.5, False),
            (10 - CHANGE, True),
        ):
            cadence = self.classified()
            self.assertFalse(inference.reclassify(cadence, {KEY: value}, 1.0), "not yet")
            later = inference.reclassify(cadence, {KEY: value}, 1.0 + SUSTAIN)
            self.assertEqual(later, expected, value)

    def test_a_move_that_comes_back_starts_the_time_again(self):
        cadence = self.classified()
        far, near = {KEY: 10 + CHANGE}, {KEY: 10.0}
        self.assertFalse(inference.reclassify(cadence, far, 1.0))
        self.assertFalse(inference.reclassify(cadence, near, 1.0 + SUSTAIN - 1))
        self.assertFalse(inference.reclassify(cadence, far, 1.0 + SUSTAIN), "it began again")
        self.assertFalse(inference.reclassify(cadence, far, 1.0 + 2 * SUSTAIN - 0.1))
        self.assertTrue(inference.reclassify(cadence, far, 1.0 + 2 * SUSTAIN))
        inference.classified(cadence, far, 2.0 + 2 * SUSTAIN, True)
        self.assertFalse(inference.reclassify(cadence, {KEY: 10 + 2 * CHANGE}, 3.0 + 2 * SUSTAIN))

    def test_a_different_set_of_channels_is_due(self):
        cadence = self.classified({KEY: 10.0, "g1_move": 5.0})
        self.assertTrue(inference.reclassify(cadence, {KEY: 10.0}, 1.0))
        self.assertTrue(inference.reclassify(cadence, {KEY: 10.0, "g2_move": 5.0}, 1.0))

    def test_a_classification_not_at_rest_is_due_on_every_tick(self):
        self.assertTrue(inference.reclassify(self.classified(rest=False), {KEY: 10.0}, 1.0))

    def test_a_result_is_at_rest_only_when_nothing_is_pending_moving_or_timed(self):
        result = {"label": "present", "presence_probability": 0.9, "score": 1.0}
        rest = inference.at_rest
        self.assertTrue(rest(0.9, result, {}, "present", 100.0))
        self.assertFalse(rest(0.9, result, {}, "unknown", 100.0), "awaiting confirmation")
        with patch.object(inference, "SETTLED", 0.25):  # Exact in binary: 0.75 - 0.5.
            moved = {**result, "presence_probability": 0.75}
            self.assertFalse(rest(0.5, moved, {}, "present", 100.0), "moving by the limit")
            self.assertTrue(rest(0.5000001, moved, {}, "present", 100.0))
        quiet = {**result, "score": 0.0}
        self.assertFalse(rest(0.9, quiet, {"supported_at": 95.0}, "present", 100.0), "timed")
        self.assertTrue(rest(0.9, quiet, {"supported_at": 87.0}, "present", 100.0))
        self.assertTrue(rest(0.9, result, {"supported_at": 99.0}, "present", 100.0))


def load_replay_tool():
    path = Path(harness.__file__).resolve().parents[1] / "tools" / "replay_autolabels.py"
    spec = importlib.util.spec_from_file_location("replay_autolabels", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def steady_model(label="unknown"):
    """A model whose answer never changes, with the integration's event-driven bookkeeping."""

    def estimate(values, references, background, temporal, now, expected, calibration=None):
        temporal["timestamp"] = now
        return {
            "label": label,
            "confidence": 0.0,
            "presence_probability": 0.5,
            "score": 0.0,
            "model": "steady",
        }

    names = ("confirm_estimate", "reclassify", "hold", "classified", "at_rest")
    return types.SimpleNamespace(
        estimate_presence=estimate, **{name: getattr(inference, name) for name in names}
    )


class ReplayTests(unittest.TestCase):
    """The replay tool feeds an event-driven model as the integration does."""

    def rows(self, count, value):
        return [(1000.0 + 6 * i, {KEY: value(i)}, None) for i in range(count)]

    def test_a_model_without_event_driven_classification_classifies_every_reading(self):
        tool = load_replay_tool()
        legacy = types.SimpleNamespace(estimate_presence=inference.estimate_presence)
        replay = tool.Replay(legacy, [KEY])
        for ts, values, _ in self.rows(30, lambda i: 10):
            replay.step(ts, values)
        self.assertEqual(replay.classifications, 30)

    def test_steady_readings_are_classified_once_per_heartbeat(self):
        tool = load_replay_tool()
        replay = tool.Replay(steady_model(), [KEY])
        for ts, values, _ in self.rows(41, lambda i: 10):
            replay.step(ts, values)
        # 240 s: the first reading, then every 60 s (10 readings of 6 s).
        self.assertEqual(replay.classifications, 5)

    def test_readings_that_keep_moving_are_classified_as_soon_as_the_move_has_lasted(self):
        tool = load_replay_tool()
        replay = tool.Replay(inference, [KEY])
        step = int(CHANGE)  # Each reading is a change away from the one before.
        for ts, values, _ in self.rows(20, lambda i: 10 + step * i):
            replay.step(ts, values)
        # Rows are 6 s apart, longer than SUSTAIN: the second reading of each move counts.
        self.assertEqual(replay.classifications, 10)
        self.assertGreater(6, SUSTAIN)

    def test_the_baseline_counts_every_reading_once_whether_reused_or_not(self):
        tool = load_replay_tool()
        replay = tool.Replay(steady_model(), [KEY])
        for ts, values, _ in self.rows(11, lambda i: 10):
            replay.step(ts, values)
        self.assertEqual(replay.classifications, 2)
        self.assertEqual(sum(replay.background[KEY]), 11, "each reading once, reused or not")

    def test_a_reused_reading_joins_the_baseline_as_read(self):
        tool = load_replay_tool()
        replay = tool.Replay(steady_model(), [KEY])
        for ts, values, _ in self.rows(11, lambda i: 10 + 2 * (i % 2)):
            replay.step(ts, values)
        self.assertEqual(replay.classifications, 2)
        self.assertEqual((replay.background[KEY][10], replay.background[KEY][12]), (6, 5))

    def test_a_reused_reading_answers_like_the_classification_it_reuses(self):
        tool = load_replay_tool()
        replay = tool.Replay(inference, [KEY])
        answers = [replay.step(ts, values) for ts, values, _ in self.rows(6, lambda i: 10)]
        self.assertEqual(len(set(answers)), 1)

    def test_evaluate_reports_how_many_readings_were_classified(self):
        tool = load_replay_tool()
        rows = [(ts, values, "not_present") for ts, values, _ in self.rows(40, lambda i: 10)]
        report = tool.evaluate(rows, [KEY], steady_model(), "causal")
        self.assertEqual(report["readings"], 40)
        self.assertEqual(report["classifications"], 4)
