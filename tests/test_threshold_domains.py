"""Hardware-advertised gate domains work for native and custom integrations alike."""

import unittest
from types import SimpleNamespace

import pytest
import test_automatic_weight
import test_tuner  # noqa: F401
from tuner_under_test.calibration import thresholds


def test_native_missing_metadata_preserves_all_integer_thresholds():
    assert thresholds.allowed(thresholds.from_state(None)) == tuple(range(101))
    assert thresholds.allowed(thresholds.from_state(SimpleNamespace(state="77"))) == tuple(
        range(101)
    )
    assert thresholds.normalize(77, None) == 77


def test_advertised_step_and_gate_bounds_define_real_search_candidates():
    state = SimpleNamespace(attributes={"min": 20, "max": 80, "step": 10})
    domain = thresholds.from_state(state)
    assert thresholds.allowed(domain) == (20, 30, 40, 50, 60, 70, 80)
    assert thresholds.normalize(77, domain) == 80
    assert thresholds.normalize(75, domain) == 80
    assert thresholds.normalize(10, domain) == 20
    assert thresholds.normalize(100, domain) == 80


def test_single_fixed_gate_and_fractional_advertised_steps():
    assert thresholds.allowed({"min": 100, "max": 100, "step": 10}) == (100,)
    assert thresholds.allowed({"min": 0, "max": 2, "step": 0.5}) == (0, 1, 2)
    assert thresholds.allowed({"min": 5, "max": 25, "step": 10}) == (5, 15, 25)


@pytest.mark.parametrize(
    "domain",
    [
        {"min": 0, "max": 100, "step": 0},
        {"min": 0, "max": 100, "step": float("nan")},
        {"min": -1, "max": 100, "step": 1},
        {"min": 20, "max": 10, "step": 1},
        {"min": 0, "max": 101, "step": 1},
        {"min": 0.5, "max": 1.5, "step": 1},
    ],
)
def test_invalid_or_unrepresentable_domains_are_rejected(domain):
    with pytest.raises(ValueError):
        thresholds.allowed(domain)


def calibration_rows():
    return [
        (i * 6, {"g0_move": 80 if i < 100 else 70}, "present" if i < 100 else "not_present")
        for i in range(200)
    ]


def test_fit_searches_legal_values_instead_of_rounding_a_finished_recommendation():
    from test_tuner import fit

    result = fit(
        calibration_rows(),
        ["g0_move"],
        {"g0_move": 77},
        constraints={"g0_move": {"min": 0, "max": 100, "step": 10}},
    )
    assert result["proposals"]["g0_move"]["threshold"] == 70
    assert result["training"]["false_positives"] == 0
    assert result["training"]["false_negatives"] == 0


def test_restricted_maximum_reports_unavoidable_error_without_illegal_quiet_seed():
    from test_tuner import fit

    result = fit(
        calibration_rows(),
        ["g0_move"],
        {"g0_move": 77},
        constraints={"g0_move": {"min": 0, "max": 50, "step": 10}},
    )
    assert result["proposals"]["g0_move"]["threshold"] <= 50
    assert result["training"]["false_positives"] == 100
    assert result["status"] == "unsafe"


@pytest.mark.parametrize("maximum,expected", [(100, 70), (50, 50)])
def test_room_search_uses_the_same_legal_domain_including_quiet_seed(maximum, expected):
    from test_rooms import labels, member
    from tuner_under_test.rooms import fitting

    item = member("any-radar-name", [80] * 100 + [70] * 100, labels())
    item["thresholds"]["g0_move"] = 77
    item["constraints"] = {"g0_move": {"min": 0, "max": maximum, "step": 10}}
    result = fitting.fit({"any-radar-name": item}, 0, 1200)
    assert result["thresholds"]["any-radar-name"]["g0_move"] == expected
    assert result["after"]["room"]["false_positive_percent"] == (0 if maximum == 100 else 100)


def test_cached_comparison_cannot_reuse_a_different_advertised_domain():
    from tuner_under_test.calibration import comparison_cache

    pattern = {"thresholds": {"g0_move": 70}}
    first = {
        "entities": {"g0_move": "number.any"},
        "threshold_constraints": {"g0_move": {"min": 0, "max": 100, "step": 10}},
    }
    changed = {**first, "threshold_constraints": {"g0_move": {"min": 0, "max": 100, "step": 20}}}
    assert comparison_cache.pattern_key(first, pattern) != comparison_cache.pattern_key(
        changed, pattern
    )


class RuntimeThresholdTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_automatic_weight.DeviceLearnTests.setUp
    asyncTearDown = test_automatic_weight.DeviceLearnTests.asyncTearDown
    configuration = test_automatic_weight.DeviceLearnTests.configuration
    labelled = test_automatic_weight.DeviceLearnTests.labelled

    def constrained(self):
        entities, current = self.labelled()
        for entity in entities.values():
            self.states[entity].attributes = {"min": 0, "max": 100, "step": 10}
        return entities, current

    def test_runtime_learning_uses_each_advertised_domain(self):
        entities, current = self.constrained()
        result = self.runtime._fit_history("a", entities, current)
        assert all(value["threshold"] % 10 == 0 for value in result["proposals"].values())
        assert result["threshold_constraints"]["g0_move"]["step"] == 10

    async def test_metadata_change_during_fit_rejects_installation(self):
        from unittest.mock import AsyncMock, patch

        from tuner_under_test.calibration import service

        entities, current = self.constrained()

        async def executor(fn, *args):
            self.states[entities["g0_move"]].attributes["step"] = 20
            return {}

        self.worker_transport.respond = executor
        with (
            patch.object(service, "read_timing", return_value={}),
            patch.object(
                service.recovery, "prepare_learning", AsyncMock(return_value=(entities, current))
            ),
            self.assertRaisesRegex(service.LearningEvidenceChanged, "constraints"),
        ):
            await service._learn_once(self.runtime, "a")

    async def test_saved_domain_change_rejects_apply_before_any_device_preparation(self):
        from unittest.mock import AsyncMock, patch

        from tuner_under_test.calibration import service

        entities, current = self.constrained()
        self.device["last_learning"] = self.runtime._fit_history("a", entities, current)
        self.states[entities["g0_move"]].attributes["step"] = 20
        with patch.object(service.device_io, "prepare_device", AsyncMock()) as prepare:
            with self.assertRaisesRegex(ValueError, "constraints"):
                await service._apply_validated(self.runtime, "a")
        prepare.assert_not_awaited()

    async def test_off_grid_saved_candidate_is_rejected_before_preparation(self):
        from unittest.mock import AsyncMock, patch

        from tuner_under_test.calibration import service

        entities, current = self.constrained()
        result = self.runtime._fit_history("a", entities, current)
        result["proposals"]["g0_move"]["threshold"] = 77
        self.device["last_learning"] = result
        with patch.object(service.device_io, "prepare_device", AsyncMock()) as prepare:
            with self.assertRaisesRegex(ValueError, "permitted"):
                await service._apply_validated(self.runtime, "a")
        prepare.assert_not_awaited()

    def test_comparison_does_not_offer_off_grid_saved_result_for_apply(self):
        from tuner_under_test.calibration import comparison_jobs

        entities, current = self.constrained()
        result = self.runtime._fit_history("a", entities, current)
        result["proposals"]["g0_move"]["threshold"] = 77
        self.device["last_learning"] = result
        assert not comparison_jobs._context(self.runtime, "a")[0]["patterns"]["user"]["applicable"]

    async def test_manual_threshold_uses_advertised_step_before_writing(self):
        from unittest.mock import AsyncMock, patch

        from tuner_under_test.calibration import service

        self.constrained()
        with (
            patch.object(service.device_io, "prepare_device", AsyncMock(return_value=None)),
            patch.object(
                service.device_io, "write_threshold", AsyncMock(return_value={"value": 80})
            ) as write,
        ):
            result = await service.set_gate_threshold(self.runtime, "a", "g0_move", 77)
        assert write.await_args.args[3] == 80
        assert result["value"] == 80

    def test_comparison_and_room_contexts_track_each_advertised_gate_domain(self):
        from tuner_under_test.calibration import comparison_jobs
        from tuner_under_test.rooms import jobs

        entities, current = self.constrained()
        self.device["last_learning"] = self.runtime._fit_history("a", entities, current)
        context, signature = comparison_jobs._context(self.runtime, "a")
        assert context["threshold_constraints"]["g0_move"]["step"] == 10
        self.runtime.data["room_groups"] = {
            "zone": {"name": "Any zone", "device_ids": ["a"], "kind": "zone", "area_id": None}
        }
        members, room_signature = jobs._context(self.runtime, "zone")
        assert members["a"]["constraints"]["g0_move"]["step"] == 10
        self.states[entities["g0_move"]].attributes["step"] = 20
        assert comparison_jobs._context(self.runtime, "a")[1] != signature
        assert jobs._context(self.runtime, "zone")[1] != room_signature

    async def test_joint_off_grid_candidate_is_rejected_before_device_preparation(self):
        from unittest.mock import AsyncMock, patch

        from tuner_under_test.rooms import application, jobs

        _entities, current = self.constrained()
        self.runtime.data["room_groups"] = {
            "zone": {"name": "Any zone", "device_ids": ["a"], "kind": "zone", "area_id": None}
        }
        _members, signature = jobs.set_context(self.runtime, ["zone"])
        learned = {
            "id": "joint",
            "group_id": "zone",
            "signature": signature,
            "thresholds": {"a": {**current, "g0_move": 77}},
        }
        self.device["learning_results"] = {"user": {"id": "joint"}}
        with patch.object(application.device_io, "prepare_device", AsyncMock()) as prepare:
            with self.assertRaisesRegex(ValueError, "permitted"):
                await application._write(self.runtime, learned, None)
        prepare.assert_not_awaited()

    async def test_manual_domain_change_during_preparation_rejects_before_write(self):
        from unittest.mock import AsyncMock, patch

        from tuner_under_test.calibration import service

        entities, _ = self.constrained()

        async def prepare(*args):
            self.states[entities["g0_move"]].attributes["step"] = 30

        with (
            patch.object(service.device_io, "prepare_device", AsyncMock(side_effect=prepare)),
            patch.object(service.device_io, "write_threshold", AsyncMock()) as write,
        ):
            with self.assertRaisesRegex(ValueError, "constraints"):
                await service.set_gate_threshold(self.runtime, "a", "g0_move", 20)
        write.assert_not_awaited()

    def test_comparison_rejects_missing_or_changed_learning_policy(self):
        from copy import deepcopy

        from tuner_under_test.calibration import comparison_jobs

        entities, current = self.constrained()
        learned = self.runtime._fit_history("a", entities, current)
        for field, value in (
            ("learner_method", None),
            ("quality_version", -1),
            ("automatic_weight", 0.5),
        ):
            changed = deepcopy(learned)
            if value is None:
                changed.pop(field)
            else:
                changed[field] = value
            self.device["learning_results"] = {"user": changed}
            context, _ = comparison_jobs._context(self.runtime, "a")
            assert not context["patterns"]["user"]["applicable"], field
        self.device["learning_results"] = {"user": learned}
        assert comparison_jobs._context(self.runtime, "a")[0]["patterns"]["user"]["applicable"]

    async def test_apply_rejects_unversioned_learning_before_preparation(self):
        from unittest.mock import AsyncMock, patch

        from tuner_under_test.calibration import service

        entities, current = self.constrained()
        learned = self.runtime._fit_history("a", entities, current)
        learned.pop("learner_method")
        self.device["last_learning"] = learned
        with patch.object(service.device_io, "prepare_device", AsyncMock()) as prepare:
            with self.assertRaisesRegex(ValueError, "policy"):
                await service._apply_validated(self.runtime, "a")
        prepare.assert_not_awaited()

    def test_joint_unchanged_off_grid_baseline_is_preserved_without_writes(self):
        from tuner_under_test.rooms import application, jobs

        entities, current = self.constrained()
        self.states[entities["g0_move"]].state = "77"
        self.runtime.data["room_groups"] = {
            "zone": {"name": "Any zone", "device_ids": ["a"], "kind": "zone", "area_id": None}
        }
        _members, signature = jobs.set_context(self.runtime, ["zone"])
        target = {**signature["a"]["thresholds"], "g0_still": 20}
        learned = {
            "id": "joint",
            "group_id": "zone",
            "signature": signature,
            "thresholds": {"a": target},
        }
        self.device["learning_results"] = {"user": {"id": "joint"}}
        application.guard(self.runtime, learned, {})
        assert application._changes(learned) == [(10, "a", "g0_still", 20)]
        assert learned["thresholds"]["a"]["g0_move"] == 77

    async def test_joint_no_change_does_not_prepare_or_write_inactive_device(self):
        from unittest.mock import AsyncMock, patch

        from tuner_under_test.rooms import application, jobs

        entities, current = self.constrained()
        self.states[entities["g0_move"]].state = "77"
        self.runtime.data["room_groups"] = {
            "zone": {"name": "Any zone", "device_ids": ["a"], "kind": "zone", "area_id": None}
        }
        _members, signature = jobs.set_context(self.runtime, ["zone"])
        learned = {
            "id": "joint",
            "group_id": "zone",
            "signature": signature,
            "thresholds": {"a": dict(signature["a"]["thresholds"])},
        }
        self.device["learning_results"] = {"user": {"id": "joint"}}
        with (
            patch.object(application.device_io, "prepare_device", AsyncMock()) as prepare,
            patch.object(application.device_io, "write_threshold", AsyncMock()) as write,
        ):
            result = await application._write(self.runtime, learned, None)
        prepare.assert_not_awaited()
        write.assert_not_awaited()
        assert result["applied"] == {}
        assert result["skipped"] == {}
        assert "last_applied" not in self.device

    async def test_physical_restore_snapshot_needs_no_learner_policy(self):
        from tuner_under_test.calibration import comparison_jobs, results, service

        entities, current = self.constrained()
        learned = self.runtime._fit_history("a", entities, current)
        results.remember_applied(self.device, learned, entities, current)
        restore = results.saved_results(self.device)["previous"]
        assert restore["source"] == "device"
        assert "learner_method" not in restore
        service._validate_current_evidence(self.runtime, "a", restore)
        assert comparison_jobs._context(self.runtime, "a")[0]["patterns"]["previous"]["applicable"]
