"""Joint fits preserve complementary and redundant detection, without inventing timing."""

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_rooms as harness
from test_rooms import labels, member
from tuner_under_test.rooms import application, fitting, groups, learning
from tuner_under_test.rooms.objective import Objective


def example(a, b, current=20):
    data = {"a": member("a", a, labels()), "b": member("b", b, labels())}
    for item in data.values():
        item["thresholds"]["g0_move"] = current
    return data


def test_strong_radar_does_not_force_weak_radar_to_detect():
    data = example([70] * 100 + [10] * 100, [3] * 200)
    result = fitting.fit(data, 0, 1200)
    assert result["thresholds"]["b"]["g0_move"] == 20
    assert 10 < result["thresholds"]["a"]["g0_move"] < 70
    assert result["cost_after"] <= result["cost_before"]


def test_both_strong_radars_retained_instead_of_picking_one_winner():
    data = example([70] * 100 + [10] * 100, [50] * 100 + [5] * 100, current=100)
    result = fitting.fit(data, 0, 1200)
    assert 10 < result["thresholds"]["a"]["g0_move"] < 70
    assert 5 < result["thresholds"]["b"]["g0_move"] < 50
    for item in result["after"]["devices"].values():
        assert item["outcomes"]["human"]["present"]["active_seconds"] > 590
    assert result["after"]["room"]["outcomes"]["human"]["present"]["active_seconds"] < 601


def test_complementary_handoffs_and_noisy_member():
    data = example([70] * 50 + [3] * 150, [3] * 50 + [60] * 50 + [30] * 100)
    result = fitting.fit(data, 0, 1200)
    assert result["thresholds"]["b"]["g0_move"] >= 30
    assert result["thresholds"]["b"]["g0_move"] < 60
    assert result["after"]["room"]["presence_recall"] > 0.99
    assert result["after"]["room"]["false_positive_percent"] == 0
    assert result["cost_after"] < result["cost_before"]


def test_search_cost_matches_report_with_different_timing_and_offsets():
    data = example([70] * 100 + [10] * 100, [50] * 100 + [5] * 100)
    data["a"]["timing"] = {"timeout": 1, "on_delay": 0.5, "off_delay": 1}
    data["b"]["timing"] = {"timeout": 8, "on_delay": 2, "off_delay": 3}
    objective = Objective(data, 0, 1200)
    for value in (0, 10, 30, 55, 75, 100):
        thresholds = {"a": {"g0_move": value}, "b": {"g0_move": 55 - value if value < 55 else 20}}
        report = fitting.measure(data, thresholds, 0, 1200)["room"]
        expected = 500 * (1 - report["presence_recall"]) + report["false_positive_percent"]
        assert abs(objective.costs(thresholds)[0] - expected) < 1e-8


def test_automatic_presence_cannot_overrule_human_empty_labels():
    item = member("a", [70] * 200, [{"start": 600, "end": 1200, "state": "not_present"}])
    records = [(i * 6, [70] + [255] * 17 + [1, 90]) for i in range(200)]
    item["view"]._iter_history_samples = lambda *args, **kwargs: iter(records)
    result = fitting.fit({"a": item}, 0, 1200)
    assert result["thresholds"]["a"]["g0_move"] >= 70
    assert result["after"]["room"]["outcomes"]["human"]["not_present"]["active_seconds"] == 0


class JointRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.GroupTests.setUp
    asyncTearDown = harness.GroupTests.asyncTearDown
    office = harness.GroupTests.office
    # Reuse the synthetic HA boundary without inheriting its test methods.
    __test__ = True

    async def test_joint_job_deduplicates_device_requests_and_saves_both(self):
        self.office()
        prepare = AsyncMock()
        with (
            patch.object(learning.recovery, "prepare_learning", prepare),
            patch.object(learning.time, "time", return_value=1200),
        ):
            first = learning.start_device(self.runtime, "a", "user", "area:office")
            second = learning.start_device(self.runtime, "b", "automatic", "area:office")
            a, b = await asyncio.gather(first, second)
        assert a["user"]["id"] == b["automatic"]["id"]
        assert set(self.runtime.data["devices"]["b"]["learning_results"]) == {"user", "automatic"}
        assert prepare.await_count == 2
        assert self.device["learning_job"]["status"] == "completed"
        assert self.hass.services.async_call.await_count == 0

    def test_zone_precedence_and_ambiguous_membership(self):
        self.office()
        desk = groups.configure(self.runtime, None, "Desk", "office", ["a"])
        assert groups.for_device(self.runtime, "a") is None
        assert groups.for_device(self.runtime, "b") is None
        with self.assertRaisesRegex(ValueError, "independent zones"):
            groups.learning_group(self.runtime, "area:office")
        groups.configure(self.runtime, desk["id"], "Desk", "office", ["a", "b"])
        assert groups.for_device(self.runtime, "b") == desk["id"]
        groups.configure(self.runtime, None, "Overlap", None, ["b"])
        with self.assertRaisesRegex(ValueError, "multiple zones"):
            groups.for_device(self.runtime, "b")

    async def _learn(self):
        self.office()
        with (
            patch.object(learning.recovery, "prepare_learning", AsyncMock()),
            patch.object(learning.time, "time", return_value=1200),
        ):
            await learning.start_device(self.runtime, "a", "user", "area:office")
        return self.runtime.data["room_learning"]["area:office"]["slots"]["user"]

    async def test_joint_apply_writes_both_and_blocks_changed_companion(self):
        learned = await self._learn()
        values = {
            key: dict(signature["thresholds"])
            for key, signature in learned["signature"].items()
            if key in ("a", "b")
        }
        self.runtime._threshold_configuration = lambda key: (
            {"g0_move": f"number.{key}"},
            dict(values[key]),
        )
        writes = []

        async def write(runtime, device, key, value, button):
            writes.append((device, key, value))
            values[device][key] = value

        with (
            patch.object(application.device_io, "prepare_device", AsyncMock(return_value=None)),
            patch.object(application.device_io, "write_threshold", write),
        ):
            result = await application.apply(self.runtime, "area:office", learned["id"], "user")
        assert not result["skipped"]
        assert {entry[0] for entry in writes} == {"a", "b"}
        assert (
            self.runtime.data["devices"]["b"]["learning_results"]["current"]["id"] == learned["id"]
        )
        values["b"]["g0_move"] = 99
        with self.assertRaisesRegex(ValueError, "zone member"):
            application.guard(self.runtime, learned, {})

    async def test_one_member_disallowing_auto_apply_blocks_entire_group(self):
        learned = await self._learn()
        self.runtime.data["learning_schedule"] = {"enabled": True, "auto_apply": True}
        self.runtime.data["devices"]["b"]["auto_apply_override"] = "off"
        report = {}
        await application.automatic(self.runtime, learned, report, "automatic")
        assert report["status"] == "skipped"
        assert "every zone member" in report["reason"]
        assert self.hass.services.async_call.await_count == 0

    async def test_partial_write_never_rotates_group_current(self):
        learned = await self._learn()
        with (
            patch.object(application.device_io, "prepare_device", AsyncMock(return_value=None)),
            patch.object(
                application.device_io,
                "write_threshold",
                AsyncMock(side_effect=ValueError("offline")),
            ),
        ):
            outcome = await application.apply(self.runtime, "area:office", learned["id"], "user")
        assert outcome["skipped"]
        for device in self.runtime.data["devices"].values():
            assert "current" not in device["learning_results"]

    async def test_existing_device_learn_routes_to_joint_job(self):
        from tuner_under_test.calibration import jobs as device_jobs

        self.office()
        with (
            patch.object(learning.recovery, "prepare_learning", AsyncMock()),
            patch.object(learning.time, "time", return_value=1200),
        ):
            report = device_jobs.start_learning(self.runtime, "a")
            assert report["group_id"] == "area:office"
            await self.runtime._learning_jobs["a"]
        assert (
            self.runtime.data["devices"]["b"]["last_learning"]["joint"]["group_id"] == "area:office"
        )

    async def test_joint_result_cannot_be_applied_as_one_radar(self):
        learned = await self._learn()
        with self.assertRaisesRegex(ValueError, "complete joint"):
            await self.runtime.apply("a", "user", learned["id"], {"g0_move": 20})
        assert self.hass.services.async_call.await_count == 0

    async def test_overnight_pass_visits_a_joint_group_once(self):
        from tuner_under_test.runtime import schedule

        self.office()
        self.runtime.data["learning_schedule"] = {"enabled": True, "auto_apply": False}
        self.runtime.async_save = AsyncMock()
        prepare = AsyncMock()
        with (
            patch.object(learning.recovery, "prepare_learning", prepare),
            patch.object(learning.time, "time", return_value=1200),
        ):
            await schedule._run(self.runtime, "2026-10-07")
        assert prepare.await_count == 2
        a, b = (item["nightly_learning"] for item in self.runtime.data["devices"].values())
        assert a["result_id"] == b["result_id"]
        assert a["automatic_apply"]["status"] == "skipped"

    def number_entities(self, device_id, threshold):
        # Home Assistant reports number entities as float strings, e.g. "20.0".
        states = {
            f"radar_{device_id}_max_{kind}_distance_gate": "2.0" for kind in ("move", "still")
        }
        for gate in range(3):
            for kind in ("move", "still"):
                states[f"radar_{device_id}_g{gate}_{kind}_threshold"] = threshold
        for name, state in states.items():
            entity_id = f"number.{name}"
            self.registry.entities[entity_id] = types.SimpleNamespace(
                device_id=device_id, domain="number", entity_id=entity_id
            )
            self.states[entity_id] = types.SimpleNamespace(state=state)

    async def test_overnight_joint_learn_reads_float_threshold_states(self):
        from tuner_under_test.runtime import schedule

        self.office()
        del self.runtime._threshold_configuration
        for device_id in ("a", "b"):
            self.number_entities(device_id, "20.0")
        # Gates 0-2 recorded, matching the 2-gate distance limit.
        records = [(i * 6, [40 if i < 100 else 0] * 6 + [255] * 12 + [0, 0]) for i in range(200)]

        def history_view(key):
            view = member(key, [], labels())["view"]
            view._iter_history_samples = lambda *args, **kwargs: iter(records)
            return view

        self.runtime._history_view = history_view
        self.runtime.data["learning_schedule"] = {"enabled": True, "auto_apply": False}
        self.runtime.async_save = AsyncMock()
        with (
            patch.object(learning.recovery, "prepare_learning", AsyncMock()),
            patch.object(learning.time, "time", return_value=1200),
        ):
            await schedule._run(self.runtime, "2026-10-08")
        for device in self.runtime.data["devices"].values():
            attempt = device["nightly_learning"]
            assert attempt["status"] != "error", attempt.get("error")
            assert attempt["result_id"]
