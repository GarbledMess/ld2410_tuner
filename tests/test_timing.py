"""Offline timing checks; no private recordings or physical device required."""

import random
import sys
import types
import unittest
from unittest.mock import patch

import pytest
import test_tuner as harness
from test_tuner import fit

config_module = sys.modules["tuner_under_test.calibration.timing_config"]
timing_module = sys.modules["tuner_under_test.calibration.timing"]
measure_module = sys.modules["tuner_under_test.calibration.timing_metrics"]
metrics_module = sys.modules["tuner_under_test.calibration.metrics"]
fitting_module = sys.modules["tuner_under_test.calibration.fitting"]


def rows(values, label="present", step=1, start=0):
    return [(start + i * step, {"g0_still": value}, label) for i, value in enumerate(values)]


def config(hold=5, on=None, off=None):
    return {"timeout": hold, "on_delay": on, "off_delay": off}


def projected(values, settings, *, upper=False, step=1):
    group = timing_module.GroupTiming(rows(values, step=step), settings, upper=upper)
    mask = sum(1 << i for i, value in enumerate(values) if value)
    result = group.project(mask)
    return [bool(result & (1 << i)) for i in range(len(values))]


def test_hold_covers_quiet_dip_and_does_not_delete_false_positive():
    values = [0, 0, 1, 0, 0, 0, 0, 0]
    assert projected(values, config(3)) == [False, False, True, True, True, False, False, False]
    assert sum(projected(values, config(3), upper=True)) == 4
    assert sum(projected(values, config(0), upper=True)) == 1


def test_zero_hold_keeps_every_observed_crossing_including_last_of_run():
    values = [0, 1, 1, 0, 0, 1, 0]
    assert projected(values, config(0)) == [bool(value) for value in values]
    assert projected([0, 0, 1, 1], config(0), upper=True) == [False, False, True, True]


def test_hardware_hold_precedes_delayed_on():
    values = [0, 0, 1, 0, 0, 0, 0, 0]
    assert projected(values, config(3, 0.5, 0))[3:5] == [True, True]
    assert not any(projected(values, config(0, 3, 0), upper=True))
    sustained = [0, 0, 1, 1, 1, 1, 1, 0, 0]
    assert projected(sustained, config(0, 3, 0))[5:7] == [True, True]


def test_sparse_isolated_crossing_is_not_claimed_to_be_subsecond():
    values = [0, 0, 1, 0, 0]
    assert projected(values, config(0, 0.5, 0), upper=True, step=6)[2]


def test_off_delay_bridges_only_when_next_delayed_on_arrives_in_time():
    values = [0, 0, 1, 0, 0, 1, 0, 0, 0, 0]
    assert all(projected(values, config(2, 1, 2))[3:9])
    delayed = [0, 0, 1, 0, 0, 0, 1, 0, 0, 0]
    assert not projected(delayed, config(2, 1, 2))[6]


def test_unknown_or_partial_filter_metadata_does_not_guess_filters():
    values = [0, 1, 0, 0]
    assert projected(values, config(None, 0.5, 1)) == [False, True, False, False]
    assert projected(values, config(2, 0.5, None)) == projected(values, config(2))


def test_gates_are_combined_before_hold_and_delay():
    present = [
        (i, {"g0_still": 20 if i % 2 else 0, "g1_still": 0 if i % 2 else 20}, "present")
        for i in range(20)
    ]
    result = measure_module.evaluate(
        {"present": present, "not_present": []}, {"g0_still": 10, "g1_still": 10}, config(0, 3, 0)
    )
    assert result["false_negatives"] == 0
    assert result["present_samples"] == 17


def test_gaps_and_label_changes_reset_timing_context():
    present = [
        (0, {"g0_still": 20}, "present"),
        (4, {"g0_still": 0}, "present"),
        (30, {"g0_still": 0}, "present"),
        (36, {"g0_still": 0}, "present"),
    ]
    absent = [(2, {"g0_still": 0}, "not_present")]
    replay = timing_module.TimingReplay(present, absent, config(5))
    assert replay.eligible == (8, 0)
    assert not replay.positive.project(1) & 2
    measured = measure_module.evaluate(
        {"present": present, "not_present": absent}, {"g0_still": 10}, config(5)
    )
    assert measured["false_negatives"] == 1
    assert measured["timing_warmup_samples"] == 4


def test_recent_window_keeps_context_from_earlier_observations():
    present = rows([0] * 7 + [20, 0, 0])
    result = measure_module.evaluate(
        {"present": present, "not_present": []}, {"g0_still": 10}, config(5), recent=True
    )
    assert result["present_samples"] == 2
    assert result["false_negatives"] == 0


def test_timed_rank_matches_public_validation_including_recent_and_gaps():
    rng = random.Random(81)
    for _ in range(40):
        time = 0
        groups = {"present": [], "not_present": []}
        for i in range(160):
            time += rng.choice([1, 6, 6, 20])
            label = "present" if (i // 20) % 2 else "not_present"
            groups[label].append((time, {"g0_still": rng.randrange(30)}, label))
        threshold = rng.randrange(30)
        settings = config(rng.choice([0, 5, 15]), 0.5, 1)
        replay = sys.modules["tuner_under_test.calibration.duration"].DurationReplay(
            groups["present"], groups["not_present"], settings
        )
        masks = [
            metrics_module.threshold_masks(groups[label], "g0_still")[threshold] for label in groups
        ]
        rank = replay.rank(*masks)
        all_metrics = measure_module.evaluate(groups, {"g0_still": threshold}, settings)
        recent = measure_module.evaluate(groups, {"g0_still": threshold}, settings, recent=True)
        duration = all_metrics["duration"]
        assert rank[:6] == (
            round(duration["error_cost"], 10),
            duration["missed_presence_episodes"] + recent["duration"]["missed_presence_episodes"],
            round(recent["duration"]["error_cost"], 10),
            round(duration["missed_seconds_upper"], 6),
            round(duration["false_positive_percent"] or 0, 9),
            duration["false_trigger_events"],
        )


def test_timing_improves_gate_choice_without_hiding_noise_or_changing_settings():
    # Frequent two-reading quiet dips are retained, not isolated-outlier exclusions.
    present = rows([8 if i % 10 in (5, 6) else 70 for i in range(500)], step=6)
    absent = rows([15] * 500, "not_present", step=6, start=4000)
    baseline = fit(present + absent, ["g0_still"])
    timed = fit(present + absent, ["g0_still"], timing=config(20))
    assert baseline["training"]["false_positives"] == 500
    assert timed["status"] == "ok"
    assert timed["training"]["false_positives"] == timed["training"]["false_negatives"] == 0
    assert 15 <= timed["proposals"]["g0_still"]["threshold"] < 70
    assert timed["raw_training"]["false_negatives"] == 100
    assert timed["review"]["period_count"] == 0
    assert timed["outlier_filter"]["human"]["excluded"]["present"] == 0
    assert timed["feasibility"]["status"] == "not_assessed"


def test_warmup_cannot_produce_a_green_result_with_no_measured_evidence():
    result = fit(
        rows([70] * 100) + rows([15] * 100, "not_present", start=200),
        ["g0_still"],
        timing=config(600),
    )
    assert result["status"] == "insufficient"
    assert result["proposals"]  # User can still choose to apply an unproven result.
    assert result["training"]["present_samples"] == 0


def test_pair_repair_preserves_presence_supported_by_alternating_gates():
    search_class = sys.modules["tuner_under_test.calibration.search"]._ThresholdSearch
    keys = ["g0_move", "g1_still"]
    present = [
        (i, {keys[0]: 30 if i % 2 == 0 else 0, keys[1]: 10 if i % 2 == 0 else 20}, "present")
        for i in range(20)
    ]
    absent = [(100 + i, {keys[0]: 25, keys[1]: 0}, "not_present") for i in range(20)]
    search = search_class(
        present, absent, {"present": [], "not_present": []}, keys, None, config(0, 3, 0)
    )
    thresholds = {keys[0]: 10, keys[1]: 15}
    selected = {key: search.tables[key][value] for key, value in thresholds.items()}
    candidates = list(
        search._pair_candidates(keys[0], keys[1], thresholds, selected, search._distinct_options())
    )
    assert candidates, "Neither gate alone passes delayed_on, but their combined run does"


def test_recovered_high_snapshot_is_uncertain_not_a_second_proven_miss():
    present = rows([70, 70, 5, 70, 70], step=6)
    result = measure_module.evaluate(
        {"present": present, "not_present": []}, {"g0_still": 20}, config(1, 0.5, 1)
    )
    assert result["present_samples"] == 4
    assert result["false_negatives"] == 1
    assert result["longest_missed_run_samples"] == 1
    assert result["onset_uncertainty"] == {
        "samples": 1,
        "misses_if_earliest_onset": 1,
        "misses_if_latest_onset": 2,
    }
    recent = measure_module.evaluate(
        {"present": present, "not_present": []}, {"g0_still": 20}, config(1, 0.5, 1), recent=True
    )
    assert recent["onset_uncertainty"]["samples"] == 0


def test_late_onset_is_always_within_reported_uncertainty():
    rng = random.Random(612)
    for _ in range(60):
        present = rows([rng.choice([0, 20]) for _ in range(30)], step=rng.choice([0.2, 1, 6]))
        replay = timing_module.TimingReplay(
            present, [], config(rng.choice([0, 1, 5]), rng.choice([0.5, 2, 10]), 1)
        )
        mask = sum(1 << i for i, row in enumerate(present) if row[1]["g0_still"])
        assert not replay.latest_onset.project(mask) & ~replay.positive.project(mask)


def test_unresolved_short_delay_does_not_lower_gates_to_fix_a_sampling_assumption():
    # Four quiet readings have unresolved recovery times between six-second
    # snapshots. Their finite missed-time cost must not force continuous noise
    # simply to eliminate every possible miss.
    present = rows([14 if i in (100, 1100, 2100, 3100) else 70 for i in range(5000)], step=6)
    absent = rows([15] * 100, "not_present", step=6, start=40000)
    # This checks timing, independently of the separate outlier detector.
    with (
        patch.object(
            fitting_module, "prepare_evidence", side_effect=lambda p, a, k: (p, a, {}, {})
        ),
        patch.object(fitting_module, "_backtest", return_value=(None, [], None)),
    ):
        result = fit(present + absent, ["g0_still"], timing=config(1, 0.5, 1))
    assert result["training"]["false_positives"] == 0
    assert result["training"]["false_negatives"] == 4
    assert result["training"]["onset_uncertainty"]["samples"] == 4
    assert result["status"] == "uncertain"
    assert result["proposals"]["g0_still"]["threshold"] >= 15


def test_long_on_delay_still_counts_misses_that_cannot_have_elapsed():
    present = rows([0, 0, 70, 70, 70, 70, 70, 70], step=1)
    replay = timing_module.TimingReplay(present, [], config(1, 3, 0))
    detected = replay.positive.project(sum(1 << i for i in range(2, 8)))
    assert not detected & (1 << 3)
    assert not detected & (1 << 4)
    assert detected & (1 << 5)


def entity(entity_id, name=None, device="a"):
    return types.SimpleNamespace(
        entity_id=entity_id, original_name=name, device_id=device, domain=entity_id.split(".")[0]
    )


def read(entities, values):
    registry = types.SimpleNamespace(entities={e.entity_id: e for e in entities})
    runtime = types.SimpleNamespace(
        hass=types.SimpleNamespace(states=types.SimpleNamespace(get=values.get))
    )
    with patch.object(config_module.er, "async_get", return_value=registry, create=True):
        return config_module.read_timing(runtime, "a")


def state(value, unit=""):
    return types.SimpleNamespace(state=str(value), attributes={"unit_of_measurement": unit})


def test_standard_timeout_discovery_survives_renaming_without_package():
    result = read(
        [
            entity("number.custom", "Timeout"),
            entity("number.training_timeout"),
            entity("number.other_timeout", device="b"),
        ],
        {"number.custom": state(15)},
    )
    assert result["timeout"] == 15
    assert result["scope"] == "radar"
    assert result["on_delay"] is result["off_delay"] is None


def test_package_metadata_and_independent_numeric_entities_are_supported():
    entities = [
        entity("number.radar_timeout"),
        entity("sensor.renamed", "LD2410 Presence On Delay"),
        entity("number.radar_presence_off_delay"),
    ]
    result = read(
        entities,
        {
            "number.radar_timeout": state(5, "s"),
            "sensor.renamed": state("500ms"),
            "number.radar_presence_off_delay": state(1000, "ms"),
        },
    )
    assert config_module.timing_values(result) == config(5, 0.5, 1)
    assert result["scope"] == "reported_presence"


@pytest.mark.parametrize(
    "value,unit",
    [("unknown", "s"), ("nan", "s"), (-1, "s"), (65536, "s"), (10, "unknown"), ("inf", "s")],
)
def test_invalid_timeout_falls_back_to_raw(value, unit):
    result = read([entity("number.radar_timeout")], {"number.radar_timeout": state(value, unit)})
    assert result["scope"] == "raw"


def test_missing_and_ambiguous_entities_are_unknown_not_zero():
    assert read([], {})["timeout"] is None
    assert read([entity("number.radar_timeout")], {})["timeout"] is None
    entities = [entity("number.one_timeout"), entity("number.two_timeout")]
    assert read(entities, {e.entity_id: state(5) for e in entities})["timeout"] is None
    assert config_module._seconds(state(500), "on_delay") is None
    assert config_module._seconds(state("2 min"), "on_delay") == 120
    assert config_module._seconds(state("1h"), "off_delay") == 3600


class TimingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    configuration = harness.RuntimeTests.configuration

    def add_timeout(self, value):
        key = "number.radar_timeout"
        self.registry.entities[key] = entity(key)
        self.states[key] = state(value, "s")
        return key

    async def test_learn_captures_current_timing_without_writing_device_settings(self):
        self.configuration()
        self.add_timeout(15)
        captured = []

        async def execute(fn, *args):
            captured.append(fn.__self__._fit_timing)
            return {"status": "insufficient", "proposals": {}}

        self.hass.async_add_executor_job = execute
        result = await self.runtime.async_learn("a")
        assert result["timing_configuration"]["timeout"] == 15
        assert captured[0]["scope"] == "radar"
        self.hass.services.async_call.assert_not_awaited()

    async def test_timing_change_during_learning_discards_the_result(self):
        self.configuration()
        key = self.add_timeout(15)

        async def execute(*args):
            self.states[key] = state(5)
            return {"status": "ok", "proposals": {}}

        self.hass.async_add_executor_job = execute
        with self.assertRaisesRegex(ValueError, "Device timing changed"):
            await self.runtime.async_learn("a")
        assert self.runtime._learning_jobs == {}
        self.hass.services.async_call.assert_not_awaited()

    async def test_apply_only_writes_thresholds_even_with_discovered_timing(self):
        self.configuration()
        key = self.add_timeout(15)
        self.device["last_learning"]["timing_configuration"] = config(5, 0.5, 1)
        result = await self.runtime.apply("a")
        assert result["applied"]
        for call in self.hass.services.async_call.await_args_list:
            assert call.args[0:2] == ("number", "set_value")
            assert call.args[2]["entity_id"].endswith("_threshold")
        assert self.states[key].state == "15"

    def test_snapshot_and_export_include_live_timing(self):
        self.configuration()
        self.add_timeout(15)
        key = "sensor.radar_g0_move_energy"
        self.registry.entities[key] = entity(key)
        registry = types.SimpleNamespace(
            async_get=lambda _: types.SimpleNamespace(
                name_by_user="Synthetic room", name="Radar", area_id=None
            )
        )
        dr = sys.modules["homeassistant.helpers.device_registry"]
        with patch.object(dr, "async_get", return_value=registry, create=True):
            snapshot = self.runtime.snapshot()["devices"]["a"]
            exported = self.runtime.export_data("a")["devices"]["a"]
        assert snapshot["timing_configuration"]["timeout"] == 15
        assert exported["timing_configuration"] == snapshot["timing_configuration"]
