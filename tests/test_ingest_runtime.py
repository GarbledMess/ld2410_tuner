"""Delivered HA events preserve precise energies through the sole native day-store path."""

import datetime
import time
import types
import unittest
from unittest.mock import patch

import test_tuner as harness
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history import dayfile, daystore, ingest

ENERGY = "sensor.radar_g0_move_energy"
START = 1_760_486_400.0
DAY = daystore.date_of(START)


def event(entity_id, state, timestamp=None):
    new = types.SimpleNamespace(state=state)
    if timestamp is not None:
        new.last_updated = datetime.datetime.fromtimestamp(timestamp, datetime.UTC)
    return types.SimpleNamespace(data={"entity_id": entity_id, "new_state": new})


class AcquisitionTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def setUp(self):
        harness.RuntimeTests.setUp(self)
        self.registry.async_get = self.registry.entities.get
        self.connect(ENERGY)

    def connect(self, entity):
        self.registry.entities[entity] = types.SimpleNamespace(
            entity_id=entity, device_id="a", domain="sensor"
        )

    def send(self, offset, value, entity=ENERGY):
        self.runtime.handle_state_change(event(entity, str(value), START + offset))

    def rows(self, offset):
        day = self.runtime._days.day("a", DAY, until=START + offset)
        if day is None:
            return []
        return [
            (t - START, e - START, day.vector(i))
            for i, (t, e) in enumerate(zip(day.times, day.ends, strict=True))
        ]

    def tick(self, offset, value):
        self.states[ENERGY] = types.SimpleNamespace(state=str(value))
        with patch.object(time, "time", return_value=START + offset):
            self.runtime.sample_devices()

    def test_fifty_millisecond_float_pulse_survives_without_activation_flag(self):
        assert not hasattr(ingest, "ACTIVE")
        for offset, value in [(1.125, 0), (1.175, 40.125), (1.225, 0)]:
            self.send(offset, value)
        rows = self.rows(2)
        for row, expected in zip(rows, [1.125, 1.175, 1.225], strict=True):
            self.assertAlmostEqual(row[0], expected, places=6)
        assert rows[1][2][0] == 40.125
        self.assertAlmostEqual(rows[1][1] - rows[1][0], 0.05, places=6)

    def test_session_baseline_contains_all_current_gates(self):
        self.runtime._live["a"] = {"g0_move": 10, "g0_still": 20}
        self.send(1, 11)
        assert self.rows(2)[0][2][0] == 11
        assert self.runtime._days.current("a")[HISTORY_KEYS.index("g0_still")] == 20

    def test_unavailable_and_invalid_values_make_only_the_delivered_gate_missing(self):
        other = "sensor.radar_g1_move_energy"
        self.connect(other)
        self.send(1, 30)
        self.send(2, 25, other)
        for offset, value in [(3, "unavailable"), (4, "140"), (5, "unknown"), (6, "bad")]:
            self.send(offset, value)
        assert [
            (r[0], (r[2][HISTORY_KEYS.index("g0_move")], r[2][HISTORY_KEYS.index("g1_move")]))
            for r in self.rows(8)
        ] == [(1, (30, None)), (2, (30, 25)), (3, (None, 25))]
        assert self.runtime._live["a"]["g0_move"] == 30

    def test_invalid_first_reading_starts_no_coverage(self):
        for value in ("unavailable", "unknown", "bad", "140"):
            self.send(1, value)
        assert self.rows(2) == []

    def test_missing_event_timestamp_uses_receipt_clock(self):
        with patch.object(time, "time", return_value=START + 1.5):
            self.runtime.handle_state_change(event(ENERGY, "30"))
        assert self.rows(2)[0][0] == 1.5

    def test_disabled_and_unknown_devices_store_nothing(self):
        self.device["recording_enabled"] = False
        self.send(1, 30)
        self.registry.entities[ENERGY].device_id = "missing"
        self.send(2, 30)
        assert self.rows(3) == []

    def test_stable_polls_extend_duration_without_duplicate_vectors_or_json_history(self):
        self.send(0, 20)
        for offset in range(2, 62, 2):
            self.tick(offset, 20)
        rows = self.rows(60)
        assert [(r[0], r[1], r[2][0]) for r in rows] == [(0, 60, 20)]
        assert not self.device.get("history")
        assert not self.device.get("guess_runs")
        assert not self.runtime._history_runtime["a"].get("samples")

    def test_histograms_use_duration_and_label_edit_does_not_double_count(self):
        self.device["history_labels"] = [
            {"start": START - 10, "end": START + 100, "state": "present"}
        ]
        self.send(0, 19.5)
        for offset in range(2, 62, 2):
            self.tick(offset, 19.5)
        processed = self.runtime._history_runtime["a"]["last_sample"] - (START + 2)
        before = self.device["histograms"]["g0_move"]["present"]
        assert processed > 0
        self.assertAlmostEqual(before[20], processed / 6)
        assert sum(before) == before[20]
        with patch.object(time, "time", return_value=START + 60):
            self.runtime.label_history_range("a", START - 10, START + 100, "present")
        after = self.device["histograms"]["g0_move"]["present"]
        self.assertAlmostEqual(after[20], 10)
        assert sum(after) == after[20]

    def test_poll_then_event_uses_one_nonoverlapping_storage_path(self):
        for offset in (0, 6, 12):
            self.tick(offset, 20)
        self.send(13, 30)
        for offset in (14, 20):
            self.tick(offset, 30)
        assert [(r[0], r[1], r[2][0]) for r in self.rows(20)] == [(0, 13, 20), (13, 20, 30)]
        assert not self.device.get("history")

    def test_silence_does_not_infer_an_end(self):
        self.send(10, 20)
        self.send(20, 20)
        self.send(500, 20)
        assert [(r[0], r[1]) for r in self.rows(501)] == [(10, 501)]

    def test_backward_change_clamps_to_last_change_instead_of_reordering(self):
        self.send(10, 20)
        self.send(20, 25)
        self.send(15, 30)
        assert [(r[0], r[1], r[2][0]) for r in self.rows(21)] == [(10, 20, 20), (20, 21, 30)]

    def test_pause_block_recovery_and_removal_end_coverage_without_bridging_resume(self):
        for reason in ("pause", "blocked", "recovery", "removed"):
            with self.subTest(reason=reason):
                self.runtime._days = daystore.DayStore(self.runtime._days.root / reason)
                self.runtime.data["devices"]["a"] = self.device
                self.device["recording_enabled"] = True
                self.device.pop("configuration_recovery", None)
                self.runtime._storage_status.clear()
                self.send(1, 20)
                if reason == "pause":
                    with patch.object(time, "time", return_value=START + 4):
                        self.runtime.set_recording("a", False)
                elif reason == "removed":
                    self.registry.entities.clear()
                    with patch.object(time, "time", return_value=START + 4):
                        self.runtime.refresh_devices(self.registry)
                else:
                    if reason == "blocked":
                        self.runtime._storage_status["blocked"] = True
                    else:
                        self.device["configuration_recovery"] = {"status": "running"}
                    self.send(4, 21)
                assert self.runtime._days.current("a") is None
                self.runtime.data["devices"]["a"] = self.device
                self.device["recording_enabled"] = True
                self.device.pop("configuration_recovery", None)
                self.runtime._storage_status.clear()
                self.connect(ENERGY)
                self.send(9, 30)
                assert [(r[0], r[1]) for r in self.rows(10)] == [(1, 4), (9, 10)]

    def test_stop_ends_runtime_coverage(self):
        self.send(1, 20)
        ingest.stop(self.runtime, START + 4)
        assert [(r[0], r[1]) for r in self.rows(10)] == [(1, 4)]

    def test_four_thousand_changes_flush_and_reopen_safely(self):
        for index in range(4000):
            self.send(index, index % 100 + 0.5)
            if index % 500 == 0:
                self.runtime._days.flush(now=START + index)
        ingest.stop(self.runtime, START + 4000)
        self.runtime._days.flush(now=START + 4000)
        log = self.runtime._days.folder("a") / f"{DAY.isoformat()}.log"
        records = dayfile.decode_log(log.read_bytes(), START)
        assert sum(isinstance(r, dayfile.Change) for r in records) == 4000
        reopened = daystore.DayStore(self.runtime._days.root)
        reopened.open(now=START + 4001)
        day = reopened.day("a", DAY, until=START + 4001)
        assert len(day.times) == 4000
        assert sum(e - t for t, e in zip(day.times, day.ends, strict=True)) == 4000
        assert day.vector(3999)[0] == 99.5
