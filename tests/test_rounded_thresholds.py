"""Radars running the package's Gate Rounding show and accept rounded thresholds.

The radar still triggers on raw energy above its own integer threshold. With rounding, a
control shown at T (step s) holds the radar threshold ``ceil(T - s/2) - 1``, so every raw
energy that reports T or more passes. The tuner keeps working in the radar's own threshold
and converts only where it reads or writes the control.
"""

import math
import sys
import types
import unittest

import pytest
import test_tuner as harness
from tuner_under_test.calibration import thresholds

io = sys.modules["tuner_under_test.calibration.device_io"]
snapshots = sys.modules["tuner_under_test.presentation.snapshots"]


def rounded(step=10, low=0, high=100):
    return {"min": float(low), "max": float(high), "step": float(step), "rounded": True}


def shown(radar, low, step):
    """The package's control lambda: the band the radar threshold opens, on the grid."""
    return min(100.0, max(0.0, low + math.floor((radar + 1 - low) / step + 0.5) * step))


def written(control, step):
    """The package's set_action for a control value already on the grid."""
    return 0 if control <= 0 else max(0, math.ceil(control - step / 2) - 1)


def test_radar_thresholds_reachable_through_a_rounded_control():
    assert thresholds.allowed(rounded()) == (0, 4, 14, 24, 34, 44, 54, 64, 74, 84, 94)
    assert thresholds.allowed(rounded(1)) == tuple(range(100))
    assert thresholds.allowed(rounded(10, 20, 80)) == (14, 24, 34, 44, 54, 64, 74)
    assert thresholds.allowed(rounded(10, 25, 95)) == (19, 29, 39, 49, 59, 69, 79, 89)


@pytest.mark.parametrize(
    ("step", "low", "high"),
    [(10, 0, 100), (1, 0, 100), (5, 0, 100), (2, 0, 100), (10, 25, 95), (25, 0, 100)],
)
def test_control_and_radar_values_round_trip_through_the_package(step, low, high):
    domain = rounded(step, low, high)
    for radar in thresholds.allowed(domain):
        control = thresholds.control_value(radar, domain)
        assert written(control, step) == radar
        assert shown(radar, low, step) == control
        assert thresholds.radar_value(control, domain) == radar


def test_reading_a_rounded_control_gives_the_radar_threshold():
    state = types.SimpleNamespace(state="30.0", attributes={"min": 0, "max": 100, "step": 10})
    assert thresholds.reading(state, rounded=True) == 24
    assert thresholds.reading(state, rounded=False) == 30
    assert math.isnan(thresholds.reading(types.SimpleNamespace(state="unknown"), rounded=True))
    one = types.SimpleNamespace(state="1", attributes={"min": 0, "max": 100, "step": 1})
    assert thresholds.reading(one, rounded=True) == 0


def test_normalize_and_bounds_use_radar_values():
    assert thresholds.normalize(30, rounded()) == 34
    assert thresholds.normalize(28, rounded()) == 24
    assert thresholds.normalize(100, rounded()) == 94
    assert thresholds.bounds(rounded(10, 20, 80)) == (14, 74)
    assert thresholds.bounds({"min": 20.0, "max": 85.0, "step": 10.0}) == (20.0, 85.0)


def test_plain_domains_are_unchanged():
    plain = {"min": 0.0, "max": 100.0, "step": 10.0}
    assert thresholds.control_value(30, plain) == 30
    assert thresholds.radar_value(30, plain) == 30


class RoundedRadarTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    STEP = 10

    def attributes(self):
        return {"min": 0, "max": 100, "step": self.STEP}

    async def package_write(self, domain, service, data, **kwargs):
        """The ESP: round the request onto the grid, store the raw value, show its band."""
        if domain == "number" and service == "set_value":
            state = self.states[data["entity_id"]]
            control = round(data["value"] / self.STEP) * self.STEP
            raw = written(control, self.STEP)
            state.state = str(shown(raw, 0, self.STEP))
            self.raw[data["entity_id"]] = raw

    def configuration(self, shown_value="30"):
        self.raw = {}
        for i in range(3):
            for kind in ("move", "still"):
                entity_id = f"number.radar_g{i}_{kind}_threshold"
                self.registry.entities[entity_id] = types.SimpleNamespace(
                    device_id="a", domain="number", entity_id=entity_id
                )
                self.states[entity_id] = types.SimpleNamespace(
                    state=shown_value, attributes=self.attributes()
                )
        for kind in ("move", "still"):
            entity_id = f"number.radar_max_{kind}_distance_gate"
            self.registry.entities[entity_id] = types.SimpleNamespace(
                device_id="a", domain="number", entity_id=entity_id
            )
            self.states[entity_id] = types.SimpleNamespace(state="2")
        self.registry.entities["number.radar_gate_rounding"] = types.SimpleNamespace(
            device_id="a", domain="number", entity_id="number.radar_gate_rounding"
        )
        self.states["number.radar_gate_rounding"] = types.SimpleNamespace(state=str(self.STEP))
        self.hass.services.async_call.side_effect = self.package_write

    def learned(self, proposal):
        entities, current = self.runtime._threshold_configuration("a")
        self.device["last_learning"] = {
            "method": sys.modules["tuner_under_test.calibration.constants"].METHOD,
            **sys.modules["tuner_under_test.calibration.settings"].signature(self.runtime.data),
            "status": "ok",
            "entities": entities,
            "configuration": current,
            "threshold_constraints": thresholds.read(self.runtime, entities),
            "proposals": {key: {"threshold": proposal} for key in entities},
        }

    def writes(self):
        return [
            call.args[2]["value"]
            for call in self.hass.services.async_call.await_args_list
            if call.args[0] == "number"
        ]

    def test_current_thresholds_are_the_radar_values(self):
        self.configuration()
        _entities, current = self.runtime._threshold_configuration("a")
        assert set(current.values()) == {24}
        registry = io.er.async_get(self.hass)
        assert set(snapshots._snapshot_thresholds(self.runtime, "a", registry).values()) == {24}

    def test_constraints_offer_the_reachable_radar_values(self):
        self.configuration()
        entities, _current = self.runtime._threshold_configuration("a")
        domains = thresholds.read(self.runtime, entities)
        assert all(thresholds.allowed(d)[:3] == (0, 4, 14) for d in domains.values())

    def test_a_radar_without_gate_rounding_reads_controls_as_they_are(self):
        self.configuration()
        del self.registry.entities["number.radar_gate_rounding"]
        _entities, current = self.runtime._threshold_configuration("a")
        assert set(current.values()) == {30}

    async def test_apply_writes_the_control_value_that_holds_the_radar_threshold(self):
        self.configuration()
        self.learned(14)
        result = await self.runtime.apply("a")
        assert result["skipped"] == {}
        assert set(result["applied"].values()) == {14}
        assert self.writes() == [20] * 6
        assert set(self.raw.values()) == {14}
        assert set(self.runtime._threshold_configuration("a")[1].values()) == {14}

    async def test_apply_leaves_matching_radar_thresholds_alone(self):
        self.configuration()
        self.learned(24)
        result = await self.runtime.apply("a")
        assert result["skipped"] == {}
        assert self.writes() == []

    async def test_manual_threshold_is_a_radar_value(self):
        self.configuration()
        result = await self.runtime.set_gate_threshold("a", "g0_move", 40)
        assert result["value"] == 44
        assert self.writes() == [50]
        assert self.raw["number.radar_g0_move_threshold"] == 44
        with pytest.raises(ValueError, match="permitted range"):
            await self.runtime.set_gate_threshold("a", "g0_move", 97)


class RoundedStepOneTests(RoundedRadarTests):
    """Gate Rounding 1 still moves the radar threshold one below the shown value."""

    STEP = 1

    def test_current_thresholds_are_the_radar_values(self):
        self.configuration("30")
        assert set(self.runtime._threshold_configuration("a")[1].values()) == {29}

    def test_constraints_offer_the_reachable_radar_values(self):
        self.configuration()
        entities, _current = self.runtime._threshold_configuration("a")
        domains = thresholds.read(self.runtime, entities)
        assert all(thresholds.allowed(d) == tuple(range(100)) for d in domains.values())

    async def test_apply_writes_the_control_value_that_holds_the_radar_threshold(self):
        self.configuration()
        self.learned(14)
        result = await self.runtime.apply("a")
        assert result["skipped"] == {}
        assert self.writes() == [15] * 6
        assert set(self.raw.values()) == {14}

    async def test_apply_leaves_matching_radar_thresholds_alone(self):
        self.configuration("15")
        self.learned(14)
        assert (await self.runtime.apply("a"))["skipped"] == {}
        assert self.writes() == []

    async def test_manual_threshold_is_a_radar_value(self):
        self.configuration()
        result = await self.runtime.set_gate_threshold("a", "g0_move", 0)
        assert result["value"] == 0
        assert self.writes() == [1]
        assert self.raw["number.radar_g0_move_threshold"] == 0


def test_a_radar_value_the_rounding_cannot_reach_is_refused():
    with pytest.raises(ValueError, match="not reachable"):
        thresholds.control_value(30, rounded())
