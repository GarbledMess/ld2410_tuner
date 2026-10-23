"""Work on every panel poll stays small however many entities the Home Assistant registry holds:
the registry is read once per change, not once per radar and question."""

import random
import sys
import time
import types
import unittest
from copy import deepcopy

import old_background as old
import test_tuner as harness
from test_rooms import GroupTests

discovery = sys.modules["tuner_under_test.runtime.discovery"]
snapshots = sys.modules["tuner_under_test.presentation.snapshots"]
timing_config = sys.modules["tuner_under_test.calibration.timing_config"]
service = sys.modules["tuner_under_test.calibration.service"]


class Counting(dict):
    """A registry's entity dictionary that counts how often it is walked."""

    walks = 0

    def values(self):
        self.walks += 1
        return super().values()


def entity(entity_id, device_id, domain, **extra):
    return types.SimpleNamespace(
        entity_id=entity_id,
        device_id=device_id,
        domain=domain,
        disabled_by=None,
        original_name=extra.pop("original_name", None),
        translation_key=extra.pop("translation_key", None),
        **extra,
    )


def radar_entities(device_id, gates=9):
    found = {}
    for gate in range(gates):
        for kind in ("move", "still"):
            for metric, domain in (("energy", "sensor"), ("threshold", "number")):
                entity_id = f"{domain}.{device_id}_g{gate}_{kind}_{metric}"
                found[entity_id] = entity(entity_id, device_id, domain)
    for name, domain in (
        (f"number.{device_id}_timeout", "number"),
        (f"number.{device_id}_presence_on_delay", "number"),
        (f"number.{device_id}_presence_off_delay", "number"),
        (f"sensor.{device_id}_still_energy", "sensor"),
    ):
        found[name] = entity(name, device_id, domain)
    return found


def random_registry(rng, devices=("a", "b", "c")):
    """Entities of radars and of other things, in no particular order."""
    found = {}
    for device_id in devices:
        found.update(radar_entities(device_id, rng.choice((1, 2, 9, 9))))
    for index in range(rng.randrange(0, 60)):
        domain = rng.choice(("sensor", "number", "switch", "light"))
        owner = rng.choice((*devices, "other", None))
        name = f"{domain}.filler_{index}" + rng.choice(("", "", "_timeout", "_presence_on_delay"))
        found[name] = entity(name, owner, domain)
    items = list(found.items())
    rng.shuffle(items)
    return dict(items)


class RegistryReadsTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    office = GroupTests.office

    def stock(self, registry):
        self.registry.entities = Counting(registry)
        for entity_id, item in registry.items():
            if item.domain == "number" and "threshold" in entity_id:
                self.states[entity_id] = types.SimpleNamespace(state="25", attributes={})
        self.states["number.a_timeout"] = types.SimpleNamespace(state="4", attributes={})

    def walks(self):
        return self.registry.entities.walks

    def poll(self):
        """What every poll asks of the registry, for the radar ``a``."""
        self.runtime.refresh_devices(self.registry)
        snapshots._snapshot_thresholds(self.runtime, "a", self.registry)
        timing_config.read_timing(self.runtime, "a")
        timing_config.read_timing(self.runtime, "a")
        return service._threshold_configuration(self.runtime, "a")

    def test_the_registry_is_read_once_and_not_again_while_it_is_unchanged(self):
        self.stock({**radar_entities("a"), **radar_entities("b")})
        self.poll()
        self.assertEqual(self.walks(), 1, "once for everything a poll asks")
        for _ in range(5):
            self.poll()
        self.assertEqual(self.walks(), 1, "nothing changed")

    def test_an_added_or_removed_entity_is_seen(self):
        self.stock(radar_entities("a"))
        before = self.poll()
        self.registry.entities["sensor.a_g8_move_energy"] = entity(
            "sensor.a_g8_move_energy", "a", "sensor"
        )
        self.runtime.refresh_devices(self.registry)
        energies = self.runtime.data["devices"]["a"]["entities"]
        self.assertIn("sensor.a_g8_move_energy", energies)
        del self.registry.entities["sensor.a_g8_move_energy"]
        self.runtime.refresh_devices(self.registry)
        self.assertNotIn("sensor.a_g8_move_energy", self.runtime.data["devices"]["a"]["entities"])
        self.assertEqual(self.poll(), before)

    def test_a_registry_update_is_seen_even_when_the_entity_count_is_the_same(self):
        self.stock(radar_entities("a"))
        self.assertEqual(
            snapshots._snapshot_thresholds(self.runtime, "a", self.registry)["g0_move"], 25
        )
        self.registry.entities["number.a_g0_move_threshold"] = entity(
            "number.a_g0_move_threshold", "b", "number"
        )  # Moved to another radar, as an entity registry update does.
        self.office()
        self.runtime.handle_registry_update(types.SimpleNamespace(data={}))
        found = snapshots._snapshot_thresholds(self.runtime, "a", self.registry)
        self.assertNotIn("g0_move", found)

    def test_another_registry_is_read_afresh_whatever_its_size(self):
        self.stock(radar_entities("a"))
        mine = snapshots._snapshot_thresholds(self.runtime, "a", self.registry)
        self.assertEqual(len(mine), 18)
        elsewhere = types.SimpleNamespace(entities=Counting(radar_entities("b")))
        self.assertEqual(len(elsewhere.entities), len(self.registry.entities))
        self.assertEqual(snapshots._snapshot_thresholds(self.runtime, "a", elsewhere), {})

    def test_timing_entities_are_worked_out_once_for_each_registry_read(self):
        from unittest.mock import patch

        from tuner_under_test import entity_index

        self.stock(radar_entities("a"))
        with patch.object(timing_config, "_names", wraps=timing_config._names) as names:
            first = timing_config.read_timing(self.runtime, "a")
            asked = names.call_count
            self.assertGreater(asked, 0)
            self.assertEqual(timing_config.read_timing(self.runtime, "a"), first)
            self.assertEqual(names.call_count, asked, "the registry has not changed")
            entity_index.forget(self.runtime)
            self.assertEqual(timing_config.read_timing(self.runtime, "a"), first)
            self.assertEqual(names.call_count, 2 * asked, "it has: asked again")

    def test_the_index_answers_as_the_walks_it_replaced(self):
        rng = random.Random(1)
        for _ in range(40):
            registry = random_registry(rng)
            self.stock(registry)
            self.runtime._entity_index = None
            data = {
                "devices": {
                    "a": {"entities": {"sensor.stale": {}}, "recording_enabled": True},
                    "gone": {"entities": {"sensor.gone": {}}},
                }
            }
            self.runtime.data["devices"] = deepcopy(data["devices"])
            expected = deepcopy(data["devices"])
            old.refresh_devices(expected, self.registry)
            self.runtime.refresh_devices(self.registry)
            self.assertEqual(self.runtime.data["devices"], expected)
            self.assertEqual(list(self.runtime.data["devices"]), list(expected))
            for device_id in ("a", "b", "c", "other", "nobody"):
                self.assertEqual(
                    snapshots._snapshot_thresholds(self.runtime, device_id, self.registry),
                    old.snapshot_thresholds(self.runtime, device_id, self.registry),
                )
                self.assertEqual(
                    timing_config._candidates(self.runtime, device_id),
                    old.candidates(self.runtime, device_id, self.registry),
                )
                try:
                    wanted = old.threshold_configuration(self.runtime, device_id, self.registry)
                except ValueError as error:
                    with self.assertRaises(ValueError) as raised:
                        service._threshold_configuration(self.runtime, device_id)
                    self.assertEqual(str(raised.exception), str(error))
                else:
                    self.assertEqual(
                        service._threshold_configuration(self.runtime, device_id), wanted
                    )


class SnapshotReadsTests(unittest.IsolatedAsyncioTestCase):
    """A whole poll, with a registry as big as a busy home's."""

    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    office = GroupTests.office

    async def test_a_poll_reads_the_registry_once_when_it_changes_and_never_otherwise(self):
        self.office()
        entities = Counting(self.registry.entities)
        for index in range(2000):
            name = f"sensor.filler_{index}"
            entities[name] = entity(name, f"dev{index // 5}", "sensor")
        for key in ("a", "b"):
            entities.update(radar_entities(key))
        self.registry.entities = entities
        self.runtime.snapshot()
        first = entities.walks
        self.assertLessEqual(first, 2, "built once")
        for _ in range(3):
            self.runtime.snapshot()
        self.assertEqual(entities.walks, first, "unchanged: not read again")


def guess_samples(rng, count, first=0.0):
    """Rows with a recorded guess: ``(code, confidence)`` per sample, in steady stretches."""
    records, code, confidence = [], 1, 90
    for index in range(count):
        if rng.random() < 0.08:
            code, confidence = rng.choice((0, 1, 2, 1, 2)), rng.choice((40, 60, 80, 99))
        t = first + index * 6 + (rng.choice((0, 0, 0, 40)) if rng.random() < 0.03 else 0)
        records.append((t, [0] * 18 + [code, confidence]))
    return records


def stub_runtime(records):
    """Each member (``records`` key) with its own recorded rows and guesses."""
    runtime = types.SimpleNamespace(
        data={"devices": {key: {} for key in records}, "room_labels": {}},
        _history_runtime={},
        _schedule_save=lambda: None,
        _room_recent=None,
    )
    for key, rows in records.items():
        harness.record_rows(runtime, key, rows)
    return runtime


class EstimateTests(unittest.TestCase):
    def test_the_estimate_is_what_it_was_for_any_recorded_guesses(self):
        from unittest.mock import patch

        from tuner_under_test.rooms import room_labels

        rng = random.Random(7)
        for _ in range(120):
            members = {
                key: guess_samples(rng, rng.choice((3, 20, 80, 700)), first=offset)
                for key, offset in (("a", 0.0), ("b", 3.0), ("c", 1.5))
                if rng.random() < 0.8 or key == "a"
            }
            runtime = stub_runtime(members)
            newest = max(rows[-1][0] for rows in members.values())
            for now in (newest + 1, newest + 7, newest + 14, newest + 400, newest + 5000):
                with patch.object(room_labels.time, "time", return_value=now):
                    ids = list(members)
                    self.assertEqual(
                        room_labels.current_estimate(runtime, ids),
                        uncached(runtime, ids),
                    )


def uncached(runtime, ids):
    """The room estimate computed afresh, without the poll's kept member reads."""
    from tuner_under_test.rooms import room_labels

    kept, runtime._room_recent = runtime._room_recent, None
    try:
        return room_labels.current_estimate(runtime, ids)
    finally:
        runtime._room_recent = kept


class EstimatePollTests(unittest.IsolatedAsyncioTestCase):
    """The room estimate of each poll, from a runtime that records as it runs."""

    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    office = GroupTests.office

    def record(self, key, t, state, confidence=1.0):
        self.runtime.data["devices"][key].setdefault("auto", {})["last_classification"] = {
            "state": state,
            "confidence": confidence,
            "timestamp": t,
        }
        self.runtime._record_history_sample(key, {"g0_move": 60}, t)

    def poll(self, now):
        from unittest.mock import patch

        from tuner_under_test.rooms import jobs

        with patch.object(
            sys.modules["tuner_under_test.rooms.room_labels"].time, "time", return_value=now
        ):
            return jobs.snapshot_with_inventory(self.runtime)[0]["groups"]

    def test_polls_that_find_new_records_publish_the_uncached_estimate(self):
        from unittest.mock import patch

        from tuner_under_test.rooms import groups, room_labels

        self.office()
        zone = groups.configure(self.runtime, None, "Desk", "office", ["a"])["id"]
        rng = random.Random(11)
        state = "present"
        base = self.now - 3000
        for step in range(300):
            if rng.random() < 0.1:
                state = rng.choice(("present", "not_present", "unknown"))
            self.record("a", base + step * 6, state)
            self.record("b", base + step * 6 + 3, rng.choice((state, "present")))
            if rng.random() < 0.05:
                self.runtime._days.flush(base + step * 6 + 3)
            now = base + step * 6 + rng.choice((4, 8, 20))
            published = self.poll(now)
            with patch.object(room_labels.time, "time", return_value=now):
                for key, ids in (("area:office", ["a", "b"]), (zone, ["a"])):
                    self.assertEqual(published[key]["estimate"], uncached(self.runtime, ids))

    def test_only_members_with_new_records_are_read_again(self):
        from unittest.mock import patch

        from tuner_under_test.history import guesses
        from tuner_under_test.rooms import groups

        self.office()
        groups.configure(self.runtime, None, "Desk", "office", ["a"])
        base = self.now - 3000
        for step in range(400):
            self.record("a", base + step * 6, "present")
            self.record("b", base + step * 6 + 3, "present")
        newest = base + 399 * 6 + 3
        self.poll(newest + 1)
        with patch.object(guesses, "evidence", wraps=guesses.evidence) as read:
            self.poll(newest + 1)  # Nothing new: every member's recent runs are kept.
            self.assertEqual(read.call_count, 0)
            self.record("a", newest + 3, "not_present")
            self.poll(newest + 4)
            self.assertEqual({call.args[1] for call in read.call_args_list}, {"a"})

    def test_a_cleared_member_is_read_again(self):
        """The newest observation is remembered between polls; clearing a member's history
        must not leave that memory behind."""
        from unittest.mock import patch

        from tuner_under_test.rooms import room_labels

        self.office()
        base = self.now - 600
        for step in range(70):
            self.record("a", base + step * 6, "present")
            self.record("b", base + step * 6 + 3, "present")
        now = base + 70 * 6 + 2
        with patch.object(room_labels.time, "time", return_value=now):
            before = room_labels.current_estimate(self.runtime, ["a", "b"])
            self.assertEqual(before["state"], "present")  # Both members agree.
            self.runtime.clear_samples("b")
            after = room_labels.current_estimate(self.runtime, ["a", "b"])
            # Present needs both members of a two-radar room: a alone is not the room.
            self.assertIsNone(after)
            self.assertEqual(after, uncached(self.runtime, ["a", "b"]))

    def test_polls_that_go_back_in_time_see_the_runs_they_would_have_seen(self):
        from unittest.mock import patch

        from tuner_under_test.rooms import room_labels

        self.office()
        base = self.now - 5100
        for step in range(840):
            confidence = 1.0 if step // 15 % 2 else 0.65  # Runs of one guess, all Present.
            self.record("a", base + step * 6, "present", confidence)
            self.record("b", base + step * 6 + 3, "present", confidence)
        self.runtime._days.flush(base + 839 * 6 + 3)
        newest = base + 839 * 6 + 3
        for now in (newest + 4, newest - 2400, newest + 2):
            with patch.object(room_labels.time, "time", return_value=now):
                self.assertEqual(
                    room_labels.current_estimate(self.runtime, ["a", "b"]),
                    uncached(self.runtime, ["a", "b"]),
                    now - newest,
                )

    async def test_a_member_whose_coverage_ended_keeps_no_current_estimate(self):
        """A member no longer observed (recording paused) keeps its guesses as history, but
        cannot keep an estimate current; an observed member alone still does."""
        from unittest.mock import patch

        from tuner_under_test.rooms import room_labels

        self.office()
        base = self.now - 600
        for step in range(50):
            self.record("a", base + step * 6, "present")
            if step < 10:
                self.record("b", base + step * 6 + 3, "present")
        with patch.object(time, "time", return_value=base + 60):
            self.runtime.set_recording("b", False)
        now = base + 50 * 6 + 2
        with patch.object(room_labels.time, "time", return_value=now):
            self.assertEqual(room_labels.current_estimate(self.runtime, ["a"])["state"], "present")
            self.assertIsNone(room_labels.current_estimate(self.runtime, ["b"]))
            self.assertEqual(
                room_labels.current_estimate(self.runtime, ["a", "b"]),
                uncached(self.runtime, ["a", "b"]),
            )

    def test_a_member_label_change_is_seen_by_the_kept_reads(self):
        """A label edit rebuilds a member's labelled histograms, which decide where its present
        guesses were quiet (the veto): reads kept from before must not survive it."""
        from unittest.mock import patch

        from tuner_under_test.rooms import room_labels

        self.office()
        base = self.now - 700
        for step in range(70):
            self.record("a", base + step * 6, "present")
            self.record("b", base + step * 6 + 3, "present")
        now = base + 70 * 6 + 2
        device = self.runtime.data["devices"]["a"]
        with patch.object(room_labels.time, "time", return_value=now):
            self.assertEqual(
                room_labels.current_estimate(self.runtime, ["a", "b"])["state"], "present"
            )
            # As a label edit leaves it: a's readings of 60 were labelled Empty, so are quiet.
            device["histograms"] = {
                "g0_move": {
                    "present": [0] * 101,
                    "not_present": [200 if i == 60 else 0 for i in range(101)],
                }
            }
            device["label_revision"] = device.get("label_revision", 0) + 1
            self.assertIsNone(uncached(self.runtime, ["a", "b"]))
            self.assertIsNone(room_labels.current_estimate(self.runtime, ["a", "b"]))

    def test_the_estimate_follows_history_edits_and_cleanup_between_polls(self):
        from unittest.mock import patch

        from tuner_under_test.rooms import room_labels

        self.office()
        base = self.now - 700
        for step in range(70):
            self.record("a", base + step * 6, "present")
            self.record("b", base + step * 6 + 3, "present")
        now = base + 70 * 6 + 2

        def agree():
            with patch.object(room_labels.time, "time", return_value=now):
                self.assertEqual(
                    room_labels.current_estimate(self.runtime, ["a", "b"]),
                    uncached(self.runtime, ["a", "b"]),
                )

        agree()
        self.runtime.label_history_range("a", base, base + 120, "not_present")  # A history edit.
        agree()
        self.runtime._days.flush(now)  # Stored history changes under the kept reads.
        agree()
        self.runtime.clear_samples("b")  # A member's history is cleared.
        agree()

    def test_removed_radars_leave_the_estimate_memory(self):
        from tuner_under_test.rooms import jobs

        self.office()
        base = self.now - 100
        for step in range(5):
            self.record("a", base + step * 6, "present")
            self.record("b", base + step * 6 + 3, "present")
        self.poll(base + 40)
        self.assertEqual(set(self.runtime._room_recent), {"a", "b"})
        del self.runtime.data["devices"]["b"]
        jobs.snapshot_with_inventory(self.runtime)
        self.assertEqual(set(self.runtime._room_recent), {"a"})
