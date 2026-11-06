"""The overnight pass skips what cannot change, resumes after restarts and orders by use."""

import asyncio
import json
import sys
import types
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import test_tuner as harness
from tuner_under_test.calibration import jobs, recovery, settings, timing_config
from tuner_under_test.history import label_counts, policy, storage
from tuner_under_test.history.daystore import DayStore
from tuner_under_test.worker import bridge, codec, flushing, retention

mod = sys.modules["tuner_under_test.runtime.coordinator"]
schedule = sys.modules["tuner_under_test.runtime.schedule"]


class Overnight(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    configuration = harness.RuntimeTests.configuration
    report_write = harness.RuntimeTests.report_write
    sample = harness.RuntimeTests.sample

    def arrange(self, *others, at="03:00"):
        self.configuration()
        self.learned = deepcopy(self.device["last_learning"])
        self.learned.update(label_revision=0, created_at=self.now)
        for key in others:
            self.radar(key)
        self.fits, self.statuses = [], {}
        self.worker_transport.respond = self.fit
        self.hass.config = types.SimpleNamespace(
            time_zone="Europe/London", path=self.hass.config.path
        )
        self.runtime.configure_learning_schedule(True, at, False)

    def radar(self, key):
        self.runtime.data["devices"][key] = {
            "entities": {f"sensor.{key}_g0_move_energy": {"gate": 0, "kind": "move"}},
            "training_state": "unknown",
        }
        names = [f"{key}_g{gate}_{kind}_threshold" for gate in (0, 1, 2) for kind in KINDS]
        names += [f"{key}_max_{kind}_distance_gate" for kind in KINDS]
        for name in names:
            entity_id = f"number.{name}"
            self.registry.entities[entity_id] = types.SimpleNamespace(
                device_id=key, domain="number", entity_id=entity_id
            )
            self.states[entity_id] = types.SimpleNamespace(state="2" if "max" in name else "10")

    async def fit(self, request):
        key = codec.decode(request["parameters"])["device_id"]
        self.fits.append(key)
        return {**deepcopy(self.learned), "status": self.statuses.get(key, "ok")}

    async def tick(self, instant):
        self.runtime.nightly_tick(datetime.fromisoformat(instant))
        task = self.runtime._nightly_task
        if task and not task.done():
            await task

    async def night(self, day):
        """The scheduled tick at 03:00 local, British Summer Time."""
        await self.tick(f"{day}T02:00:00+00:00")

    def status(self, key="a"):
        return self.runtime.data["devices"][key]["nightly_learning"]["status"]

    def saved(self):
        return self.runtime.store.async_save.call_args.args[0]

    def block(self, key):
        """Hold ``key``'s fit open; returns the event set once it is in the worker."""
        started = asyncio.Event()

        async def held(request):
            if codec.decode(request["parameters"])["device_id"] != key:
                return await self.fit(request)
            started.set()
            await asyncio.Event().wait()

        self.worker_transport.respond = held
        return started

    async def restart(self, *, crash=False):
        """A new runtime from what was stored; ``crash`` loses anything not yet saved."""
        old = self.runtime
        if old._nightly_task:
            old._nightly_task.cancel()
            await asyncio.gather(old._nightly_task, return_exceptions=True)
        running = list(old._learning_jobs.values())
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        if not crash:
            await storage.save_directly(old)
        data = json.loads(json.dumps(self.saved()))
        await bridge.stop(old)
        self.runtime = mod.TunerRuntime(self.hass, old.store, data)
        self.runtime._schedule_save = lambda: None
        self.runtime._schedule_flush = lambda: None
        self.runtime._days = old._days
        self.device = data["devices"]["a"]
        self.worker_transport.respond = self.fit
        schedule.restore(self.runtime)


KINDS = ("move", "still")


class SkipTests(Overnight):
    async def test_second_night_records_unchanged_and_keeps_the_saved_result(self):
        self.arrange()
        await self.night("2026-09-26")
        result = self.device["learning_results"]["automatic"]["id"]
        await self.night("2026-09-27")
        assert self.fits == ["a"]
        run = self.device["nightly_learning"]
        assert run["status"] == "unchanged"
        assert run["day"] == "2026-09-27"
        assert run["result_id"] == result
        assert run["learned_day"] == "2026-09-26"
        assert "automatic_apply" not in run
        assert self.device["learning_results"]["automatic"]["id"] == result
        self.hass.services.async_call.assert_not_awaited()

    async def test_with_nothing_new_it_stays_skipped_however_long_ago_it_learned(self):
        self.arrange()
        await self.night("2026-09-26")
        for day in ("10-03", "10-20", "11-30"):
            await self.night(f"2026-{day}")
        assert self.fits == ["a"]
        assert self.device["nightly_learning"]["learned_day"] == "2026-09-26"

    async def test_new_readings_on_their_own_force_a_learn(self):
        self.arrange()
        await self.night("2026-09-26")
        self.sample(self.now)
        await self.night("2026-09-27")
        assert self.fits == ["a", "a"]
        assert self.status() == "ok"
        await self.night("2026-09-28")
        assert self.fits == ["a", "a"]

    async def test_an_observation_with_unchanged_readings_is_new_evidence(self):
        self.arrange()
        self.sample(self.now)
        await self.night("2026-09-26")
        self.fits.clear()
        self.sample(self.now + 60)  # A quiet room: same values, later time.
        await self.night("2026-09-27")
        assert self.fits == ["a"]

    async def test_everything_a_human_provides_forces_a_learn(self):
        self.arrange()

        async def history_label():
            await self.runtime.label_history_range("a", self.now - 60, self.now - 30, "present")

        def threshold():
            self.states["number.radar_g0_move_threshold"].state = "11"

        def timing():
            values = {**timing_config.DEFAULTS, "mode": "fallback", "timeout": 7}
            timing_config.configure(self.runtime, values)

        def sources():
            self.device["presence_sources"] = {"entities": ["binary_sensor.door"]}

        def retained():
            self.runtime.data["storage_settings"] = {**policy.DEFAULTS, "human_days": 45}

        changes = {
            "history label": history_label,
            "live training label": lambda: self.runtime.set_training_state("a", "present", None),
            "threshold": threshold,
            "timing": timing,
            "presence sources": sources,
            "automatic weight": lambda: settings.configure(self.runtime, 0.5),
            "retention": retained,
        }
        day = datetime(2026, 9, 26)
        await self.night(day.date().isoformat())
        for name, change in changes.items():
            with self.subTest(name):
                self.fits.clear()
                result = change()
                if asyncio.iscoroutine(result):
                    await result
                day += timedelta(days=1)
                await self.night(day.date().isoformat())
                assert self.fits == ["a"]
                self.fits.clear()
                # Nothing else changed, one day later: the night is quiet again.
                day += timedelta(days=1)
                await self.night(day.date().isoformat())
                assert self.fits == []
                assert self.status() == "unchanged"

    async def test_turning_automatic_apply_on_forces_a_learn_that_can_apply(self):
        self.arrange()
        with patch.object(schedule.automatic, "run", AsyncMock()) as run:
            await self.night("2026-09-26")
            run.reset_mock()
            self.device["auto_apply_override"] = "overnight"
            await self.night("2026-09-27")
        assert self.fits == ["a", "a"]
        run.assert_awaited_once()

    async def test_clearing_recordings_forces_a_learn(self):
        self.arrange()
        await self.night("2026-09-26")
        self.runtime.clear_samples("a")
        await self.night("2026-09-27")
        assert self.fits == ["a", "a"]

    async def test_retention_rewriting_history_forces_a_learn_but_is_not_a_human_change(self):
        self.arrange()
        await self.night("2026-09-26")
        baseline = deepcopy(self.device[schedule.overnight.BASELINE])
        prepared = storage.budget.copy_data(self.runtime.data)
        changes = [("a", "2026-09-01", None)]
        batch = {"progress": {}, "radar_done": False, "done": False, "selected": [changes[0][:2]]}
        retention._metadata(prepared, "a", label_counts.empty(), batch, self.now, changes)
        storage._accept_days(self.runtime, prepared, changes, None)
        assert self.device["label_revision"] == 1
        await self.night("2026-09-27")
        assert self.fits == ["a", "a"]
        assert self.device[schedule.overnight.BASELINE]["human"] == baseline["human"]

    async def test_a_baseline_survives_a_storage_round_trip_and_a_reopened_store(self):
        self.arrange()
        self.device["presence_sources"] = {"entities": ["binary_sensor.door"], "confidence": 90}
        self.sample(self.now)
        await self.night("2026-09-26")
        await flushing.flush(self.runtime)
        root = self.runtime._days.root
        await self.restart()
        self.runtime._days = DayStore(root)
        await asyncio.to_thread(self.runtime._days.open, self.now)
        await self.night("2026-09-27")
        assert self.fits == ["a"]
        assert self.status() == "unchanged"

    async def test_anything_short_of_a_usable_result_always_runs(self):
        def missing_result(device):
            device["learning_results"].pop("automatic")

        def replaced_result(device):
            device["learning_results"]["automatic"]["id"] = "other"

        def status(value):
            return lambda device: device["nightly_learning"].update(status=value)

        def application(value):
            return lambda device: device["nightly_learning"].update(
                automatic_apply={"status": value}
            )

        cases = {
            "error": status("error"),
            "interrupted": status("interrupted"),
            "running": status("running"),
            "insufficient": status("insufficient"),
            "no record": lambda device: device.pop("nightly_learning"),
            "no result": missing_result,
            "replaced result": replaced_result,
            "apply failed": application("error"),
            "apply partial": application("partial"),
            "apply interrupted": application("interrupted"),
        }
        self.arrange()
        await self.night("2026-09-26")
        for name, change in cases.items():
            with self.subTest(name):
                self.fits.clear()
                change(self.device)
                self.runtime.data["learning_schedule"].pop("last_day")
                await self.night("2026-09-27")
                assert self.fits == ["a"]

    async def test_a_failed_learn_is_retried_rather_than_hidden_by_a_skip(self):
        self.arrange()
        await self.night("2026-09-26")
        self.worker_transport.respond = AsyncMock(side_effect=ValueError("worker exploded"))
        self.device["presence_sources"] = {"entities": ["binary_sensor.door"]}
        await self.night("2026-09-27")
        assert self.status() == "error"
        # The inputs now match nothing stored as learned, and the error is not overwritten.
        self.worker_transport.respond = self.fit
        await self.night("2026-09-28")
        assert self.fits == ["a", "a"]
        assert self.status() == "ok"

    async def test_unreadable_settings_run_and_report_rather_than_skip(self):
        self.arrange()
        await self.night("2026-09-26")
        del self.registry.entities["number.radar_g0_move_threshold"]
        await self.night("2026-09-27")
        assert self.status() == "error"
        assert "Enable all active" in self.device["nightly_learning"]["error"]

    async def test_a_setting_changed_during_the_fit_runs_again_next_night(self):
        self.arrange()

        async def changed(request):
            self.states["number.radar_g1_move_threshold"].state = "11"
            return await self.fit(request)

        self.worker_transport.respond = changed
        await self.night("2026-09-26")
        self.worker_transport.respond = self.fit
        self.fits.clear()
        await self.night("2026-09-27")
        assert self.fits == ["a"]


class ResumeTests(Overnight):
    async def begin(self, key="b", day="2026-09-26"):
        started = self.block(key)
        self.runtime.nightly_tick(datetime.fromisoformat(f"{day}T02:00:00+00:00"))
        await started.wait()

    async def resume(self, day="2026-09-26"):
        first = datetime.fromisoformat(f"{day}T09:00:00+00:00")
        await self.tick(first.isoformat())
        await self.tick((first + timedelta(minutes=5)).isoformat())

    async def test_remaining_items_are_durable_and_finish_after_a_crash(self):
        self.arrange("b", "c")
        await self.begin()
        assert self.fits == ["a"]
        queue = self.saved()["learning_schedule"]["queue"]
        assert queue["day"] == "2026-09-26"
        assert queue["items"] == ["b", "c"]
        await self.restart(crash=True)
        await self.resume()
        assert self.fits == ["a", "b", "c"]
        for key in "abc":
            assert self.status(key) == "ok"
        assert "queue" not in self.runtime.data["learning_schedule"]
        assert "queue" not in self.saved()["learning_schedule"]

    async def test_restored_item_reads_interrupted_until_it_resumes(self):
        self.arrange("b", "c")
        await self.begin()
        await self.restart()
        assert self.status("b") == "interrupted"
        assert self.runtime.data["devices"]["b"]["learning_job"]["status"] == "interrupted"
        assert schedule.settings(self.runtime)["running"] is False
        await self.resume()
        assert self.status("b") == "ok"
        assert "error" not in self.runtime.data["devices"]["b"]["nightly_learning"]

    async def test_resume_waits_for_the_radars_to_return_after_startup(self):
        self.arrange("b")
        await self.begin()
        await self.restart()
        self.fits.clear()
        await self.tick("2026-09-26T09:00:00+00:00")
        assert self.fits == []
        await self.tick("2026-09-26T09:04:59+00:00")
        assert self.fits == []
        await self.tick("2026-09-26T09:05:00+00:00")
        assert self.fits == ["b"]

    async def test_a_finished_night_does_not_run_again_after_restart(self):
        self.arrange("b")
        await self.night("2026-09-26")
        await self.restart()
        await self.resume()
        await self.night("2026-09-26")
        assert self.fits == ["a", "b"]

    async def test_a_passed_day_drops_the_queue_and_the_next_night_is_normal(self):
        self.arrange("b", "c")
        await self.begin()
        await self.restart()
        self.fits.clear()
        await self.tick("2026-09-27T00:01:00+01:00")
        assert self.fits == []
        assert "queue" not in self.runtime.data["learning_schedule"]
        await self.night("2026-09-27")
        assert self.fits == ["b", "c"]

    async def test_a_disabled_schedule_drops_the_queue_for_good(self):
        self.arrange("b", "c")
        await self.begin()
        await self.restart()
        self.fits.clear()
        self.runtime.configure_learning_schedule(False, "03:00")
        await self.resume()
        assert "queue" not in self.runtime.data["learning_schedule"]
        self.runtime.configure_learning_schedule(True, "03:00")
        await self.resume()
        assert self.fits == []

    async def test_disabling_mid_run_also_drops_the_rest(self):
        self.arrange("b", "c")

        async def disabling(request):
            self.runtime.configure_learning_schedule(False, "03:00")
            return await self.fit(request)

        self.worker_transport.respond = disabling
        await self.night("2026-09-26")
        assert self.fits == ["a"]
        assert "queue" not in self.runtime.data["learning_schedule"]

    async def test_repeated_dst_hour_resumes_once_and_never_starts_twice(self):
        self.arrange("b", "c", at="01:30")
        started = self.block("b")
        self.runtime.nightly_tick(datetime.fromisoformat("2026-10-25T00:30:00+00:00"))
        await started.wait()
        await self.restart()
        # The clock reads 01:30 again one hour later: that continues rather than restarts.
        await self.tick("2026-10-25T01:30:00+00:00")
        await self.tick("2026-10-25T01:35:00+00:00")
        assert self.fits == ["a", "b", "c"]
        await self.restart()
        await self.tick("2026-10-25T01:44:00+00:00")
        assert self.fits == ["a", "b", "c"]
        assert self.runtime.data["learning_schedule"]["last_day"] == "2026-10-25"

    async def test_an_item_that_keeps_dying_is_retried_only_once(self):
        self.arrange("b", "c")
        await self.begin()
        await self.restart(crash=True)
        started = self.block("b")
        first = datetime.fromisoformat("2026-09-26T09:00:00+00:00")
        self.runtime.nightly_tick(first)
        self.runtime.nightly_tick(first + timedelta(minutes=5))
        await started.wait()
        await self.restart(crash=True)
        await self.resume()
        assert self.fits == ["a", "c"]
        assert "queue" not in self.runtime.data["learning_schedule"]

    async def test_a_removed_radar_leaves_the_queue(self):
        self.arrange("b", "c")
        await self.begin()
        await self.restart()
        self.fits.clear()
        del self.runtime.data["devices"]["c"]
        await self.resume()
        assert self.fits == ["b"]
        assert "queue" not in self.runtime.data["learning_schedule"]

    async def test_a_failing_pass_does_not_restart_every_minute(self):
        self.arrange()
        self.runtime.async_save = AsyncMock(side_effect=ValueError("storage limit"))
        self.runtime.nightly_tick(datetime.fromisoformat("2026-09-26T02:00:00+00:00"))
        failed = self.runtime._nightly_task
        await asyncio.gather(failed, return_exceptions=True)
        assert "queue" not in self.runtime.data["learning_schedule"]
        self.runtime.nightly_tick(datetime.fromisoformat("2026-09-26T02:01:00+00:00"))
        assert self.runtime._nightly_task is failed
        assert self.fits == []

    async def test_a_failed_checkpoint_does_not_lose_the_rest_of_the_night(self):
        self.arrange("b")
        with patch.object(storage, "save_directly", AsyncMock(side_effect=OSError("disk"))):
            await self.night("2026-09-26")
        assert self.fits == ["a", "b"]

    async def test_a_restart_during_the_reference_refresh_keeps_the_night(self):
        self.arrange("b")
        entered = asyncio.Event()

        async def stuck(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(schedule.rebuilds, "refresh", stuck):
            self.runtime.nightly_tick(datetime.fromisoformat("2026-09-26T02:00:00+00:00"))
            await entered.wait()
            await self.restart(crash=True)
        await self.resume()
        assert self.fits == ["a", "b"]


class OrderTests(Overnight):
    def evidence(self, *keys):
        for key in keys:
            self.runtime._record_history_sample(key, {"g0_move": 10}, self.now)

    async def test_human_changes_first_then_the_oldest_last_learn(self):
        self.arrange()
        await self.night("2026-09-26")
        for key, day in (("b", "2026-09-27"), ("c", "2026-09-28"), ("d", "2026-09-29")):
            self.radar(key)
            await self.night(day)
        assert self.fits == ["a", "b", "c", "d"]
        self.fits.clear()
        # All have new readings; only d also has a human change; e has never learned.
        self.evidence("a", "b", "c", "d")
        self.states["number.d_g0_move_threshold"].state = "11"
        self.radar("e")
        await self.night("2026-09-30")
        assert self.fits == ["d", "e", "a", "b", "c"]

    async def test_equal_last_learns_use_a_stable_order(self):
        self.arrange("c", "b")
        await self.night("2026-09-26")
        assert self.fits == ["a", "b", "c"]
        self.fits.clear()
        self.evidence("c", "b", "a")
        await self.night("2026-09-27")
        assert self.fits == ["a", "b", "c"]


class YieldTests(Overnight):
    def gated(self, key, *, error=None):
        """Hold a user Learn of ``key`` in preparation until the returned event is set."""
        release = asyncio.Event()
        real = recovery.prepare_learning

        async def prepare(runtime, device_id):
            if device_id == key and not release.is_set():
                await release.wait()
                if error:
                    raise error
            return await real(runtime, device_id)

        return release, patch.object(recovery, "prepare_learning", prepare)

    def start_user_learn_during_first_fit(self, key):
        async def first(request):
            if not self.fits:
                self.user = jobs.start(self.runtime, key)
            return await self.fit(request)

        self.worker_transport.respond = first

    async def a_is_settled(self):
        """Let the night pass the first item; a next fit, if admitted, would start here."""
        while self.device.get("nightly_learning", {}).get("status") != "ok":
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)

    async def test_a_user_learn_goes_before_the_next_overnight_item(self):
        self.arrange("b", "c")
        release, gate = self.gated("c")
        self.start_user_learn_during_first_fit("c")
        with gate:
            self.runtime.nightly_tick(datetime.fromisoformat("2026-09-26T02:00:00+00:00"))
            await self.a_is_settled()
            assert self.fits == ["a"]
            release.set()
            await self.runtime._nightly_task
        assert self.fits[:2] == ["a", "c"]
        assert "b" in self.fits
        assert (await self.user)["user"]["status"] == "ok"

    async def test_a_failed_user_learn_does_not_stop_the_night(self):
        self.arrange("b", "c")
        release, gate = self.gated("c", error=ValueError("radar offline"))
        self.start_user_learn_during_first_fit("c")
        with gate:
            self.runtime.nightly_tick(datetime.fromisoformat("2026-09-26T02:00:00+00:00"))
            await self.a_is_settled()
            release.set()
            await self.runtime._nightly_task
        await asyncio.gather(self.user, return_exceptions=True)
        assert self.fits == ["a", "b", "c"]
        assert self.status("c") == "ok"
