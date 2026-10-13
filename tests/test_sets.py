"""Learning sets: connected components of multi-radar groups linked by shared radars."""

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_rooms as harness
from tuner_under_test.calibration import jobs
from tuner_under_test.rooms import groups, learning, sets


def zone(*device_ids, area="office"):
    return {"kind": "zone", "area_id": area, "device_ids": list(device_ids)}


def area(*device_ids, area_id="office"):
    return {"kind": "area", "area_id": area_id, "device_ids": list(device_ids)}


def test_disjoint_zones_are_separate_sets():
    found = sets.learning_sets({"z1": zone("a", "b"), "z2": zone("c", "d")})
    assert found == {
        "z1": {"group_ids": ["z1"], "device_ids": ["a", "b"]},
        "z2": {"group_ids": ["z2"], "device_ids": ["c", "d"]},
    }


def test_shared_radar_links_zones():
    found = sets.learning_sets({"z1": zone("a", "b"), "z2": zone("b", "c"), "z3": zone("x", "y")})
    assert found["z1+z2"] == {"group_ids": ["z1", "z2"], "device_ids": ["a", "b", "c"]}
    assert set(found) == {"z1+z2", "z3"}


def test_chain_of_zones_forms_one_set():
    found = sets.learning_sets({"z1": zone("a", "b"), "z2": zone("b", "c"), "z3": zone("c", "d")})
    assert list(found) == ["z1+z2+z3"]
    assert found["z1+z2+z3"]["device_ids"] == ["a", "b", "c", "d"]


def test_area_with_zones_excluded():
    found = sets.learning_sets({"area:office": area("a", "b"), "z1": zone("a", "c")})
    assert list(found) == ["z1"]


def test_area_without_zones_takes_part():
    found = sets.learning_sets(
        {"area:den": area("a", "b", area_id="den"), "z1": zone("b", "c", area="other")}
    )
    assert list(found) == ["area:den+z1"]


def test_area_less_group_links_across_areas():
    found = sets.learning_sets(
        {
            "area:den": area("a", "b", area_id="den"),
            "area:hall": area("c", "d", area_id="hall"),
            "link": {"kind": "zone", "area_id": None, "device_ids": ["b", "c"]},
        }
    )
    assert list(found) == ["area:den+area:hall+link"]
    assert found["area:den+area:hall+link"]["device_ids"] == ["a", "b", "c", "d"]


def test_single_radar_group_not_a_set():
    assert sets.learning_sets({"z1": zone("a"), "z2": zone("a", "b")}) == {
        "z2": {"group_ids": ["z2"], "device_ids": ["a", "b"]}
    }
    assert sets.learning_sets({"z1": zone("a")}) == {}
    assert sets.learning_sets({}) == {}


def test_set_id_deterministic():
    one = {"zb": zone("b", "c"), "za": zone("a", "b")}
    other = {"za": zone("a", "b"), "zb": zone("b", "c")}
    assert list(sets.learning_sets(one)) == list(sets.learning_sets(other)) == ["za+zb"]


def test_one_group_set_id_equals_group_id():
    found = sets.learning_sets({"zone:abc": zone("a", "b")})
    assert list(found) == ["zone:abc"]


def test_set_for_device_finds_its_set():
    found = sets.learning_sets({"z1": zone("a", "b"), "z2": zone("c", "d")})
    assert sets.set_for_device(found, "c") == "z2"
    assert sets.set_for_device(found, "zz") is None


class RuntimeSetTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.GroupTests.setUp
    asyncTearDown = harness.GroupTests.asyncTearDown
    office = harness.GroupTests.office
    __test__ = True

    def linked(self):
        self.office()
        first = groups.configure(self.runtime, None, "Desk", "office", ["a", "b"])["id"]
        second = groups.configure(self.runtime, None, "Sofa", "office", ["a", "b"])["id"]
        return "+".join(sorted((first, second)))

    def test_for_device_resolves_linked_set_and_no_multiple_zones_error(self):
        set_id = self.linked()
        assert groups.for_device(self.runtime, "a") == set_id
        assert groups.for_device(self.runtime, "b") == set_id

    def test_for_device_one_group_returns_group_id(self):
        self.office()
        assert groups.for_device(self.runtime, "a") == "area:office"

    async def test_linked_set_device_learn_starts_the_set(self):
        set_id = self.linked()
        with (
            patch.object(learning.recovery, "prepare_learning", AsyncMock()),
            patch.object(learning.time, "time", return_value=1200),
        ):
            await jobs.start(self.runtime, "a")
        job = self.runtime.data["room_learning"][set_id]["job"]
        assert job["status"] == "completed", job.get("error")
        assert self.runtime.data["devices"]["b"]["learning_job"] is job
        await asyncio.sleep(0)

    def test_overnight_plan_learns_linked_set_once_then_ungrouped_radars(self):
        from tuner_under_test.runtime import schedule

        self.linked()
        # The office area has zones, so c (in no zone) is an ungrouped radar.
        self.runtime.data["devices"]["c"] = dict(self.device)
        self.runtime.data["devices"]["paused"] = {**self.device, "recording_enabled": False}
        devices = list(self.runtime.data["devices"].items())
        plan = [(key, members) for key, _device, members in schedule._plan(self.runtime, devices)]
        assert plan == [("a", ["a", "b"]), ("c", [])]

    def test_snapshot_lists_learning_sets(self):
        set_id = self.linked()
        for key in ("a", "b"):
            self.registry.entities[f"sensor.{key}_g0_move_energy"] = types.SimpleNamespace(
                entity_id=f"sensor.{key}_g0_move_energy", device_id=key, domain="sensor"
            )
        found = self.runtime.snapshot()["learning_sets"]
        assert found[set_id]["device_ids"] == ["a", "b"]
        assert found[set_id]["group_ids"] == set_id.split("+")

    def _snapshot(self):
        for key in ("a", "b"):
            self.registry.entities[f"sensor.{key}_g0_move_energy"] = types.SimpleNamespace(
                entity_id=f"sensor.{key}_g0_move_energy", device_id=key, domain="sensor"
            )
        return self.runtime.snapshot()

    def test_snapshot_learning_sets_carry_the_set_entry_not_a_stale_zone_entry(self):
        set_id = self.linked()
        first = set_id.split("+")[0]
        entry = {"job": {"status": "completed"}, "slots": {"user": {"id": "linked"}}}
        stale = {"slots": {"user": {"id": "from-before-the-zone-joined"}}}
        self.runtime.data["room_learning"] = {set_id: entry, first: stale}
        found = self._snapshot()["learning_sets"]
        assert found[set_id]["learning"] == entry
        assert found[set_id]["group_ids"] == set_id.split("+")

    def test_snapshot_one_group_set_learning_matches_its_group(self):
        self.office()
        entry = {"slots": {"user": {"id": "office"}}}
        self.runtime.data["room_learning"] = {"area:office": entry}
        snapshot = self._snapshot()
        assert snapshot["learning_sets"]["area:office"]["learning"] == entry
        assert snapshot["rooms"]["groups"]["area:office"]["learning"] == entry

    def test_snapshot_learning_sets_without_learning_are_none(self):
        set_id = self.linked()
        assert self._snapshot()["learning_sets"][set_id]["learning"] is None

    async def test_snapshot_learning_sets_show_the_running_job(self):
        set_id = self.linked()
        release = asyncio.Event()

        async def prepare(*_args):
            await release.wait()

        with (
            patch.object(learning.recovery, "prepare_learning", prepare),
            patch.object(learning.time, "time", return_value=1200),
        ):
            learning.start_set(self.runtime, set_id)
            running = self._snapshot()["learning_sets"][set_id]["learning"]["job"]
            assert running["status"] == "running"
            assert running["group_ids"] == set_id.split("+")
            release.set()
            await self.runtime._room_learning_jobs[set_id]
        found = self._snapshot()["learning_sets"][set_id]["learning"]
        assert found["job"]["status"] == "completed", found["job"].get("error")
        assert found["slots"]["user"]["group_ids"] == set_id.split("+")
