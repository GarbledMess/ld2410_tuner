"""External presence boundaries never become provisional learning evidence.

Buffered external presence is confirmed afterwards, over the interior of the source's positive
period only, as a recorded guess span (history/source_labels.py). Intervals are in seconds
from the first poll.
"""

import sys
import time
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
import test_sources as source_harness
import test_tuner as harness
from test_sources import bermuda, boolean, sources

source_labels = sys.modules["tuner_under_test.history.source_labels"]
spans = sys.modules["tuner_under_test.history.spans"]
event_fitting = sys.modules["tuner_under_test.calibration.event_fitting"]
EXTERNAL = harness.guesses.EXTERNAL


def joined(runs, origin):
    """Present external runs as merged ``(start, end)`` offsets from ``origin``."""
    found = []
    for run in runs:
        if run[2] != "present" or run[4] != EXTERNAL:
            continue
        start, end = round(run[0] - origin, 3), round(run[1] - origin, 3)
        if found and found[-1][1] == start:
            found[-1] = (found[-1][0], end)
        else:
            found.append((start, end))
    return found


class BufferTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    sample = harness.RuntimeTests.sample
    entity = source_harness.SourceTests.entity

    def setUp(self):
        harness.RuntimeTests.setUp(self)
        self.now = self.clock = float(int(self.now))  # Recorded times are whole milliseconds.
        clock = patch.object(time, "time", side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)

    def configure(self, **changes):
        config = {**sources.DEFAULTS, "sources": [boolean()], **changes}
        self.runtime.configure_presence_sources("a", config)
        self.entity("binary_sensor.camera_person", "on")
        return config

    def tick(self, second, state="on", value=70):
        self.clock = self.now + second
        self.entity("binary_sensor.camera_person", state)
        self.sample(self.clock, value)

    def present(self, runtime=None):
        return joined(harness.guesses.of(runtime or self.runtime, "a").runs, self.now)

    def energies(self):
        return [(c.start, c.end, c.values) for c in spans.events(self.runtime, "a")]

    def measured(self):
        found = event_fitting.assess(self.runtime, "a", {"g0_move": 50}, quality_context=False)
        return {
            source: (
                found[source]["duration"]["present_seconds"],
                found[source]["duration"]["empty_seconds"],
            )
            for source in ("human", "automatic")
        }

    def test_live_period_only_confirms_interior_and_never_changes_raw_readings(self):
        self.configure()
        for second in range(0, 31, 6):
            self.tick(second)
        assert self.present() == [(10, 20)]  # The 10 s start and end buffers are trimmed.
        before = self.energies()
        self.tick(36, "off")
        assert self.present() == [(10, 26)]
        assert self.energies()[: len(before) - 1] == before[:-1]
        assert {values[0] for _start, _end, values in self.energies()} == {70}
        assert self.device["auto"]["observations"]["present"] == pytest.approx(16 / 6)
        runs = harness.guesses.of(self.runtime, "a").runs
        assert {run[2:] for run in runs if run[2] == "present"} == {("present", 18, EXTERNAL)}
        assert self.measured()["automatic"] == (16, 0)

    def test_confirmation_never_reaches_a_view_taken_before_it(self):
        self.configure()
        for second in range(0, 19, 6):
            self.tick(second)
        view = self.runtime._history_view("a")
        self.runtime._history_cache["old"] = "cached"
        self.tick(24)
        self.tick(30, "off")
        assert self.present() == [(10, 20)]
        assert self.present(view) == []
        assert not self.runtime._history_cache

    async def test_manual_labels_keep_priority_including_trimmed_boundaries(self):
        self.configure()
        for second in range(0, 31, 6):
            self.tick(second)
        for second, state in ((0, "present"), (12, "not_present"), (18, "unknown")):
            await self.runtime.label_history_range(
                "a", self.now + second - 1, self.now + second + 1, state
            )
        measured = self.measured()
        assert measured["human"] == (1, 2)  # [0, 1) present (recorded from 0); [11, 13) empty.
        assert measured["automatic"] == (6, 0)  # [10, 20) less the labelled [11, 13), [17, 19).

    def test_unknown_closes_the_period_without_labelling_the_tail_absent(self):
        self.configure(mark_not_present=True)
        for second in range(0, 31, 6):
            self.tick(second)
        self.tick(36, "unavailable")
        assert self.present() == [(10, 26)]
        assert not self.runtime._source_runtime
        self.tick(42)
        self.tick(48, "off")
        assert self.present() == [(10, 26)]
        runs = harness.guesses.of(self.runtime, "a").runs
        absent = [run for run in runs if run[2] == "not_present"]
        assert [run[0] - self.now for run in absent] == [48]
        assert absent[0][2:] == ("not_present", 18, EXTERNAL)

    def test_or_handoff_between_boolean_and_bermuda_is_continuous(self):
        self.configure(sources=[boolean(), bermuda()])
        self.entity("sensor.phone_area", "Office", area_id="office")
        for second in (0, 6, 12):
            self.tick(second)
        self.entity("sensor.phone_area", "Bedroom", area_id="bedroom")
        for second in (18, 24, 30):
            self.tick(second, "off")
        assert self.present() == [(10, 20)]
        assert self.runtime._source_runtime["a"]["start"] == self.now

    def test_summary_reports_buffering_without_mutating_or_confirming_history(self):
        self.configure()
        self.tick(0)
        summary = sources.summary(self.runtime, self.device)
        assert summary["buffering"]
        assert summary["state"] == "unknown"
        assert summary["raw_state"] == "present"
        assert not self.present()
        for second in (6, 12, 18, 24):
            self.tick(second)
        before = deepcopy(self.energies())
        self.clock = self.now + 26
        summary = sources.summary(self.runtime, self.device)
        assert not summary["buffering"]
        assert summary["state"] == "present"
        assert self.energies()[:-1] == before[:-1]

    async def test_config_change_and_recording_pause_discard_unconfirmed_tail(self):
        self.configure()
        self.tick(0)
        self.tick(6)
        self.runtime.set_recording("a", False)
        self.runtime.set_recording("a", True)
        self.tick(12)
        self.tick(18)
        self.configure(start_buffer_seconds=0)
        self.tick(24)
        self.tick(30)
        self.tick(36, "off")
        assert self.present() == [(24, 26)]

    def test_interrupted_sampling_restarts_the_buffer(self):
        for reason in ("long_gap", "missing_energy", "storage_blocked", "recovery", "restart"):
            with self.subTest(reason=reason):
                self.runtime.clear_samples("a")
                self.configure()
                self.tick(0)
                self.tick(6)
                if reason == "long_gap":
                    self.tick(24)
                elif reason == "restart":
                    self.runtime._source_runtime.clear()
                elif reason == "missing_energy":
                    self.tick(8, value=float("nan"))
                elif reason == "storage_blocked":
                    self.runtime._storage_status["blocked"] = True
                    self.tick(8)
                    self.runtime._storage_status.clear()
                else:
                    self.device["configuration_recovery"] = {"status": "running"}
                    self.tick(8)
                    self.device.pop("configuration_recovery")
                self.tick(30)
                self.tick(36, "off")
                assert not self.present()

    async def test_settings_persist_and_legacy_settings_receive_defaults(self):
        config = self.configure(start_buffer_seconds=12.5, end_buffer_seconds=8)
        await self.runtime.async_save()
        saved = self.runtime.store.async_save.call_args.args[0]
        assert saved["devices"]["a"]["presence_sources"] == config
        legacy = {"sources": [boolean()], "mark_not_present": False, "confidence": 90}
        assert sources.validate(legacy) == {**sources.DEFAULTS, **legacy}
        self.device["presence_sources"] = legacy
        assert sources.settings(self.device)["end_buffer_seconds"] == 10


def run_case(body):
    case = BufferTests()
    case._setupAsyncioRunner()
    try:
        case.setUp()
        body(case)
    finally:
        try:
            case._asyncioRunner.run(case.asyncTearDown())
        finally:
            try:
                assert case.doCleanups(), "Manual fixture cleanup failed"
            finally:
                case._tearDownAsyncioRunner()


@pytest.mark.parametrize("duration", [6, 12, 18, 20])
def test_empty_trimmed_intervals_never_enter_learning(duration):
    def body(case):
        case.configure()
        for second in range(0, duration, 2):
            case.tick(second)
        case.tick(duration, "off")
        assert case.present() == []
        assert case.energies(), "Raw readings must remain available"

    run_case(body)


@pytest.mark.parametrize(
    "start,end,expected",
    [(0, 0, [(0, 30)]), (0, 10, [(0, 20)]), (10, 0, [(10, 30)]), (12.5, 8, [(12.5, 22)])],
)
def test_asymmetric_and_zero_buffers(start, end, expected):
    def body(case):
        case.configure(start_buffer_seconds=start, end_buffer_seconds=end)
        for second in range(0, 25, 6):
            case.tick(second)
        case.tick(30, "off")
        assert case.present() == expected

    run_case(body)


@pytest.mark.parametrize("field", ["start_buffer_seconds", "end_buffer_seconds"])
@pytest.mark.parametrize("value", [-1, 3601, True, "10", None, float("nan"), float("inf")])
def test_invalid_buffers_are_rejected(field, value):
    with pytest.raises(ValueError, match="buffers"):
        sources.validate({**sources.DEFAULTS, field: value})


class ConfirmTests(unittest.TestCase):
    """``confirm_presence`` fills only time the radar left without a guess."""

    def setUp(self):
        from tuner_under_test.history import dayfile, daystore

        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runtime = harness.mod.TunerRuntime(None, None, {"devices": {"a": {}}})
        self.runtime._schedule_save = lambda: None
        self.runtime._days = daystore.DayStore(Path(folder.name))
        for record in (
            dayfile.Change(1, {0: 70}),
            dayfile.Guess(1, "not_present", 18, 1),
            dayfile.Guess(2, "present", 16, 1),
            dayfile.Guess(3, None, 0, 0),
        ):
            self.runtime._days.append("a", record)

    def runs(self):
        return [run[:3] + run[4:] for run in harness.guesses.of(self.runtime, "a").runs]

    def test_time_that_already_has_a_guess_keeps_it(self):
        with patch.object(time, "time", return_value=10):
            source_labels.confirm_presence(self.runtime, "a", 0, 4, 90)
        assert self.runs() == [
            (0, 1, "present", EXTERNAL),  # Unrecorded, so it trains nothing (below).
            (1, 2, "not_present", 1),
            (2, 3, "present", 1),
            (3, 4, "present", EXTERNAL),
        ]
        observations = self.runtime.data["devices"]["a"]["auto"]["observations"]
        assert observations == {"present": pytest.approx(1 / 6)}  # Only recorded [3, 4).

    def test_an_empty_or_fully_guessed_range_changes_nothing(self):
        before = self.runs()
        with patch.object(time, "time", return_value=10):
            source_labels.confirm_presence(self.runtime, "a", 4, 4, 90)
            source_labels.confirm_presence(self.runtime, "a", 1, 3, 90)
        assert self.runs() == before
        assert "auto" not in self.runtime.data["devices"]["a"]


def test_manual_buffer_case_releases_its_process_transport_patch():
    from tuner_under_test.worker.supervisor import Supervisor

    original = Supervisor._process
    run_case(lambda case: None)
    assert Supervisor._process is original
