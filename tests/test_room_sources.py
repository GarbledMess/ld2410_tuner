"""Cameras, boolean entities and Bermuda attach to a room and write sensor room labels."""

import sys
import types
import unittest
from unittest.mock import patch

import test_rooms as rooms_harness
import test_tuner as harness
from tuner_under_test.rooms import groups, jobs, membership, room_labels, truth
from tuner_under_test.rooms.intervals import Timeline

room_sources = sys.modules["tuner_under_test.rooms.room_sources"]
GROUP = "area:office"
ENTITY = "binary_sensor.office_camera"


def values(**changes):
    return {
        "sources": [{"entity_id": ENTITY, "kind": "boolean", "area": ""}],
        "mark_not_present": False,
        "confidence": 80,
        "start_buffer_seconds": 0,
        "end_buffer_seconds": 0,
        **changes,
    }


class RoomSourceTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    office = rooms_harness.GroupTests.office

    def configure(self, **changes):
        return room_sources.configure_room_sources(self.runtime, GROUP, values(**changes))

    def entity(self, state):
        self.states[ENTITY] = types.SimpleNamespace(state=state, attributes={})

    def run_ticks(self, start, stop, step=2):
        for now in range(start, stop + 1, step):
            room_sources.sample_rooms(self.runtime, now)

    def sensor(self):
        return [
            x for x in room_labels.room_labels_for(self.runtime, GROUP) if x["source"] == "sensor"
        ]

    def spans(self):
        return [(x["start"], x["end"]) for x in self.sensor()]

    def test_room_source_present_extends_sensor_label(self):
        self.office()
        self.configure()
        self.entity("on")
        self.run_ticks(1000, 1006)
        self.assertEqual(
            self.sensor(),
            [
                {
                    "start": 1000,
                    "end": 1006,
                    "state": "present",
                    "source": "sensor",
                    "confidence": 0.8,
                }
            ],
        )
        self.run_ticks(1008, 1010)
        self.assertEqual(self.spans(), [(1000, 1010)])

    def test_room_bermuda_honours_max_distance(self):
        self.office()
        self.configure(
            sources=[
                {
                    "entity_id": "sensor.phone_area",
                    "kind": "bermuda",
                    "area": "office",
                    "max_distance": 3,
                }
            ],
            mark_not_present=True,
        )
        self.registry.entities.update(
            {
                "sensor.phone_area": types.SimpleNamespace(platform="bermuda", unique_id="phone"),
                "sensor.phone_range": types.SimpleNamespace(
                    platform="bermuda", unique_id="phone_range"
                ),
            }
        )
        self.states["sensor.phone_area"] = types.SimpleNamespace(state="office", attributes={})
        self.states["sensor.phone_range"] = types.SimpleNamespace(state="4.1", attributes={})
        self.run_ticks(1000, 1006)
        self.assertEqual({x["state"] for x in self.sensor()}, {"not_present"})
        self.states["sensor.phone_range"].state = "2.3"
        self.run_ticks(1008, 1014)
        self.assertEqual(self.sensor()[-1]["state"], "present")
        self.states["sensor.phone_range"].state = "unknown"
        self.run_ticks(1016, 1020)
        self.assertEqual(self.sensor()[-1]["end"], 1016)

    def test_sensor_writes_never_touch_manual_labels(self):
        self.office()
        room_labels.label_room_range(self.runtime, GROUP, 1000, 1004, "not_present")
        manual = [dict(x) for x in room_labels.room_labels_for(self.runtime, GROUP)]
        self.configure()
        self.entity("on")
        self.run_ticks(1000, 1008)
        labels = room_labels.room_labels_for(self.runtime, GROUP)
        self.assertEqual([x for x in labels if x["source"] == "manual"], manual)
        self.assertEqual(self.spans(), [(1000, 1008)])

    def test_extending_ticks_defer_retrain_until_the_label_closes(self):
        self.office()
        self.configure()
        self.entity("on")
        before = self.runtime.data["devices"]["a"].get("label_revision", 0)
        with patch.object(membership, "retrain", wraps=membership.retrain) as retrain:
            self.run_ticks(1000, 1020)
            retrain.assert_not_called()
            self.assertEqual(self.runtime.data["devices"]["a"].get("label_revision", 0), before)
            self.entity("off")
            self.run_ticks(1022, 1022)
            retrain.assert_called_once()
        # Deliberate change (final review I2): like device sources, a sensor-driven retrain
        # marks references stale without bumping member label revisions.
        self.assertEqual(self.runtime.data["devices"]["a"].get("label_revision", 0), before)
        self.assertEqual(self.runtime.data["devices"]["b"].get("label_revision", 0), before)
        # The tick that sees the entity go off still settles up to its own time.
        self.assertEqual(self.spans(), [(1000, 1022)])

    def test_room_source_buffers_delay_start(self):
        self.office()
        self.configure(start_buffer_seconds=10, end_buffer_seconds=6)
        self.entity("on")
        self.run_ticks(1000, 1014)
        self.assertEqual(self.sensor(), [])
        self.run_ticks(1016, 1030)
        # Starts after the start buffer; the end buffer holds back the newest seconds.
        self.assertEqual(self.spans(), [(1010, 1024)])
        self.entity("off")
        self.run_ticks(1032, 1032)
        self.assertEqual(self.spans(), [(1010, 1026)])
        self.run_ticks(1034, 1050)
        self.assertEqual(self.spans(), [(1010, 1026)])

    def test_mark_not_present_writes_empty(self):
        self.office()
        self.configure(mark_not_present=True)
        self.entity("off")
        self.run_ticks(1000, 1006)
        labels = self.sensor()
        self.assertEqual({x["state"] for x in labels}, {"not_present"})
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["confidence"], 0.8)
        self.assertEqual(labels[0]["end"], 1006)

    def test_off_without_mark_not_present_writes_nothing(self):
        self.office()
        self.configure()
        self.entity("off")
        self.run_ticks(1000, 1010)
        self.assertEqual(self.sensor(), [])

    def test_unavailable_entity_closes_label_and_never_bridges(self):
        self.office()
        self.configure()
        self.entity("on")
        self.run_ticks(1000, 1004)
        self.entity("unavailable")
        self.run_ticks(1006, 1010)
        self.entity("on")
        self.run_ticks(1012, 1016)
        self.assertEqual(self.spans(), [(1000, 1006), (1012, 1016)])

    def test_present_then_empty_changes_state(self):
        self.office()
        self.configure(mark_not_present=True)
        self.entity("on")
        self.run_ticks(1000, 1006)
        self.entity("off")
        self.run_ticks(1008, 1012)
        self.assertEqual(
            [(x["state"], x["start"], x["end"]) for x in self.sensor()],
            [("present", 1000, 1008), ("not_present", 1008, 1012)],
        )

    def test_confidence_change_keeps_separate_sensor_labels(self):
        self.office()
        self.configure()
        self.entity("on")
        self.run_ticks(1000, 1004)
        self.configure(confidence=50)
        self.run_ticks(1010, 1014)
        self.assertEqual([x["confidence"] for x in self.sensor()], [0.8, 0.5])
        room_labels.label_room_range(self.runtime, GROUP, 2000, 2010, "present")
        self.assertEqual([x["confidence"] for x in self.sensor()], [0.8, 0.5])

    def test_configuring_room_sources_marks_assessment_stale(self):
        self.office()
        _, signature = jobs._context(self.runtime, GROUP)
        self.runtime.data.setdefault("room_assessments", {})[GROUP] = {
            "signature": signature,
            "report": {"state": "ready"},
        }
        self.assertEqual(jobs.summary(self.runtime, GROUP)["state"], "ready")
        result = self.configure()
        self.assertEqual(result["confidence"], 80)
        self.assertEqual(jobs.summary(self.runtime, GROUP)["state"], "stale")
        self.assertEqual(self.runtime.data["room_presence_sources"][GROUP], result)

    def test_sensor_writes_mark_assessment_stale(self):
        self.office()
        self.configure()
        _, signature = jobs._context(self.runtime, GROUP)
        self.runtime.data.setdefault("room_assessments", {})[GROUP] = {
            "signature": signature,
            "report": {"state": "ready"},
        }
        self.entity("on")
        self.run_ticks(1000, 1004)
        self.assertEqual(jobs.summary(self.runtime, GROUP)["state"], "stale")

    def cycle(self, start):
        self.entity("on")
        self.run_ticks(start, start + 10)
        self.entity("off")
        self.run_ticks(start + 12, start + 12)

    def test_sensor_retrain_keeps_member_learning_and_open_edits_valid(self):
        self.office()
        device = self.runtime.data["devices"]["a"]
        self.runtime.label_history_range("a", 10, 20, "present")
        device["last_learning"] = {"id": "kept"}
        revision = device["label_revision"]
        self.configure()
        with patch.object(membership, "retrain", wraps=membership.retrain) as retrain:
            self.cycle(1000)
            self.cycle(2000)
        self.assertEqual(retrain.call_count, 2)
        self.assertEqual(device["label_revision"], revision)
        self.assertEqual(device["last_learning"], {"id": "kept"})
        # A device-label edit opened before the sensor transitions is still accepted.
        self.runtime.edit_history_label("a", 10, 20, 10, 30, "present", revision)
        self.assertEqual(device["label_revision"], revision + 1)

    def test_manual_room_label_still_invalidates_member_learning(self):
        self.office()
        device = self.runtime.data["devices"]["a"]
        device["last_learning"] = {"id": "old"}
        revision = device.get("label_revision", 0)
        room_labels.label_room_range(self.runtime, GROUP, 10, 20, "present")
        self.assertEqual(device["label_revision"], revision + 1)
        self.assertNotIn("last_learning", device)

    def test_sensor_revisions_reach_assessment_signatures_only(self):
        self.office()
        self.configure()
        definitions = groups.definitions(self.runtime)[2]
        self.cycle(1000)
        assessment = jobs._context(self.runtime, GROUP)[1]
        learning = jobs.set_context(self.runtime, [GROUP])[1]
        self.assertIn("room_labels", assessment)
        self.assertNotIn("room_labels", learning)
        self.assertEqual(truth.revisions(self.runtime, definitions, GROUP, sensor=False), {})
        room_labels.label_room_range(self.runtime, GROUP, 10, 20, "present")
        self.assertEqual(jobs.set_context(self.runtime, [GROUP])[1]["room_labels"], {GROUP: 1})

    def test_written_sensor_label_reaches_room_truth_level_four(self):
        self.office()
        self.configure()
        self.entity("on")
        self.run_ticks(1000, 1010)
        member = {"manual": lambda _t: None, "automatic": Timeline(())}
        context = truth.for_view(truth.view(self.runtime, GROUP), {"a": member})
        self.assertEqual(truth.room_truth(context, 1005), ("present", "automatic", 0.8))
        self.assertIsNone(truth.room_truth(context, 1011))

    def test_invalid_values_and_unknown_group_are_rejected(self):
        self.office()
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            room_sources.configure_room_sources(self.runtime, "zone:missing", values())
        for bad in (
            {"sources": []},
            values(confidence=0),
            values(sources=[{"entity_id": "Bad", "kind": "boolean", "area": ""}]),
            values(start_buffer_seconds=-1),
        ):
            with self.assertRaises(ValueError):
                room_sources.configure_room_sources(self.runtime, GROUP, bad)
        self.assertNotIn("room_presence_sources", self.runtime.data)

    def test_settings_default_and_removed_group_cleanup(self):
        self.office()
        zone = groups.configure(self.runtime, None, "Desk", "office", ["a"])["id"]
        self.assertEqual(room_sources.settings(self.runtime, zone)["sources"], [])
        room_sources.configure_room_sources(self.runtime, zone, values())
        self.entity("on")
        room_sources.sample_rooms(self.runtime, 1000)
        groups.remove(self.runtime, zone)
        self.assertNotIn(zone, self.runtime.data["room_presence_sources"])
        self.assertNotIn(f"room:{zone}", self.runtime._source_runtime)
        room_sources.sample_rooms(self.runtime, 1002)  # no error for a vanished group

    def test_sample_devices_also_samples_rooms(self):
        self.office()
        self.configure()
        self.entity("on")
        with patch("time.time", return_value=1000.0):
            self.runtime.sample_devices()
        with patch("time.time", return_value=1002.0):
            self.runtime.sample_devices()
        self.assertEqual(self.spans(), [(1000.0, 1002.0)])

    def test_blocked_storage_skips_room_sampling(self):
        self.office()
        self.configure()
        self.entity("on")
        self.runtime._storage_status["blocked"] = True
        self.run_ticks(1000, 1006)
        self.assertEqual(self.sensor(), [])

    def test_snapshot_exposes_room_sources(self):
        self.office()
        self.configure()
        group = jobs.snapshot_with_inventory(self.runtime)[0]["groups"][GROUP]
        self.assertEqual(group["presence_sources"]["confidence"], 80)
        self.assertEqual(group["presence_sources"]["state"], "unknown")
        self.entity("on")
        group = jobs.snapshot_with_inventory(self.runtime)[0]["groups"][GROUP]
        self.assertEqual(group["presence_sources"]["raw_state"], "present")

    def snapshot_revision(self):
        group = jobs.snapshot_with_inventory(self.runtime)[0]["groups"][GROUP]
        return group["label_revision"]

    def edit(self, revision):
        return room_labels.edit_room_label(
            self.runtime, GROUP, 100, 110, 100, 105, "present", revision
        )

    def test_manual_edit_succeeds_while_sensor_label_extends(self):
        self.office()
        room_labels.label_room_range(self.runtime, GROUP, 100, 110, "not_present")
        self.configure()
        self.entity("on")
        self.run_ticks(1000, 1004)
        revision = self.snapshot_revision()
        self.run_ticks(1006, 1020)
        self.assertEqual(self.snapshot_revision(), revision)
        self.assertTrue(self.edit(revision)["ok"])

    def test_genuinely_stale_manual_edit_is_still_rejected(self):
        self.office()
        room_labels.label_room_range(self.runtime, GROUP, 100, 110, "not_present")
        revision = self.snapshot_revision()
        room_labels.label_room_range(self.runtime, GROUP, 200, 210, "present")
        with self.assertRaisesRegex(ValueError, "History labels changed"):
            self.edit(revision)

    def test_signature_revision_changes_on_open_and_close_not_extension(self):
        self.office()
        self.configure()
        self.entity("on")
        definitions = groups.definitions(self.runtime)[2]
        seen = lambda: dict(truth.revisions(self.runtime, definitions, GROUP))  # noqa: E731
        base = seen()
        self.run_ticks(1000, 1002)
        opened = seen()
        self.assertNotEqual(opened, base)
        self.run_ticks(1004, 1020)
        self.assertEqual(seen(), opened)
        self.entity("off")
        self.run_ticks(1022, 1022)
        self.assertNotEqual(seen(), opened)

    def test_signature_revision_is_unchanged_without_sensor_labels(self):
        self.office()
        room_labels.label_room_range(self.runtime, GROUP, 10, 20, "present")
        definitions = groups.definitions(self.runtime)[2]
        self.assertEqual(truth.revisions(self.runtime, definitions, GROUP), {GROUP: 1})

    def stored_sensor(self, end, state="present"):
        """Simulate a restart: stored label present, no in-memory state."""
        self.runtime._room_sensor_checked.clear()
        self.runtime._room_sensor_open.clear()
        self.runtime.data["room_labels"] = {
            GROUP: [
                {
                    "start": end - 20,
                    "end": end,
                    "state": state,
                    "source": "sensor",
                    "confidence": 0.8,
                }
            ]
        }

    def test_restart_finalises_stored_label_that_is_not_continued(self):
        self.office()
        self.configure()
        self.stored_sensor(500)
        self.entity("off")
        with patch.object(membership, "retrain", wraps=membership.retrain) as retrain:
            self.run_ticks(1000, 1010)
            retrain.assert_called_once()

    def test_restart_adopts_stored_label_that_is_continued(self):
        self.office()
        self.configure()
        self.stored_sensor(1000)
        self.entity("on")
        with patch.object(membership, "retrain", wraps=membership.retrain) as retrain:
            self.run_ticks(1002, 1010)
            retrain.assert_not_called()
            self.assertEqual(self.runtime._room_sensor_open[GROUP]["state"], "present")
            self.entity("off")
            self.run_ticks(1012, 1012)
            retrain.assert_called_once()

    def flap(self, start, cycles):
        """On for two ticks, then off: each cycle opens and finalises one label."""
        for index in range(cycles):
            base = start + index * 8
            self.entity("on")
            self.run_ticks(base, base + 2)
            self.entity("off")
            self.run_ticks(base + 4, base + 4)

    def test_every_finalise_only_marks_member_references_stale(self):
        # P1: finalising no longer rebuilds references, so the per-group 600 s retrain limiter
        # is gone: each finalise marks the members stale, which is cheap.
        training = sys.modules["tuner_under_test.presence.reference_training"]
        self.office()
        self.configure()
        devices = self.runtime.data["devices"]
        with (
            patch.object(training, "rebuild_human") as rebuild,
            patch.object(membership, "retrain", wraps=membership.retrain) as retrain,
        ):
            self.flap(1000, 10)
        rebuild.assert_not_called()
        self.assertEqual(retrain.call_count, 10)
        self.assertIn("reference_stale_since", devices["a"])
        self.assertIn("reference_stale_since", devices["b"])

    def test_an_empty_outage_finalises_and_marks_members_stale(self):
        self.office()
        self.configure(mark_not_present=True)
        self.entity("off")
        self.run_ticks(1000, 1004)
        devices = self.runtime.data["devices"]
        self.assertNotIn("reference_stale_since", devices["a"])
        self.run_ticks(1100, 1104)  # Never bridge the telemetry outage.
        self.assertEqual(self.spans(), [(1000, 1004), (1100, 1104)])
        self.assertIn("reference_stale_since", devices["a"])
        self.assertIn("reference_stale_since", devices["b"])

    def test_restart_without_stored_sensor_labels_marks_nothing(self):
        self.office()
        self.configure()
        self.runtime._room_sensor_checked.clear()
        self.entity("off")
        with patch.object(membership, "retrain", wraps=membership.retrain) as retrain:
            self.run_ticks(1000, 1000)
        retrain.assert_not_called()
        self.assertNotIn(GROUP, self.runtime._room_sensor_open)

    def test_sensor_revision_bumps_at_every_finalise(self):
        self.office()
        self.configure()
        definitions = groups.definitions(self.runtime)[2]
        self.flap(1000, 1)
        first = dict(truth.revisions(self.runtime, definitions, GROUP))
        self.flap(1010, 1)
        self.assertNotEqual(truth.revisions(self.runtime, definitions, GROUP), first)

    def test_time_gap_opens_a_new_label_and_bumps_signature(self):
        self.office()
        self.configure()
        definitions = groups.definitions(self.runtime)[2]
        self.entity("on")
        self.run_ticks(1000, 1004)
        before = dict(truth.revisions(self.runtime, definitions, GROUP))
        self.run_ticks(1032, 1036)
        self.assertEqual(self.spans(), [(1000, 1004), (1032, 1036)])
        self.assertNotEqual(truth.revisions(self.runtime, definitions, GROUP), before)

    def test_sensor_volume_never_evicts_manual_labels(self):
        self.office()
        room_labels.label_room_range(self.runtime, GROUP, 10, 20, "present")
        for index in range(1005):
            room_labels.write_sensor_range(
                self.runtime, GROUP, 1000 + index * 10, 1005 + index * 10, "present", 0.8
            )
        room_labels.label_room_range(self.runtime, GROUP, 30, 40, "present")
        manual = [
            (x["start"], x["end"])
            for x in room_labels.room_labels_for(self.runtime, GROUP)
            if x["source"] == "manual"
        ]
        self.assertEqual(manual, [(10, 20), (30, 40)])
