"""Combined Gate Report: one Gate Energies state per radar frame instead of 18 gate states.

The ESPHome package's Combined Gate Report switch makes the radar publish every gate in one
text sensor and refresh the 18 gate sensors only slowly. The tuner keeps the switch on, reads
the combined sensor, and falls back to the gate sensors whenever it carries no frame.
"""

import types
import unittest

import test_tuner as harness
from tuner_under_test import entity_index
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.presence import autolabelling
from tuner_under_test.runtime import engineering, gate_report

FRAME = "10,0,20,,,30,100,,0,,,,,,,,,"
VALUES = {
    "g0_move": 10.0,
    "g0_still": 0.0,
    "g1_move": 20.0,
    "g2_still": 30.0,
    "g3_move": 100.0,
    "g4_move": 0.0,
}


class ParseTests(unittest.TestCase):
    def test_a_frame_gives_every_gate_with_missing_ones_as_none(self):
        frame = gate_report.parse(FRAME)
        assert list(frame) == HISTORY_KEYS
        assert {key: value for key, value in frame.items() if value is not None} == VALUES
        assert all(isinstance(value, float) for value in frame.values() if value is not None)

    def test_an_all_unknown_frame_is_still_a_frame(self):
        assert gate_report.parse("," * 17) == dict.fromkeys(HISTORY_KEYS)

    def test_anything_else_is_no_frame(self):
        for state in (
            None,
            "",
            "unknown",
            "unavailable",
            "," * 16,  # 17 fields
            "," * 18,  # 19 fields
            FRAME.replace("100", "101"),
            FRAME.replace("30", "-1"),
            FRAME.replace("20", "x"),
            FRAME.replace("20", "nan"),
            FRAME.replace("20", "inf"),
        ):
            assert gate_report.parse(state) is None, state


class CombinedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def entity(self, entity_id, state=None, device_id="a", **extra):
        self.registry.entities[entity_id] = types.SimpleNamespace(
            entity_id=entity_id,
            device_id=device_id,
            domain=entity_id.split(".")[0],
            original_name=extra.pop("original_name", None),
            **extra,
        )
        if state is not None:
            self.states[entity_id] = types.SimpleNamespace(state=state)
        return entity_id

    def radar(self, device_id="a", prefix="radar", combined=True):
        for key in HISTORY_KEYS:
            self.entity(f"sensor.{prefix}_{key}_energy", "50", device_id)
        if combined:
            self.entity(f"sensor.{prefix}_gate_energies", FRAME, device_id)

    def event(self, entity_id, state, at=1000.0):
        new_state = types.SimpleNamespace(
            state=state, last_updated=types.SimpleNamespace(timestamp=lambda: at)
        )
        self.runtime.handle_state_change(
            types.SimpleNamespace(data={"entity_id": entity_id, "new_state": new_state})
        )

    def test_index_finds_the_combined_sensor_of_each_radar(self):
        self.radar()
        self.radar("b", "other", combined=False)
        radars = entity_index.read(self.runtime, self.registry).radars
        assert radars["a"]["combined"] == "sensor.radar_gate_energies"
        assert radars["b"]["combined"] is None
        assert "sensor.radar_gate_energies" not in radars["a"]["entities"]

    def test_index_recognizes_a_renamed_combined_sensor_and_ignores_ambiguous_ones(self):
        self.radar(combined=False)
        self.entity("sensor.custom", FRAME, original_name="Gate Energies")
        assert entity_index.read(self.runtime, self.registry).radars["a"]["combined"] == (
            "sensor.custom"
        )
        self.entity("sensor.second_gate_energies", FRAME)
        entity_index.forget(self.runtime)
        assert entity_index.read(self.runtime, self.registry).radars["a"]["combined"] is None

    def test_a_disabled_combined_sensor_is_not_used(self):
        self.radar(combined=False)
        self.entity("sensor.radar_gate_energies", FRAME, disabled_by="user")
        assert entity_index.read(self.runtime, self.registry).radars["a"]["combined"] is None

    def test_state_changes_are_tracked_for_the_combined_sensor(self):
        self.radar()
        tracked = []
        self.hass.track_state = lambda ids, listener: tracked.extend(ids) or (lambda: None)
        self.runtime.subscribe_state_changes()
        assert "sensor.radar_gate_energies" in tracked
        assert "sensor.radar_g8_still_energy" in tracked

    def test_a_combined_frame_updates_every_gate_at_its_time(self):
        self.radar()
        self.runtime.refresh_devices(self.registry)
        self.event("sensor.radar_gate_energies", FRAME, at=self.now)
        assert self.runtime._live["a"] == VALUES
        current = self.runtime._days.current("a")
        assert [current[HISTORY_KEYS.index(key)] for key in VALUES] == [
            int(value) for value in VALUES.values()
        ]
        assert abs(self.runtime._days.last("a") - self.now) < 0.01  # Stored to the millisecond.

    def test_a_combined_sensor_going_unavailable_makes_every_gate_missing(self):
        self.radar()
        self.runtime.refresh_devices(self.registry)
        self.event("sensor.radar_gate_energies", FRAME, at=self.now)
        self.event("sensor.radar_gate_energies", "unavailable", at=self.now + 1)
        assert self.runtime._live["a"] == {}
        assert all(value is None for value in self.runtime._days.current("a"))

    def test_a_combined_sensor_without_a_frame_leaves_the_gate_sensors_in_charge(self):
        self.radar()
        self.runtime.refresh_devices(self.registry)
        self.event("sensor.radar_g0_move_energy", "40", at=self.now)
        for state in ("", "unknown"):
            self.event("sensor.radar_gate_energies", state, at=self.now + 1)
        assert self.runtime._live["a"] == {"g0_move": 40.0}

    def test_other_sensors_and_unknown_radars_are_ignored(self):
        self.radar()
        self.radar("z", "unknown")  # Its device is not one the tuner holds.
        self.entity("sensor.radar_detection_distance", "120")
        self.runtime.refresh_devices(self.registry)
        del self.runtime.data["devices"]["z"]
        self.event("sensor.radar_detection_distance", "130", at=self.now)
        self.event("sensor.unknown_gate_energies", FRAME, at=self.now)
        assert "a" not in self.runtime._live
        assert "z" not in self.runtime._live
        assert self.runtime._days.current("z") is None

    def test_the_sampler_prefers_the_combined_frame(self):
        self.radar()
        self.runtime.refresh_devices(self.registry)
        assert autolabelling._read_energies(self.runtime, self.device, "a") == VALUES

    def test_the_sampler_reads_the_gate_sensors_when_there_is_no_frame(self):
        gates = dict.fromkeys(HISTORY_KEYS, 50.0)
        for state in ("", "unknown", "unavailable", None):
            self.radar()
            if state is None:
                del self.states["sensor.radar_gate_energies"]
            else:
                self.states["sensor.radar_gate_energies"].state = state
            entity_index.forget(self.runtime)
            self.runtime.refresh_devices(self.registry)
            assert autolabelling._read_energies(self.runtime, self.device, "a") == gates, state

    def test_the_sampler_reads_the_gate_sensors_of_an_older_package(self):
        self.radar(combined=False)
        self.runtime.refresh_devices(self.registry)
        values = autolabelling._read_energies(self.runtime, self.device, "a")
        assert values == dict.fromkeys(HISTORY_KEYS, 50.0)


class SwitchTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp

    async def asyncTearDown(self):
        await engineering.stop(self.runtime)
        await harness.RuntimeTests.asyncTearDown(self)

    def switch(self, entity_id, state="off", device_id="a", **extra):
        self.registry.entities[entity_id] = types.SimpleNamespace(
            entity_id=entity_id, device_id=device_id, domain="switch", **extra
        )
        self.states[entity_id] = types.SimpleNamespace(state=state)
        return entity_id

    async def turned_on(self, domain, service, data, **kwargs):
        self.states[data["entity_id"]].state = "on" if service == "turn_on" else "off"

    async def check(self):
        engineering.tick(self.runtime)
        await self.runtime._engineering_task

    def calls(self, service):
        return [
            call.args[2]["entity_id"]
            for call in self.hass.services.async_call.await_args_list
            if call.args[:2] == ("switch", service)
        ]

    async def test_every_included_radar_reports_combined_even_when_not_recording(self):
        self.runtime.data["devices"]["b"] = {"entities": {"gate": {}}, "recording_enabled": False}
        self.runtime.data["devices"]["c"] = {"entities": {}, "excluded": True}
        first = self.switch("switch.radar_combined_gate_report")
        second = self.switch("switch.custom", device_id="b", original_name="Combined Gate Report")
        self.switch("switch.gone_combined_gate_report", device_id="c")
        self.hass.services.async_call.side_effect = self.turned_on
        await self.check()
        assert sorted(self.calls("turn_on")) == sorted([first, second])
        await self.check()
        assert len(self.calls("turn_on")) == 2

    async def test_on_unknown_unavailable_disabled_and_missing_switches_are_untouched(self):
        for state in ("on", "unknown", "unavailable"):
            self.switch("switch.radar_combined_gate_report", state)
            await self.check()
        self.switch("switch.radar_combined_gate_report", disabled_by="user")
        await self.check()
        assert self.calls("turn_on") == []

    async def test_a_failed_switch_call_is_retried_on_the_next_check(self):
        switch = self.switch("switch.radar_combined_gate_report")
        self.hass.services.async_call.side_effect = ValueError("offline")
        await self.check()
        self.hass.services.async_call.side_effect = self.turned_on
        await self.check()
        assert self.calls("turn_on") == [switch, switch]
        assert self.states[switch].state == "on"


class RemoveEntryTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    async def test_removing_the_integration_returns_radars_to_individual_gate_reports(self):
        entries = {
            "switch.radar_combined_gate_report": "on",
            "switch.custom": "on",
            "switch.idle_combined_gate_report": "off",
            "switch.radar_engineering_mode": "on",
        }
        for entity_id, state in entries.items():
            self.registry.entities[entity_id] = types.SimpleNamespace(
                entity_id=entity_id,
                device_id="a",
                domain="switch",
                original_name="Combined Gate Report" if entity_id == "switch.custom" else None,
            )
            self.states[entity_id] = types.SimpleNamespace(state=state)
        self.hass.entity_registry = self.registry
        await harness.mod.async_remove_entry(self.hass, object())
        turned_off = [
            call.args[2]["entity_id"] for call in self.hass.services.async_call.await_args_list
        ]
        assert sorted(turned_off) == ["switch.custom", "switch.radar_combined_gate_report"]
        assert all(
            call.args[:2] == ("switch", "turn_off")
            for call in self.hass.services.async_call.await_args_list
        )

    async def test_an_unreachable_radar_does_not_stop_removal(self):
        for name in ("one", "two"):
            entity_id = f"switch.{name}_combined_gate_report"
            self.registry.entities[entity_id] = types.SimpleNamespace(
                entity_id=entity_id, device_id=name, domain="switch", original_name=None
            )
            self.states[entity_id] = types.SimpleNamespace(state="on")
        self.hass.entity_registry = self.registry
        self.hass.services.async_call.side_effect = ValueError("offline")
        await harness.mod.async_remove_entry(self.hass, object())
        assert self.hass.services.async_call.await_count == 2
