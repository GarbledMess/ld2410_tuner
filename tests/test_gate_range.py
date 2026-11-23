"""A radar's range: gates beyond its Max Move/Still Distance are neither recorded nor charted."""

import types
import unittest

import test_tuner as harness
from test_combined_gate_report import CombinedRuntimeTests
from tuner_under_test import gate_range
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.presence import autolabelling
from tuner_under_test.presentation import charts
from tuner_under_test.worker import display


def state(value):
    return types.SimpleNamespace(state=value)


class OutsideTests(unittest.TestCase):
    def test_gates_beyond_each_kinds_farthest_gate_are_outside(self):
        out = gate_range.outside({"move": 5, "still": 3})
        assert out == {f"g{gate}_move" for gate in (6, 7, 8)} | {
            f"g{gate}_still" for gate in range(4, 9)
        }

    def test_an_unknown_range_keeps_every_gate(self):
        assert gate_range.outside({"move": None, "still": None}) == set()
        assert gate_range.outside({"move": None, "still": 8}) == set()
        assert gate_range.outside({"move": 2, "still": None}) == {
            f"g{gate}_move" for gate in range(3, 9)
        }

    def test_only_finite_gate_numbers_are_a_range(self):
        assert gate_range._gate(None) is None
        for value in ("unknown", "unavailable", "", "nan", "inf", "-1"):
            assert gate_range._gate(state(value)) is None, value
        assert gate_range._gate(state("6")) == 6
        assert gate_range._gate(state("4.0")) == 4
        assert gate_range._gate(state("12")) == 8

    def test_room_charts_take_each_radars_own_keys(self):
        assert display._device_keys(["g0_move"], "a") == ["g0_move"]
        assert display._device_keys({"a": ["g1_move"]}, "a") == ["g1_move"]
        assert display._device_keys({"a": ["g1_move"]}, "b") == []


class RangeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    entity = CombinedRuntimeTests.entity
    radar = CombinedRuntimeTests.radar
    event = CombinedRuntimeTests.event

    def ranged(self, move="4", still="2", name="max_{}_distance", combined=True):
        self.radar(combined=combined)
        self.entity(f"number.radar_{name.format('move')}", move)
        self.entity(f"number.radar_{name.format('still')}", still)
        self.runtime.refresh_devices(self.registry)

    def recorded(self):
        return dict(zip(HISTORY_KEYS, self.runtime._days.current("a"), strict=True))

    def test_the_range_is_read_from_the_radars_settings(self):
        self.ranged()
        assert gate_range.read(self.runtime, "a") == {"move": 4, "still": 2}
        self.ranged(name="max_{}_distance_gate")  # Another naming, now ambiguous for both.
        gate_range.forget(self.runtime)
        assert gate_range.read(self.runtime, "a") == {"move": None, "still": None}

    def test_a_radar_without_range_settings_keeps_every_gate(self):
        self.radar()
        self.runtime.refresh_devices(self.registry)
        assert gate_range.beyond(self.runtime, "a") == set()

    def test_gate_events_beyond_the_range_are_recorded_as_missing(self):
        self.ranged()
        self.event("sensor.radar_g6_move_energy", "40", at=self.now)
        self.event("sensor.radar_g3_still_energy", "40", at=self.now + 1)
        self.event("sensor.radar_g4_move_energy", "40", at=self.now + 2)
        recorded = self.recorded()
        assert recorded["g6_move"] is None
        assert recorded["g3_still"] is None
        assert recorded["g4_move"] == 40
        assert self.runtime._live["a"]["g6_move"] == 40.0  # Live readings are untouched.

    def test_combined_frames_beyond_the_range_are_recorded_as_missing(self):
        self.ranged(move="1", still="0")
        self.event("sensor.radar_gate_energies", "10,0,20,5,30,40," + ",".join("1" * 12))
        recorded = self.recorded()
        assert recorded["g0_move"] == 10 and recorded["g0_still"] == 0
        assert recorded["g1_move"] == 20 and recorded["g1_still"] is None
        assert all(value is None for key, value in recorded.items() if int(key[1]) >= 2)

    def test_a_changed_range_applies_at_the_next_poll(self):
        self.ranged(combined=False)  # The poll reads 50 from every gate sensor.
        autolabelling._sample_device(self.runtime, "a", self.device)
        assert self.recorded()["g8_move"] is None
        self.states["number.radar_max_move_distance"].state = "8"
        autolabelling._sample_device(self.runtime, "a", self.device)
        assert self.recorded()["g8_move"] == 50  # Recorded again once within range.
        self.states["number.radar_max_move_distance"].state = "3"
        autolabelling._sample_device(self.runtime, "a", self.device)
        assert self.recorded()["g4_move"] is None

    def test_charts_leave_out_the_gates_beyond_the_range(self):
        self.ranged()
        keys = [f"g{gate}_move" for gate in range(9)] + ["g0_still", "g5_still", "bogus"]
        assert charts._in_range(self.runtime, "a", keys) == [
            *(f"g{gate}_move" for gate in range(5)),
            "g0_still",
        ]
        view = types.SimpleNamespace(hass=None)  # A detached view charts what it is asked.
        assert charts._in_range(view, "a", keys) == keys
