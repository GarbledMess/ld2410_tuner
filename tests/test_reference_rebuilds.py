"""Room-evidence reference rebuilds run off the Home Assistant event loop."""

import asyncio
import sys
import time
import types
import unittest
from collections import Counter
from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import test_references as references_harness
import test_tuner as harness
from tuner_under_test.rooms import attribution, event_attribution, membership

training = sys.modules["tuner_under_test.presence.reference_training"]
refs = sys.modules["tuner_under_test.presence.references"]
QUIET = references_harness.QUIET
GROUP = "area:office"


def manual(start, end, state):
    return {"start": start, "end": end, "state": state, "source": "manual"}


class LoopRebuildTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    office = references_harness.RoomReferenceTests.office
    record = references_harness.RoomReferenceTests.record
    label = references_harness.RoomReferenceTests.label
    human = references_harness.RoomReferenceTests.human
    inferred = references_harness.RoomReferenceTests.inferred

    def scenario(self):
        """a quiet, b quiet then high; the room is labelled Empty over the quiet stretch.
        Labels are written directly, so nothing has been rebuilt yet."""
        self.office()
        self.record(5, 5, count=QUIET)
        self.record(5, 70, start=QUIET)
        self.at = lambda index: self.base + index * 6
        self.runtime.data["room_labels"] = {
            GROUP: [manual(self.at(0), self.at(QUIET), "not_present")]
        }
        self.runtime.data["room_label_revision"] = {GROUP: 1}

    def present(self):
        """Add the room Present label directly (no rebuild request)."""
        self.runtime.data["room_labels"][GROUP].append(
            manual(self.at(QUIET), self.at(QUIET + 40), "present")
        )

    def hold(self):
        """Rebuild jobs wait for ``release``; each records its function and b's profile.
        Other executor jobs (the shutdown save) run at once."""
        self.calls = []
        self.release = asyncio.Event()

        async def executor(request):
            if request["operation"] != "references":
                return
            self.calls.append((request["operation"], deepcopy(self.b.get("reference_profile"))))
            await self.release.wait()

        self.worker_transport.before = executor

    async def started(self, count=1):
        # Preparation runs in executor threads, so a fixed number of loop turns
        # is not enough on a slow host; wait for the observable condition.
        deadline = asyncio.get_running_loop().time() + 30
        while len(self.calls) < count:
            assert asyncio.get_running_loop().time() < deadline, "rebuild never started"
            await asyncio.sleep(0.001)

    async def request(self, device_ids):
        """Queue a refresh of these radars without waiting for it."""
        from tuner_under_test.presence import rebuilds

        return rebuilds.queue(self.runtime, device_ids, reason="test")

    async def finish(self):
        """Wait until every queued rebuild is installed."""
        while (task := self.runtime._reference_task) is not None and not task.done():
            await asyncio.wait({task})

    async def test_a_rebuild_keeps_no_attribution_state_after_its_job(self):
        """Regression: guesses and truth readers stayed alive after a rebuild. Native
        attribution is cached per job view only, so nothing outlives the job."""
        self.scenario()
        self.label(QUIET, QUIET + 40, "present")
        with patch.object(event_attribution, "_found", wraps=event_attribution._found) as found:
            await self.request(None)
            await self.finish()
        assert found.call_count
        assert not getattr(self.runtime, "_native_found_job", None)

    async def test_room_label_edit_leaves_attribution_to_the_executor(self):
        self.scenario()
        self.b["last_learning"] = {"id": "old"}
        revision = self.b.get("label_revision", 0)
        self.hold()
        with patch.object(
            attribution, "radar_attribution", wraps=attribution.radar_attribution
        ) as spy:
            self.label(QUIET, QUIET + 40, "present")
            # Synchronous state changes happen at once; the profile computation does not.
            assert self.b["label_revision"] == revision + 1
            assert "last_learning" not in self.b
            assert await self.request(None) == ["a", "b"]  # The edit marked both stale.
            assert spy.call_count == 0, "room attribution ran on the event loop"
            assert self.inferred(self.b) == set()
            await self.started()
            assert [operation for operation, _ in self.calls] == ["references"]
            self.release.set()
            await self.finish()
            assert spy.called
        assert self.human(self.b) == {"not_present"}
        assert self.inferred(self.b) == {"present"}

    async def test_device_label_edit_on_a_grouped_radar_leaves_attribution_to_the_executor(self):
        self.scenario()
        self.present()
        revision = self.b.get("label_revision", 0)
        self.hold()
        with patch.object(
            attribution, "radar_attribution", wraps=attribution.radar_attribution
        ) as spy:
            await self.runtime.label_history_range("b", self.at(0), self.at(10), "not_present")
            assert self.b["label_revision"] == revision + 1
            # Deliberate change: a room peer's references read b's labels, so a is stale too.
            assert await self.request(None) == ["a", "b"]
            assert spy.call_count == 0, "room attribution ran on the event loop"
            await self.started()
            self.release.set()
            await self.finish()
            assert spy.called
        assert self.inferred(self.b) == {"present"}

    async def test_many_requests_coalesce_into_at_most_two_rebuilds_per_radar(self):
        self.scenario()
        self.hold()
        with patch.object(training, "rebuild_human", wraps=training.rebuild_human) as rebuild:
            for index in range(5):
                self.label(QUIET + index, QUIET + 40, "present")
                await self.request(None)
            await self.started()
            for index in range(5):  # More requests while the first rebuild computes.
                self.label(QUIET, QUIET + 40 - index, "present")
                await self.request(None)
            self.release.set()
            await self.finish()
        rebuilt = Counter(
            training._device_id(view, device) for (view, device), _ in rebuild.call_args_list
        )
        assert set(rebuilt) == {"a", "b"}
        assert max(rebuilt.values()) <= 2, rebuilt
        assert len(self.calls) <= 2
        assert self.inferred(self.b) == {"present"}

    async def test_a_result_computed_before_a_peer_label_change_is_discarded(self):
        """Regression: b's rebuild reads a's labels for room truth, but only b's own label
        revision guarded the install, so a's edit mid-rebuild let the stale result in."""
        self.scenario()
        self.present()
        self.hold()
        await self.request({"b"})
        await self.started()
        before = deepcopy(self.b.get("reference_profile"))
        await self.runtime.label_history_range("a", self.at(QUIET), self.at(QUIET + 40), "present")
        self.release.set()
        await self.finish()
        assert len(self.calls) == 2, "the stale result must be recomputed"
        assert self.calls[1][1] == before, "the stale result must not be installed"

    async def test_a_result_computed_before_a_label_change_is_discarded(self):
        self.scenario()
        self.hold()
        await self.request({"b"})
        await self.started()
        before = deepcopy(self.b.get("reference_profile"))
        # A room label change lands while the rebuild computes, without a rebuild request.
        self.present()
        self.runtime.data["room_label_revision"][GROUP] += 1
        self.release.set()
        await self.finish()
        assert len(self.calls) == 2, "the stale result must be recomputed"
        assert self.calls[1][1] == before, "the stale result must not be installed"
        assert self.inferred(self.b) == {"present"}

    async def test_installed_profile_matches_an_inline_rebuild(self):
        self.scenario()
        self.present()
        profile = training.profile(self.b)
        refs.observe(profile, {"g0_move": 9}, "not_present", "entity", 0.9, self.at(1))
        # Starts with the room period rebuilt for the attributed episode, yet is not it.
        refs.observe(profile, {"g0_move": 60}, "present", "radar", 0.9, self.at(QUIET))
        # A full set of old human Empty periods, past retention: the rebuild's new ones push
        # the oldest out, so both paths must keep only the newest MAX_PERIODS.
        old = self.base - 60 * refs.DAY
        for index in range(refs.MAX_PERIODS):
            refs.observe(profile, {"g0_move": 4}, "not_present", "human", 1.0, old + index * 60)
        original = deepcopy(profile)
        training.rebuild_human(self.runtime, self.b)
        expected = deepcopy(self.b["reference_profile"])
        human = [
            p for p in expected["periods"] if (p["source"], p["state"]) == ("human", "not_present")
        ]
        assert len(human) == refs.MAX_PERIODS and human[0]["start"] > old
        radar = [
            (p["start"], bool(p.get("room"))) for p in expected["periods"] if p["source"] == "radar"
        ]
        assert (self.at(QUIET), False) in radar and (self.at(QUIET), True) in radar
        self.b["reference_profile"] = original
        await self.request({"b"})
        await self.finish()
        assert self.b["reference_profile"] == expected

    async def test_a_human_period_extended_across_the_retention_edge_is_not_duplicated(self):
        from tuner_under_test.history.policy import retention_seconds

        self.scenario()
        self.present()
        # The clock puts the retention cutoff 3 s after the first sample: an old human Empty
        # period ending there is continued by the rebuild, so it becomes the rebuilt copy.
        now = self.base + 3 + retention_seconds(self.runtime.data)
        profile = training.profile(self.b)
        for index in range(5):
            refs.observe(
                profile, {"g0_move": 4}, "not_present", "human", 1.0, self.base - 24 + index * 6
            )
        original = deepcopy(profile)
        with patch.object(time, "time", return_value=now):
            training.rebuild_human(self.runtime, self.b)
            expected = deepcopy(self.b["reference_profile"])
            self.b["reference_profile"] = original
            await self.request({"b"})
            await self.finish()
        starts = [p["start"] for p in expected["periods"] if p["source"] == "human"]
        assert starts.count(self.base - 24) == 1
        assert self.b["reference_profile"] == expected

    async def test_live_human_observations_newer_than_the_rebuild_are_kept(self):
        self.scenario()
        self.present()
        self.hold()
        await self.request({"b"})
        await self.started()
        later = time.time() + 60  # Recorded after the rebuild copied the history.
        refs.observe(training.profile(self.b), {"g0_move": 70}, "present", "human", 1.0, later)
        self.release.set()
        await self.finish()
        human = [p for p in self.b["reference_profile"]["periods"] if p["source"] == "human"]
        assert human[-1]["start"] == later
        assert self.human(self.b) == {"not_present", "present"}
        assert self.inferred(self.b) == {"present"}

    async def test_live_entity_observations_during_a_rebuild_are_kept(self):
        self.scenario()
        self.present()
        self.hold()
        await self.request({"b"})
        await self.started()
        later = self.at(QUIET + 60)
        refs.observe(training.profile(self.b), {"g0_move": 9}, "not_present", "entity", 0.9, later)
        self.release.set()
        await self.finish()
        entity = [p for p in self.b["reference_profile"]["periods"] if p["source"] == "entity"]
        assert [(p["start"], p.get("room")) for p in entity] == [(later, None)]
        assert self.inferred(self.b) == {"present"}

    async def test_entity_periods_reset_during_a_rebuild_stay_removed(self):
        self.scenario()
        self.present()
        refs.observe(
            training.profile(self.b), {"g0_move": 9}, "not_present", "entity", 0.9, self.at(1)
        )
        self.hold()
        await self.request({"b"})
        await self.started()
        training.reset_entities(self.b)  # Presence sources reconfigured mid-rebuild.
        self.release.set()
        await self.finish()
        periods = self.b["reference_profile"]["periods"]
        assert not [p for p in periods if p["source"] == "entity" and not p.get("room")]
        assert self.inferred(self.b) == {"present"}

    async def test_radars_removed_or_ungrouped_while_queued_are_handled(self):
        from tuner_under_test.rooms import groups

        self.scenario()
        self.present()
        self.runtime.data["devices"]["c"] = deepcopy(self.device)
        groups.configure(self.runtime, "area:office", "Office", "office", ["a", "b", "c"])
        await self.finish()
        self.hold()
        await self.request({"b"})
        await self.started()
        await self.request({"a", "c"})  # Queued behind b's rebuild.
        # While b computes: c is deleted and a leaves the only multi-radar group.
        del self.runtime.data["devices"]["c"]
        groups.configure(self.runtime, "area:office", "Office", "office", ["a"])
        before = deepcopy(self.b["reference_profile"])
        del self.runtime.data["devices"]["b"]
        self.release.set()
        await self.finish()
        assert len(self.calls) == 2
        assert self.runtime._reference_pending == set()
        assert set(self.runtime.data["devices"]) == {"a"}
        assert self.b["reference_profile"] == before  # Removed: nothing was installed.
        periods = self.runtime.data["devices"]["a"]["reference_profile"]["periods"]
        assert not [p for p in periods if p.get("room")]  # a rebuilt without room evidence.

    async def test_attribution_is_computed_once_per_radar_per_job(self):
        self.scenario()
        self.present()
        recording = sys.modules["tuner_under_test.history.recording"]
        view = recording._history_view(self.runtime, "b")  # A real job view (office() stubs it).
        attribution.prepare(self.runtime, view, "b")
        saved = view.data["room_snapshot"]
        with patch.object(event_attribution, "_found", wraps=event_attribution._found) as found:
            for _ in range(3):  # Reference rebuild, Seen by and roles all ask in one job.
                attribution.radar_attribution(view, "b", saved)
                attribution.radar_attribution(view, "b", saved, human=False)
        assert found.call_count == 2  # Human and full truth, once each.

    async def shutdown(self):
        mod = sys.modules["tuner_under_test"]
        with (
            patch.object(mod.engineering, "stop", AsyncMock()),
            patch.object(mod.schedule, "stop", AsyncMock()),
        ):
            # A hang guard only: unload must return without the held rebuild finishing.
            await asyncio.wait_for(mod._shutdown(self.runtime), 30)

    async def test_one_pass_copies_each_members_labels_once(self):
        """A pass prepares every queued radar's view on the event loop; each view's room
        snapshot holds all its members' labels. Copied once per pass, not once per radar."""
        self.scenario()
        self.present()
        labels = self.runtime.data["devices"]["b"]["history_labels"] = [
            {"start": self.at(0), "end": self.at(QUIET), "state": "not_present"}
        ]
        copied = []
        real = attribution.deepcopy

        def copy(value, *args):
            copied.append(value is labels)
            return real(value, *args)

        with patch.object(attribution, "deepcopy", side_effect=copy):
            await self.request({"a", "b"})
            await self.finish()
        assert copied.count(True) == 1

    async def test_snapshots_share_copies_only_within_one_pass(self):
        self.scenario()
        labels = self.runtime.data["devices"]["b"]["history_labels"] = [
            {"start": self.at(0), "end": self.at(QUIET), "state": "present"}
        ]
        with attribution.shared_copies(self.runtime):
            first = attribution.snapshot(self.runtime, "a")["members"]["b"]["history_labels"]
            second = attribution.snapshot(self.runtime, "b")["members"]["b"]["history_labels"]
        assert first is second
        assert first == labels and first is not labels
        later = attribution.snapshot(self.runtime, "a")["members"]["b"]["history_labels"]
        assert later == labels and later is not first

    async def test_unload_drops_the_queued_backlog_without_running_it(self):
        # Deliberate change: unload used to wait for every queued rebuild (it could take
        # minutes at 30 days); it now cancels the pass in flight and drops the queue.
        self.scenario()
        self.present()
        self.hold()
        await self.request({"b"})
        await self.started()
        await self.request({"a"})  # Queued behind b's held rebuild.
        await self.shutdown()
        assert len(self.calls) == 1, "the queued rebuild must not run"
        assert self.runtime._reference_pending == set()
        assert self.runtime._reference_task.done()

    async def test_no_result_is_installed_or_queued_after_unload(self):
        from tuner_under_test.presence import rebuilds

        self.scenario()
        self.present()
        self.hold()
        await self.request({"b"})
        await self.started()
        before = deepcopy(self.b.get("reference_profile"))
        rebuilds.mark_stale(self.runtime, {"b"}, 100.0)
        job = rebuilds._prepare(self.runtime, "b")
        await self.shutdown()
        self.release.set()  # The executor job may still finish: its result is dropped.
        for _ in range(20):
            await asyncio.sleep(0)
        rebuilds._install(self.runtime, job, rebuilds.compute([job])[0])
        assert self.b.get("reference_profile") == before
        assert self.b[STALE] == 100.0, "a dropped result leaves the radar stale"
        revision = self.b.get("label_revision", 0)
        membership.retrain(self.runtime, {"b"})  # An edit after unload only marks stale.
        assert self.b["label_revision"] == revision + 1
        assert STALE in self.b
        assert await self.request({"b"}) == [], "a refresh after unload queues nothing"
        assert self.runtime._reference_pending == set()
        assert len(self.calls) == 1
        assert self.inferred(self.b) == set()

    async def test_the_worker_is_a_background_task(self):
        background = []

        def create(target, name, **_kwargs):
            background.append(name)
            return asyncio.create_task(target)

        self.hass.async_create_background_task = create
        tracked = patch.object(self.hass, "async_create_task", side_effect=AssertionError)
        self.scenario()
        self.present()
        with tracked:
            await self.request({"b"})
            await self.finish()
        assert len(background) == 1
        assert self.inferred(self.b) == {"present"}


STALE = "reference_stale_since"


class StaleReferenceTests(unittest.IsolatedAsyncioTestCase):
    """Events only mark references stale; rebuilds run overnight or on demand (P1)."""

    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    office = references_harness.RoomReferenceTests.office
    record = references_harness.RoomReferenceTests.record
    label = references_harness.RoomReferenceTests.label
    human = references_harness.RoomReferenceTests.human
    inferred = references_harness.RoomReferenceTests.inferred
    scenario = LoopRebuildTests.scenario
    present = LoopRebuildTests.present
    hold = LoopRebuildTests.hold
    started = LoopRebuildTests.started
    shutdown = LoopRebuildTests.shutdown

    async def refresh(self, device_ids=None):
        from tuner_under_test.presence import rebuilds

        return await rebuilds.refresh(self.runtime, device_ids, reason="test")

    async def idle(self):
        for _ in range(20):
            await asyncio.sleep(0)

    async def test_room_label_edit_marks_members_stale_without_rebuilding(self):
        self.scenario()
        self.b["last_learning"] = {"id": "old"}
        revision = self.b.get("label_revision", 0)
        with patch.object(training, "rebuild_human", wraps=training.rebuild_human) as rebuild:
            with patch.object(time, "time", return_value=self.base + 5000):
                self.label(QUIET, QUIET + 40, "present")
            await self.idle()
            rebuild.assert_not_called()
        # The synchronous invalidation is unchanged; only the profile waits.
        assert self.b["label_revision"] == revision + 1
        assert "last_learning" not in self.b
        assert self.b[STALE] == self.base + 5000
        assert self.runtime.data["devices"]["a"][STALE] == self.base + 5000
        assert self.inferred(self.b) == set()
        assert await self.refresh() == ["a", "b"]
        assert self.inferred(self.b) == {"present"}
        assert STALE not in self.b and STALE not in self.runtime.data["devices"]["a"]

    async def test_device_label_edit_on_an_ungrouped_radar_no_longer_rebuilds_at_once(self):
        # Deliberate change (P1): an ungrouped radar's device label edit used to rebuild its
        # references inline on the event loop. It now marks them stale; the correction that
        # drops overlapping automatic summaries stays immediate.
        when = self.now - 60
        row = bytes([30] * len(harness.constants.HISTORY_KEYS))
        harness.record_rows(self.runtime, "a", [(when + i * 6, row) for i in range(5)])
        profile = training.profile(self.device)
        refs.observe(profile, {"g0_move": 9}, "not_present", "entity", 0.9, when - 9000)
        refs.observe(profile, {"g0_move": 9}, "not_present", "entity", 0.9, when)
        with patch.object(training, "rebuild_human", wraps=training.rebuild_human) as rebuild:
            await self.runtime.label_history_range("a", when - 1, when + 30, "present")
            await self.idle()
            rebuild.assert_not_called()
        periods = self.device["reference_profile"]["periods"]
        assert [(p["source"], p["start"]) for p in periods] == [("entity", when - 9000)]
        assert self.device[STALE] >= when
        await self.refresh(["a"])
        assert self.human(self.device) == {"present"}
        assert STALE not in self.device

    async def test_device_label_edit_marks_the_radars_room_peers_stale(self):
        # a's labels decide the room truth b's references are rebuilt from.
        self.scenario()
        await self.runtime.label_history_range("a", self.at(QUIET), self.at(QUIET + 40), "present")
        assert STALE in self.runtime.data["devices"]["a"]
        assert STALE in self.b

    async def test_membership_and_recording_changes_mark_members_stale(self):
        from tuner_under_test.rooms import groups

        self.scenario()
        self.runtime.set_recording("b", False)  # b leaves the area group.
        assert STALE in self.b and STALE in self.runtime.data["devices"]["a"]
        del self.b[STALE], self.runtime.data["devices"]["a"][STALE]
        before = groups.definitions(self.runtime)[2]
        groups.configure(self.runtime, "area:office", "Office", "office", ["a", "b"])
        membership.membership_changed(self.runtime, before)
        assert STALE in self.b and STALE in self.runtime.data["devices"]["a"]
        assert self.inferred(self.b) == set()

    def test_the_earliest_stale_time_is_kept(self):
        from tuner_under_test.presence import rebuilds

        rebuilds.mark_stale(self.runtime, {"a", "missing"}, 100.0)
        rebuilds.mark_stale(self.runtime, {"a"}, 200.0)
        assert self.device[STALE] == 100.0
        assert "missing" not in self.runtime.data["devices"]

    async def test_refresh_without_radars_rebuilds_only_stale_ones(self):
        from tuner_under_test.presence import rebuilds

        self.scenario()
        self.present()
        rebuilds.mark_stale(self.runtime, {"b"}, 100.0)
        assert await self.refresh() == ["b"]
        assert await self.refresh() == []
        requests = self.worker_transport.requests
        assert [[job["device_id"] for job in item["parameters"]["jobs"]] for item in requests] == [
            ["b"]
        ]
        assert self.inferred(self.b) == {"present"}
        assert await self.refresh(["a", "missing"]) == ["a"]  # Explicit radars rebuild anyway.

    async def test_an_event_during_a_refresh_leaves_the_radar_stale(self):
        from tuner_under_test.presence import rebuilds

        self.scenario()
        self.present()
        rebuilds.mark_stale(self.runtime, {"b"}, 100.0)
        self.hold()
        task = asyncio.create_task(self.refresh(["b"]))
        await self.started()
        # A sensor-driven change: no revision bump, so the result still installs.
        rebuilds.mark_stale(self.runtime, {"b"}, 300.0)
        self.release.set()
        await task
        assert self.inferred(self.b) == {"present"}
        assert self.b[STALE] == 300.0

    async def test_a_discarded_result_is_recomputed_and_settles_both_marks(self):
        from tuner_under_test.presence import rebuilds

        self.scenario()
        self.present()
        rebuilds.mark_stale(self.runtime, {"b"}, 100.0)
        self.hold()
        task = asyncio.create_task(self.refresh(["b"]))
        await self.started()
        self.runtime.data["room_label_revision"][GROUP] += 1  # Discards the first result.
        rebuilds.mark_stale(self.runtime, {"b"}, 300.0)
        self.release.set()
        await task
        assert len(self.calls) == 2
        assert self.inferred(self.b) == {"present"}
        # The recomputed pass copied the inputs after both marks, so it settles them.
        assert STALE not in self.b

    async def test_unload_after_a_discarded_result_keeps_the_radar_stale(self):
        from tuner_under_test.presence import rebuilds

        self.scenario()
        self.present()
        rebuilds.mark_stale(self.runtime, {"b"}, 100.0)
        self.hold()
        task = asyncio.create_task(self.refresh(["b"]))
        await self.started()
        self.runtime.data["room_label_revision"][GROUP] += 1  # Discards the first result.
        self.release.set()
        self.release = asyncio.Event()  # Hold the recomputation.
        await self.started(2)
        await self.shutdown()
        await task
        assert self.inferred(self.b) == set()
        assert self.b[STALE] == 100.0

    async def test_a_failure_while_copying_the_history_keeps_the_radars_stale(self):
        from tuner_under_test.presence import rebuilds

        self.scenario()
        rebuilds.mark_stale(self.runtime, {"a", "b"}, 100.0)
        original = attribution.prepare

        def prepare(runtime, view, device_id, shared=None):
            if device_id == "b":  # a was already taken for this pass.
                raise RuntimeError("boom")
            return original(runtime, view, device_id)

        with (
            patch.object(attribution, "prepare", side_effect=prepare),
            self.assertLogs(rebuilds._LOGGER, "ERROR"),
        ):
            assert await asyncio.wait_for(self.refresh(), 5) == ["a", "b"]
        assert self.runtime.data["devices"]["a"][STALE] == 100.0
        assert self.b[STALE] == 100.0
        assert self.runtime._reference_task.done()
        assert await self.refresh(["a"]) == ["a"], "the worker still runs later passes"
        assert STALE not in self.runtime.data["devices"]["a"]

    async def test_the_snapshot_says_while_a_radar_is_refreshing(self):
        from tuner_under_test.presence import rebuilds

        self.scenario()
        self.present()
        for key in ("a", "b"):  # The snapshot lists radars the entity registry still has.
            self.registry.entities[f"sensor.{key}_g0_move_energy"] = types.SimpleNamespace(
                entity_id=f"sensor.{key}_g0_move_energy", device_id=key, domain="sensor"
            )
        assert not rebuilds.busy(self.runtime, "b")
        self.hold()
        rebuilds.queue(self.runtime, ["b"], reason="test")
        assert rebuilds.busy(self.runtime, "b"), "queued"
        await self.started()
        assert rebuilds.busy(self.runtime, "b"), "computing"
        assert not rebuilds.busy(self.runtime, "a")
        assert self.runtime.snapshot()["devices"]["b"]["references_refreshing"] is True
        self.release.set()
        await self.runtime._reference_task
        assert not rebuilds.busy(self.runtime, "b")
        assert self.runtime.snapshot()["devices"]["b"]["references_refreshing"] is False

    async def test_a_failed_refresh_keeps_the_radars_stale(self):
        from tuner_under_test.presence import rebuilds

        self.scenario()
        rebuilds.mark_stale(self.runtime, {"b"}, 100.0)

        async def failure(request):
            raise RuntimeError("boom")

        self.worker_transport.before = failure
        with self.assertLogs(rebuilds._LOGGER, "ERROR"):
            assert await self.refresh(["a", "b"]) == ["a", "b"]
        assert self.b[STALE] == 100.0
        assert STALE not in self.runtime.data["devices"]["a"]

    async def test_unload_during_a_refresh_keeps_the_radars_stale(self):
        from tuner_under_test.presence import rebuilds

        self.scenario()
        self.present()
        rebuilds.mark_stale(self.runtime, {"b"}, 100.0)
        rebuilds.mark_stale(self.runtime, {"a"}, 150.0)
        self.hold()
        task = asyncio.create_task(self.refresh(["b"]))
        await self.started()
        queued = asyncio.create_task(self.refresh(["a"]))  # Waits behind b.
        await self.idle()
        await self.shutdown()
        await asyncio.gather(task, queued)
        assert len(self.calls) == 1
        assert not rebuilds.busy(self.runtime, "a") and not rebuilds.busy(self.runtime, "b")
        assert self.b[STALE] == 100.0
        assert self.runtime.data["devices"]["a"][STALE] == 150.0
        assert self.inferred(self.b) == set()
        # A hang guard only: after unload a refresh must return without queueing.
        assert await asyncio.wait_for(self.refresh(["b"]), 5) == [], "nothing is queued"

    async def test_refresh_references_command_queues_without_waiting(self):
        from tuner_under_test.presence import rebuilds

        self.scenario()
        self.present()
        rebuilds.mark_stale(self.runtime, {"b"}, 100.0)
        self.hold()
        # A guard only: the command must return while the rebuild is held.
        queued = await asyncio.wait_for(self.runtime.refresh_references(None), 5)
        assert queued == {"queued": ["b"]}
        await self.started()
        assert self.inferred(self.b) == set(), "the command returned before the rebuild"
        self.release.set()
        await self.runtime._reference_task
        assert self.inferred(self.b) == {"present"}
        assert await self.runtime.refresh_references("a") == {"queued": ["a"]}
        await self.runtime._reference_task
        with self.assertRaisesRegex(ValueError, "Unknown device"):
            await self.runtime.refresh_references("missing")

    def test_the_snapshot_says_since_when_references_are_out_of_date(self):
        from tuner_under_test.presence import rebuilds

        assert self.runtime.auto_learning_summary(self.device)["stale_since"] is None
        rebuilds.mark_stale(self.runtime, {"a"}, 100.0)
        assert self.runtime.auto_learning_summary(self.device)["stale_since"] == 100.0
        self.runtime.clear_samples("a")  # Nothing is left to rebuild.
        assert STALE not in self.device

    async def test_overnight_pass_refreshes_stale_references_before_learning(self):
        from tuner_under_test.presence import rebuilds
        from tuner_under_test.runtime import schedule

        self.scenario()
        self.present()
        rebuilds.mark_stale(self.runtime, {"a", "b"}, 100.0)
        self.runtime.data["learning_schedule"] = {"enabled": True}
        seen = []

        async def learn(runtime, device_id, device, day):
            seen.append((device_id, STALE in device, self.inferred(self.b)))
            device["nightly_learning"] = {"status": "ok"}

        with (
            patch.object(schedule, "_learn_device", side_effect=learn),
            patch.object(schedule, "_plan", return_value=[("b", self.b, [])]),
        ):
            await schedule._run(self.runtime, "2026-10-10")
        assert seen == [("b", False, {"present"})]
        assert STALE not in self.runtime.data["devices"]["a"]


class DailyReferenceRefreshTests(unittest.IsolatedAsyncioTestCase):
    """References refresh once a local day at the schedule time, even with learning off."""

    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    office = references_harness.RoomReferenceTests.office
    record = references_harness.RoomReferenceTests.record
    label = references_harness.RoomReferenceTests.label
    human = references_harness.RoomReferenceTests.human
    inferred = references_harness.RoomReferenceTests.inferred
    scenario = LoopRebuildTests.scenario
    present = LoopRebuildTests.present

    def schedule(self, enabled=False, at="03:00"):
        from tuner_under_test.presence import rebuilds

        self.scenario()
        self.present()
        self.hass.config = types.SimpleNamespace(time_zone="Europe/London")
        self.runtime.data["learning_schedule"] = {"enabled": enabled, "time": at}
        self.saves = Mock()
        self.runtime._schedule_save = self.saves
        self.rebuilt = []

        async def spy(request):
            if request["operation"] == "references":
                self.rebuilt.extend(job["device_id"] for job in request["parameters"]["jobs"])

        self.worker_transport.before = spy
        return rebuilds

    async def tick(self, instant):
        self.runtime.nightly_tick(datetime.fromisoformat(instant))
        for task in (self.runtime._nightly_task, self.runtime._reference_task):
            if task is not None:
                await task

    async def test_disabled_learning_still_refreshes_references_once_a_day(self):
        rebuilds = self.schedule()
        rebuilds.mark_stale(self.runtime, {"b"}, 100.0)
        await self.tick("2026-09-26T01:59:00+00:00")  # 02:59 BST.
        assert self.rebuilt == [] and STALE in self.b
        await self.tick("2026-09-26T02:00:00+00:00")
        assert self.rebuilt == ["b"]
        assert STALE not in self.b and self.inferred(self.b) == {"present"}
        saved = self.runtime.data["learning_schedule"]
        assert saved["references_day"] == "2026-09-26"
        assert "last_day" not in saved, "learning's claim is separate"
        self.saves.assert_called()
        assert self.runtime._nightly_task is None, "no learning pass runs"
        rebuilds.mark_stale(self.runtime, {"b"}, 200.0)
        await self.tick("2026-09-26T09:00:00+00:00")
        self.runtime._reference_task = None  # Same persisted data after a restart.
        await self.tick("2026-09-26T22:59:00+00:00")  # 23:59 BST, still the same day.
        assert self.rebuilt == ["b"]
        await self.tick("2026-09-27T02:00:00+00:00")
        assert self.rebuilt == ["b", "b"]
        assert saved["references_day"] == "2026-09-27"

    async def test_the_daily_claim_is_saved_even_with_nothing_stale(self):
        self.schedule()
        await self.tick("2026-09-26T02:00:00+00:00")
        assert self.rebuilt == []
        assert self.runtime.data["learning_schedule"]["references_day"] == "2026-09-26"
        self.saves.assert_called_once()

    async def test_daily_refresh_runs_once_per_local_day_across_dst(self):
        rebuilds = self.schedule(at="01:30")
        rebuilds.mark_stale(self.runtime, {"b"}, 100.0)
        await self.tick("2026-10-25T00:30:00+00:00")  # 01:30 BST.
        assert self.rebuilt == ["b"]
        rebuilds.mark_stale(self.runtime, {"b"}, 200.0)
        await self.tick("2026-10-25T01:30:00+00:00")  # 01:30 GMT: the repeated hour.
        assert self.rebuilt == ["b"]
        await self.tick("2027-03-28T00:59:00+00:00")  # 00:59 GMT, before the jump.
        assert self.rebuilt == ["b"]
        await self.tick("2027-03-28T01:00:00+00:00")  # 02:00 BST: 01:30 was skipped.
        assert self.rebuilt == ["b", "b"]

    async def learning(self):
        from tuner_under_test.runtime import schedule

        learned = []

        async def learn(runtime, device_id, device, day):
            learned.append((device_id, day))
            device["nightly_learning"] = {"status": "ok"}

        for name, value in (
            ("_learn_device", learn),
            ("_plan", lambda runtime, devices: [("b", self.b, [])]),
        ):
            patcher = patch.object(schedule, name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return learned

    async def test_enabled_schedule_rebuilds_each_radar_at_most_once_that_day(self):
        rebuilds = self.schedule(enabled=True)
        learned = await self.learning()
        rebuilds.mark_stale(self.runtime, {"a", "b"}, 100.0)
        for instant in ("02:00", "02:01", "09:00", "22:00"):
            await self.tick(f"2026-09-26T{instant}:00+00:00")
        assert sorted(self.rebuilt) == ["a", "b"]
        assert learned == [("b", "2026-09-26")]
        assert "references_day" not in self.runtime.data["learning_schedule"]

    async def test_enabling_learning_within_dispatch_window_after_reference_refresh(self):
        rebuilds = self.schedule()
        learned = await self.learning()
        rebuilds.mark_stale(self.runtime, {"b"}, 100.0)
        await self.tick("2026-09-26T02:00:00+00:00")
        assert self.rebuilt == ["b"] and learned == []
        self.runtime.data["learning_schedule"]["enabled"] = True
        await self.tick("2026-09-26T02:01:00+00:00")
        assert learned == [("b", "2026-09-26")]
        assert self.rebuilt == ["b"], "references were current: nothing rebuilt twice"


class ChronologicalCapTests(unittest.TestCase):
    """Each source and state keeps its newest MAX_PERIODS by time, not by list position."""

    def period(self, start, room=False):
        period = {"source": "entity", "state": "present", "start": start, "end": start + 5}
        return {**period, "room": True} if room else period

    def test_installing_room_evidence_keeps_newer_direct_evidence(self):
        current = {"periods": [self.period(1000)]}
        rebuilt = {"periods": [self.period(index * 10) for index in range(refs.MAX_PERIODS)]}
        training._install_room_evidence(current, 0, [rebuilt])
        starts = [p["start"] for p in current["periods"]]
        assert len(starts) == refs.MAX_PERIODS
        assert starts == sorted(starts) and starts[-1] == 1000 and 0 not in starts

    def test_splicing_a_rebuild_keeps_newer_direct_evidence(self):
        device = {"reference_profile": {**refs.empty(), "periods": [self.period(1000)]}}
        rebuilt = [self.period(index * 10, room=True) for index in range(refs.MAX_PERIODS)]
        training.splice(device, rebuilt, 0, 2000)
        starts = [p["start"] for p in device["reference_profile"]["periods"]]
        assert len(starts) == refs.MAX_PERIODS
        assert starts == sorted(starts) and starts[-1] == 1000 and 0 not in starts


class SensorFloorReferenceTests(unittest.IsolatedAsyncioTestCase):
    """Room sensor labels below MIN_AUTO_CONFIDENCE never train references (review M1)."""

    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    office = references_harness.RoomReferenceTests.office
    record = references_harness.RoomReferenceTests.record
    scenario = references_harness.AutomaticRoomTruthTests.scenario
    record_guessed = references_harness.AutomaticRoomTruthTests.record_guessed

    def entity(self, confidence):
        self.runtime.data["room_labels"] = {
            GROUP: [
                {
                    "start": self.base,
                    "end": self.base + 240,
                    "state": "not_present",
                    "source": "sensor",
                    "confidence": confidence,
                }
            ]
        }
        training.rebuild_human(self.runtime, self.b)
        periods = self.b["reference_profile"]["periods"]
        return [p for p in periods if p.get("room") and p["source"] == "entity"]

    def test_sub_floor_sensor_labels_never_train_entity_references(self):
        self.scenario()
        floor = attribution.MIN_AUTO_CONFIDENCE
        assert self.entity(round(floor - 0.01, 2)) == []
        assert [round(p["confidence"], 2) for p in self.entity(floor)] == [floor]


if __name__ == "__main__":
    unittest.main()
