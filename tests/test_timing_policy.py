"""Global timing choices apply equally to manual and scheduled threshold fitting."""

import copy
import sys
import types
import unittest
from unittest.mock import patch

import pytest
import test_tuner as harness
from test_timing import config, entity, state

policy = sys.modules["tuner_under_test.calibration.timing_config"]
schedule = sys.modules["tuner_under_test.runtime.schedule"]
measure = sys.modules["tuner_under_test.calibration.timing_metrics"]


def resolved(mode="device", values=None, entries=None):
    runtime = types.SimpleNamespace(
        data={"timing_settings": {**policy.DEFAULTS, "mode": mode}},
        hass=types.SimpleNamespace(states=types.SimpleNamespace(get=(values or {}).get)),
    )
    registry = types.SimpleNamespace(entities={e.entity_id: e for e in entries or []})
    with patch.object(policy.er, "async_get", return_value=registry, create=True):
        return policy.read_timing(runtime, "a")


def test_defaults_are_opt_in_and_missing_delays_stay_unknown():
    result = resolved()
    assert policy.timing_values(result) == dict.fromkeys(policy.FIELDS)
    assert set(result["sources"].values()) == {"unknown"}


def test_fallback_is_per_field_and_never_overrides_valid_zero():
    result = resolved(
        "fallback", {"number.radar_timeout": state(0)}, [entity("number.radar_timeout")]
    )
    assert policy.timing_values(result) == config(0, 0.5, 1)
    assert result["sources"] == {
        "timeout": "device",
        "on_delay": "fallback",
        "off_delay": "fallback",
    }
    assert result["reported"]["on_delay"] is None
    assert result["scope"] == "reported_presence"


@pytest.mark.parametrize("value", ["unavailable", "unknown", "nan", "invalid"])
def test_unreadable_device_value_uses_explicit_fallback(value):
    result = resolved(
        "fallback", {"number.radar_timeout": state(value)}, [entity("number.radar_timeout")]
    )
    assert result["timeout"] == 1
    assert result["sources"]["timeout"] == "fallback"


def test_disabled_timing_ignores_device_controls_and_uses_raw_replay():
    result = resolved(
        "disabled", {"number.radar_timeout": state(30)}, [entity("number.radar_timeout")]
    )
    assert policy.timing_values(result) == dict.fromkeys(policy.FIELDS)
    assert result["reported"]["timeout"] == 30
    groups = {
        "present": [(i * 6, {"g0_still": 20 if i % 3 else 5}, "present") for i in range(20)],
        "not_present": [(200 + i * 6, {"g0_still": 5}, "not_present") for i in range(20)],
    }
    assert measure.evaluate(groups, {"g0_still": 10}, result) == measure.evaluate(
        groups, {"g0_still": 10}
    )
    assert not measure.timing_summary(result, groups)["active"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "invalid"),
        ("timeout", -1),
        ("on_delay", float("nan")),
        ("off_delay", float("inf")),
        ("timeout", True),
        ("on_delay", "1"),
        ("off_delay", 65536),
    ],
)
def test_invalid_policy_does_not_change_saved_settings(field, value):
    runtime = types.SimpleNamespace(data={})
    with pytest.raises(ValueError):
        policy.configure(runtime, {**policy.DEFAULTS, field: value})
    assert runtime.data == {}


def test_invalid_policy_shape_is_rejected():
    for values in (None, {}, {**policy.DEFAULTS, "extra": 1}):
        with pytest.raises(ValueError):
            policy.configure(types.SimpleNamespace(data={}), values)


class TimingPolicyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    configuration = harness.RuntimeTests.configuration

    async def test_settings_survive_save_and_are_shared_by_both_learning_sources(self):
        self.configuration()
        values = {"mode": "fallback", "timeout": 2, "on_delay": 0.25, "off_delay": 3}
        self.runtime.configure_timing(values)
        values["timeout"] = 999  # The request must not alias saved state.
        await self.runtime.async_save()
        persisted = self.runtime.store.async_save.call_args.args[0]
        assert policy.settings(types.SimpleNamespace(data=persisted))["timeout"] == 2
        captured = []

        async def fit(fn, *args):
            captured.append(copy.deepcopy(fn.__self__._fit_timing))
            return {
                "status": "ok",
                "proposals": {},
                "timing": measure.timing_summary(captured[-1], {}),
            }

        self.hass.async_add_executor_job = fit
        for source in ("user", "automatic"):
            await self.runtime.async_learn("a", source=source)
        assert captured[0] == captured[1]
        assert policy.timing_values(captured[0]) == config(2, 0.25, 3)
        self.hass.services.async_call.assert_not_awaited()

    async def test_policy_change_during_fit_discards_result(self):
        self.configuration()

        async def fit(*args):
            self.runtime.configure_timing({**policy.DEFAULTS, "mode": "disabled"})
            return {"status": "ok", "proposals": {}}

        self.hass.async_add_executor_job = fit
        with self.assertRaisesRegex(ValueError, "Device timing changed"):
            await self.runtime.async_learn("a")

    async def test_missing_timing_does_not_fail_overnight_job(self):
        self.configuration()

        async def fit(fn, *args):
            timing = fn.__self__._fit_timing
            assert timing["on_delay"] is None
            return {
                "status": "uncertain",
                "proposals": {},
                "timing": measure.timing_summary(timing, {}),
                "training": {
                    "duration": {
                        "presence_recall": 1,
                        "presence_recall_lower": 0.999,
                        "false_positive_percent": 0.2,
                    }
                },
            }

        self.hass.async_add_executor_job = fit
        await schedule._learn_device(self.runtime, "a", self.device, "2026-09-29")
        attempt = self.device["nightly_learning"]
        assert attempt["status"] == "uncertain"
        assert "error" not in attempt
        assert attempt["assessment"]["false_positive_percent"] == 0.2
        assert attempt["timing"]["configuration"]["on_delay"] is None
        self.hass.services.async_call.assert_not_awaited()


def test_automatic_only_overnight_headline_retains_estimated_metrics():
    result = {
        "estimated_training": {
            "duration": {
                "presence_recall": 1,
                "presence_recall_lower": 1,
                "false_positive_percent": 2,
            }
        }
    }
    assert schedule._assessment(result) == result["estimated_training"]["duration"]
