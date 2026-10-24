"""Delivered HA events preserve pulse timing and whole-percentage gate energies."""

import asyncio
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

    def test_fifty_millisecond_pulse_survives_whole_percentage_quantization(self):
        assert not hasattr(ingest, "ACTIVE")
        for offset, value in [(1.125, 0), (1.175, 40.125), (1.225, 0)]:
            self.send(offset, value)
        rows = self.rows(2)
        for row, expected in zip(rows, [1.125, 1.175, 1.225], strict=True):
            self.assertAlmostEqual(row[0], expected, places=6)
        assert rows[1][2][0] == 41
        assert isinstance(self.runtime._days.day("a", DAY, until=START + 2).columns[0], bytes)
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
        assert self.runtime._live["a"] == {"g1_move": 25}

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

    async def test_stable_observations_survive_worker_flush_and_restart(self):
        from tuner_under_test.worker import flushing

        for value in (0, 100):
            self.runtime._days = daystore.DayStore(self.runtime._days.root / str(value))
            self.send(1, value)
            self.tick(2, value)
            with patch.object(time, "time", return_value=START + 3):
                await flushing.flush(self.runtime)
            self.tick(3600, value)
            with patch.object(time, "time", return_value=START + 3610):
                await flushing.flush(self.runtime)
            reopened = daystore.DayStore(self.runtime._days.root)
            await asyncio.to_thread(reopened.open, START + 3620)
            day = reopened.day("a", DAY, until=START + 3620)
            assert list(day.times) == [START + 1]
            assert list(day.ends) == [START + 3600]
            assert day.vector(0)[0] == value
            assert len(list(self.runtime._days.root.rglob("*.log"))) == 1

    def test_all_unavailable_poll_schedules_missing_readings_for_flush(self):
        self.send(1, 20)
        scheduled = []
        self.runtime._schedule_flush = lambda: scheduled.append(True)
        self.tick(2, "unavailable")
        assert self.runtime._days.current("a")[0] is None
        assert scheduled == [True]

    def test_stable_polls_extend_duration_without_duplicate_vectors_or_json_history(self):
        self.send(0, 20)
        for offset in range(2, 62, 2):
            self.tick(offset, 20)
        rows = self.rows(60)
        assert [(r[0], r[1], r[2][0]) for r in rows] == [(0, 60, 20)]
        assert not self.device.get("history")
        assert not self.device.get("guess_runs")
        assert not self.runtime._history_runtime["a"].get("samples")

    async def test_histograms_use_duration_and_label_edit_does_not_double_count(self):
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
            await self.runtime.label_history_range("a", START - 10, START + 100, "present")
            await harness.settled(self.runtime)
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

    async def test_pause_block_recovery_and_removal_end_coverage_without_bridging_resume(self):
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
        assert day.vector(3999)[0] == 100
        assert all(isinstance(column, bytes) for column in day.columns)


class ReferenceBatchTests(AcquisitionTests):
    def test_live_reference_evidence_is_applied_once_a_minute(self):
        """Folding every 5 s re-encoded the open period and invalidated the classifier's
        reference pool each time; references are long-term, so a minute's batch is enough."""
        from tuner_under_test.history import live

        self.device["history_labels"] = [{"start": START, "end": START + 600, "state": "present"}]
        for offset in range(0, 52, 2):
            self.tick(offset, 30 + offset % 7)
        before = repr(self.device.get("reference_profile", {}).get("periods"))
        self.tick(52, 31)
        self.tick(56, 32)
        assert repr(self.device.get("reference_profile", {}).get("periods")) == before
        for offset in range(58, 70, 2):
            self.tick(offset, 33)
        periods = self.device["reference_profile"]["periods"]
        human = [p for p in periods if p["source"] == "human"]
        assert human and human[-1]["end"] - human[0]["start"] >= live.REFERENCE_BATCH_SECONDS - 6


def test_radars_apply_their_reference_batches_on_different_ticks():
    """All radars started together would fold a minute of evidence on the same tick: one tick
    of the event loop then carried every radar's work."""
    from types import SimpleNamespace

    from tuner_under_test.history import live

    applied = []
    runtime = SimpleNamespace(_history_runtime={}, data={"devices": {}})
    pending = {f"r{i}": [(SimpleNamespace(start=0.0), ("present", 90, 1))] for i in range(4)}
    for key, spans in pending.items():
        runtime._history_runtime[key] = {"reference_spans": list(spans)}
        runtime.data["devices"][key] = {}
    with patch.object(live, "_references", lambda rt, device, pending: applied.append(device)):
        ticks = []
        for now in (61.0, 63.0, 65.0, 67.0):
            before = len(applied)
            for key in pending:
                live._apply_batch(runtime, key, runtime.data["devices"][key], now)
            ticks.append(len(applied) - before)
    assert ticks == [1, 1, 1, 1]


class ReferenceBatchFoldTests(AcquisitionTests):
    def test_a_batch_is_folded_once_with_the_same_result_as_one_by_one(self):
        from copy import deepcopy

        from tuner_under_test.presence import reference_training, references

        self.device["history_labels"] = [{"start": START, "end": START + 600, "state": "present"}]
        values = [30 if key == "g0_move" else None for key in HISTORY_KEYS]
        items = [(START + 5 * i, START + 5 * i + 5, values, None, 0.0, False) for i in range(40)]
        single = deepcopy(self.device)
        for item in items:
            reference_training.observe_span(self.runtime, single, *item[:3])
        with patch.object(references, "encode", wraps=references.encode) as encoded:
            reference_training.observe_spans(self.runtime, self.device, items)
        assert self.device["reference_profile"] == single["reference_profile"]
        # One encode of the batch's evidence (a new period also starts as an encoded {}).
        assert [call for call in encoded.call_args_list if call.args[0]] != []
        assert len([call for call in encoded.call_args_list if call.args[0]]) == 1
