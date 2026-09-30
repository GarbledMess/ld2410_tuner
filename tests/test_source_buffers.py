"""External presence boundaries never become provisional learning evidence."""

import sys
import unittest
from copy import deepcopy
from unittest.mock import patch

import pytest
import test_sources as source_harness
import test_tuner as harness
from test_sources import bermuda, boolean, service, sources

source_labels = sys.modules["tuner_under_test.history.source_labels"]


class BufferTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    sample = harness.RuntimeTests.sample
    entity = source_harness.SourceTests.entity

    def configure(self, **changes):
        config = {**sources.DEFAULTS, "sources": [boolean()], **changes}
        self.runtime.configure_presence_sources("a", config)
        self.entity("binary_sensor.camera_person", "on")
        return config

    def tick(self, second, state="on"):
        self.entity("binary_sensor.camera_person", state)
        self.sample(self.now + second, 70)

    def rows(self):
        return list(self.runtime._iter_history_samples(self.device, include_auto=True))

    def present_times(self):
        return [round(ts - self.now, 3) for ts, row in self.rows() if row[-2] == 1]

    def test_live_period_only_confirms_interior_and_never_changes_raw_readings(self):
        self.configure()
        for second in range(0, 31, 6):
            self.tick(second)
        assert self.present_times() == [12, 18]
        assert [row[-2] for _, row in self.rows()] == [0, 0, 1, 1, 0, 0]
        before = [(ts, row[:-2]) for ts, row in self.rows()]
        self.tick(36, "off")
        assert self.present_times() == [12, 18, 24]
        assert before == [(ts, row[:-2]) for ts, row in self.rows()][:6]
        assert self.device["auto"]["observations"]["present"] == 3
        assert all(row[-1] == 90 for _, row in self.rows() if row[-2] == 1)
        with patch.object(service, "fit_thresholds", return_value={}) as fit:
            service._fit_history(self.runtime, "a", {"g0_move": "number.threshold"}, {})
        assert [
            (round(ts - self.now), state, confidence)
            for ts, _, state, confidence in fit.call_args.args[3]
        ] == [(12, "present", 0.9), (18, "present", 0.9), (24, "present", 0.9)]

    def test_compressed_tail_can_be_confirmed_without_mutating_a_learning_snapshot(self):
        self.configure()
        for second in range(0, 19, 6):
            self.tick(second)
        self.runtime._flush_history_block("a")
        original = deepcopy(self.device["history"])
        view = self.runtime._history_view("a")
        self.runtime._history_cache["old"] = "cached"
        self.tick(24)
        self.tick(30, "off")
        assert self.present_times() == [12, 18]
        assert self.device["history"] != original
        assert view.data["devices"]["a"]["history"] == original
        assert not self.runtime._history_cache
        self.runtime._flush_history_block("a")
        assert self.present_times() == [12, 18]

    def test_manual_labels_keep_priority_including_trimmed_boundaries(self):
        self.configure()
        for second in range(0, 31, 6):
            self.tick(second)
        for second, state in ((0, "present"), (12, "not_present"), (18, "unknown")):
            self.runtime.label_history_range(
                "a", self.now + second - 1, self.now + second + 1, state
            )
        with patch.object(service, "fit_thresholds", return_value={}) as fit:
            service._fit_history(self.runtime, "a", {"g0_move": "number.threshold"}, {})
        assert [(round(ts - self.now), state) for ts, _, state in fit.call_args.args[0]] == [
            (0, "present"),
            (12, "not_present"),
        ]
        assert not fit.call_args.args[3]

    def test_unknown_closes_the_period_without_labelling_the_tail_absent(self):
        self.configure(mark_not_present=True)
        for second in range(0, 31, 6):
            self.tick(second)
        self.tick(36, "unavailable")
        assert self.present_times() == [12, 18, 24]
        assert self.rows()[-2][1][-2:] == bytes((0, 0))
        assert not self.runtime._source_runtime
        self.tick(42)
        self.tick(48, "off")
        assert self.present_times() == [12, 18, 24]
        assert self.rows()[-1][1][-2:] == bytes((2, 90))

    def test_or_handoff_between_boolean_and_bermuda_is_continuous(self):
        self.configure(sources=[boolean(), bermuda()])
        self.entity("sensor.phone_area", "Office", area_id="office")
        for second in (0, 6, 12):
            self.tick(second)
        self.entity("sensor.phone_area", "Bedroom", area_id="bedroom")
        for second in (18, 24, 30):
            self.tick(second, "off")
        assert self.present_times() == [12, 18]
        assert self.runtime._source_runtime["a"]["start"] == self.now

    def test_summary_reports_buffering_without_mutating_or_confirming_history(self):
        self.configure()
        self.tick(0)
        with patch("time.time", return_value=self.now):
            summary = sources.summary(self.runtime, self.device)
        assert summary["buffering"] and summary["state"] == "unknown"
        assert summary["raw_state"] == "present"
        assert not self.present_times()
        for second in (6, 12, 18, 24):
            self.tick(second)
        before = deepcopy(self.rows())
        with patch("time.time", return_value=self.now + 26):
            summary = sources.summary(self.runtime, self.device)
        assert not summary["buffering"] and summary["state"] == "present"
        assert self.rows() == before

    def test_config_change_and_recording_pause_discard_unconfirmed_tail(self):
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
        assert self.present_times() == [24]

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
                    self.sample(self.now + 8, float("nan"))
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
                assert not self.present_times()

    async def test_settings_persist_and_legacy_settings_receive_defaults(self):
        config = self.configure(start_buffer_seconds=12.5, end_buffer_seconds=8)
        await self.runtime.async_save()
        saved = self.runtime.store.async_save.call_args.args[0]
        assert saved["devices"]["a"]["presence_sources"] == config
        legacy = {"sources": [boolean()], "mark_not_present": False, "confidence": 90}
        assert sources.validate(legacy) == {**sources.DEFAULTS, **legacy}
        self.device["presence_sources"] = legacy
        assert sources.settings(self.device)["end_buffer_seconds"] == 10


@pytest.mark.parametrize("duration", [6, 12, 18, 20])
def test_empty_trimmed_intervals_never_enter_learning(duration):
    case = BufferTests()
    case.setUp()
    try:
        case.configure()
        for second in range(0, duration, 2):
            case.tick(second)
        case.tick(duration, "off")
        assert case.present_times() == []
        assert all(row[-2:] == bytes((0, 0)) for _, row in case.rows())
        assert case.rows(), "Raw samples must remain available"
    finally:
        case.doCleanups()


@pytest.mark.parametrize(
    "start,end,expected",
    [(0, 0, [0, 6, 12, 18, 24]), (0, 10, [0, 6, 12, 18]), (10, 0, [12, 18, 24]), (12.5, 8, [18])],
)
def test_asymmetric_and_zero_buffers(start, end, expected):
    case = BufferTests()
    case.setUp()
    try:
        case.configure(start_buffer_seconds=start, end_buffer_seconds=end)
        for second in range(0, 25, 6):
            case.tick(second)
        case.tick(30, "off")
        assert case.present_times() == expected
    finally:
        case.doCleanups()


@pytest.mark.parametrize("field", ["start_buffer_seconds", "end_buffer_seconds"])
@pytest.mark.parametrize("value", [-1, 3601, True, "10", None, float("nan"), float("inf")])
def test_invalid_buffers_are_rejected(field, value):
    with pytest.raises(ValueError, match="buffers"):
        sources.validate({**sources.DEFAULTS, field: value})


def test_confirmation_leaves_other_labels_and_malformed_blocks_alone():
    row = bytes([70] * source_labels.WIDTH)
    pending = [(1, row + bytes((2, 90))), (2, row + bytes((1, 80))), (3, row)]
    assert source_labels._confirm_pending(pending, 0, 4, 90) == 0
    block = {"start": 1, "count": 1, "version": 3, "data": "bad"}
    assert source_labels._confirm_block(block, 0, 4, 90) == (block, 0)
