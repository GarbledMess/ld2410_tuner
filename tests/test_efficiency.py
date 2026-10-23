"""Work on the event loop stays small on a weak machine: equivalence of the fast paths with
the plain ones they replace, and guards that event-driven work never walks all history."""

import asyncio
import random
import time
import unittest
from unittest.mock import patch

import pytest
import test_tuner as harness
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history import guesses, label_lookup

NOW = 2_000_000_000.0
DAY = 86400


class LabelEditTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    @patch.object(time, "time", return_value=NOW)
    async def test_device_label_edit_reads_only_the_blocks_it_relabels(self, _clock):
        """Regression guard: a device label edit (and a Seen by chip tap, which is one) runs
        on the event loop; after cleanup it must not walk all 30 days of history."""
        from tuner_under_test.history import dayfile, spans, summaries

        self.now = NOW
        for index in range(30):
            timestamp = self.now - 30 * DAY + 6 + index * DAY
            self.runtime._days.append("a", dayfile.Change(timestamp, {0: index % 10 + 1}))
            self.runtime._days.append(
                "a", dayfile.Guess(timestamp, "present", 20, guesses.EXTERNAL)
            )
        self.runtime._days.append("a", dayfile.End(self.now))
        self.device["history_labels"] = [
            {"start": self.now - 20 * DAY, "end": self.now - 19 * DAY, "state": "present"}
        ]
        await self.runtime.async_clean_history(persist=False)
        start = self.now - 10 * DAY
        with (
            patch.object(spans, "day_spans", side_effect=AssertionError("row replay on loop")),
            patch.object(
                summaries.Summary,
                "histograms",
                autospec=True,
                side_effect=summaries.Summary.histograms,
            ) as spy,
        ):
            result = self.runtime.label_history_range("a", start, start + 3600, "not_present")
        self.assertEqual(result["observed_seconds"], 3600)
        self.assertEqual(
            sum(self.runtime._label_counts["a"].raw["g0_move"]["not_present"]), 3600 / 6
        )
        self.assertGreater(spy.call_count, 0, "the edited native day is recounted from its summary")
        self.assertLess(spy.call_count, 40, "bounded summary queries on the event loop")


def old_rebuild(runtime, device):
    """``labels._rebuild_histograms`` counted from scratch (no kept counts) over the same
    recorded days and the day being recorded (reference)."""
    from tuner_under_test.history import label_counts
    from tuner_under_test.history.policy import retention_seconds

    device_id = next(key for key, item in runtime.data["devices"].items() if item is device)
    cutoff = max(0, time.time() - retention_seconds(runtime.data))
    raw = label_counts.count(
        label_counts._day_blocks(runtime, device_id),
        cutoff,
        label_lookup.label_index(device),
        label_counts._live_day(runtime, device_id),
    ).raw
    rebuilt = label_counts.stored({key: raw[key] for key in HISTORY_KEYS})
    return rebuilt


def assert_counts_equal(found, expected):
    """Native counts are durations in floating point, stored to 0.001 of a sample: kept and
    recounted sums may differ by float noise that rounds to neighbouring steps."""
    assert found.keys() == expected.keys()
    for key in found:
        for state in ("present", "not_present"):
            assert found[key][state] == pytest.approx(expected[key][state], abs=1e-3), (key, state)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class LabelCountTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def history(self, rng):
        """Record 4 days every ~2 minutes with fractional times and gaps, so the retention
        edge cuts through recorded days and spans; some gates do not read."""
        rows, t = [], self.clock.now - 4 * DAY
        while t < self.clock.now - 60:
            rows.append((t, bytes(rng.choice((rng.randrange(101), 255)) for _ in range(18))))
            t += rng.choice((5.6, 6.4, 11.3, 3000.7))
        harness.record_rows(self.runtime, "a", rows)

    def random_range(self, rng):
        start = self.clock.now - rng.random() * 5 * DAY
        return start, start + rng.choice((60, 3600, 6 * 3600, 2 * DAY)) * rng.random() + 1

    async def step(self, rng, forced=None):
        runtime, device = self.runtime, self.device
        action = rng.randrange(13) if forced is None else forced
        if action <= 2:
            runtime.label_history_range(
                "a", *self.random_range(rng), rng.choice(("present", "not_present", "unknown"))
            )
        elif action == 3 and device.get("history_labels"):
            label = rng.choice(device["history_labels"])
            start, end = self.random_range(rng)
            state = rng.choice(("present", "not_present", "unlabelled"))
            runtime.edit_history_label(
                "a",
                label["start"],
                label["end"],
                start,
                end,
                state,
                device.get("label_revision", 0),
            )
        elif action == 4:
            runtime.set_training_state("a", rng.choice(("present", "not_present", "unknown")))
        elif action == 5:
            for _ in range(rng.randrange(1, 90)):
                self.clock.now += rng.choice((5.2, 6.0, 6.7))
                runtime._record_history_sample("a", {"g0_move": rng.randrange(101)}, self.clock.now)
        elif action == 6:
            self.clock.now += rng.random() * 8 * 3600
        elif action == 7:
            await runtime.async_clean_history(persist=False)
        elif action == 8:
            start, end = self.clock.now - rng.random() * DAY, self.clock.now
            harness.sys.modules["tuner_under_test.history.source_labels"].confirm_presence(
                runtime, "a", start, end, 77
            )
        elif action == 9:
            days = rng.choice((3, 4, 5))
            runtime.data["storage_settings"] = {"human_days": days, "automatic_days": 3}
        elif action == 10:  # A finished day is thinned: it is rewritten without its first hour.
            self._rewrite_a_day(rng)
        elif action == 11 and (forced is not None or rng.random() < 0.15):
            runtime.clear_samples("a")
            self.history(rng)
        elif action == 12:  # A trim or retention: the oldest recorded day goes.
            days = runtime._days
            finished = days.days("a")[:-1]
            if finished:
                days.delete("a", finished[:1])

    def _rewrite_a_day(self, rng):
        from tuner_under_test.history import cleanup

        days = self.runtime._days
        finished = days.days("a")[:-1]
        if finished:
            date = rng.choice(finished)
            day = days.day("a", date)
            days.replace("a", date, cleanup.cut(day, [(day.start, day.start + 3600)]))

    async def test_kept_counts_rebuild_exactly_what_counting_everything_does(self):
        """Equivalence: every histogram rebuild from kept counts equals the old full rebuild,
        over random edits, live labels, recording, the retention edge moving through recorded
        days, cleanups, confirmations, retention changes, rewritten and deleted days and
        clears."""
        from tuner_under_test.history import labels

        checked = []
        real = labels._rebuild_histograms

        def compare(runtime, device):
            rebuilt = real(runtime, device)
            assert_counts_equal(rebuilt, old_rebuild(runtime, device))
            checked.append(bool(runtime._label_counts))
            return rebuilt

        for seed in range(3):
            rng = random.Random(4140 + seed)
            self.clock = Clock(NOW + seed * DAY)
            with (
                patch.object(time, "time", self.clock),
                patch.object(labels, "_rebuild_histograms", side_effect=compare),
            ):
                self.runtime.clear_samples("a")
                self.history(rng)
                self.device["history_labels"] = []
                self.runtime.data["storage_settings"] = {"human_days": 3, "automatic_days": 3}
                for index in range(52):  # Every kind of step first, then random ones.
                    await self.step(rng, index if index < 13 else None)
            self.runtime._label_counts.clear()
        self.assertGreater(len(checked), 40)
        self.assertGreater(sum(checked), 25, "most rebuilds used kept counts")


# --- Reference distributions: the old plain computation, kept here as the reference. ---


def old_distributions(profile, now):
    from tuner_under_test.presence import references as refs

    periods = profile.get("periods", [])
    now = refs.moment(profile, now)  # Periods are re-weighed every FRESHNESS_STEP, per radar.
    pools = {source: {} for source in refs.SOURCE_WEIGHT}
    latest, available = {}, {}
    for period in periods:
        source = period["source"]
        weight = refs.freshness(now - period["end"]) * period["confidence"]
        for key, bins in refs.decode(period["data"]).items():
            histogram = pools[source].setdefault(key, {}).setdefault(period["state"], [0.0] * 101)
            scale = min(1.0, 120 / sum(bins.values())) * weight
            for value, count in bins.items():
                histogram[int(value)] += count * scale
            identity = (source, key, period["state"])
            latest[identity] = max(latest.get(identity, 0), period["end"])
            if source != "radar":
                slot = (key, period["state"])
                available[slot] = available.get(slot, 0) + sum(bins.values())
    reference = {}
    for key in HISTORY_KEYS:
        combined = refs._combine_channel(pools, key, latest, available, now)
        if combined:
            reference[key] = combined
    return reference, refs._confidence_cap(
        [dict(period) for period in periods if period["source"]], now
    )


def old_unfamiliar(profile, values):
    from tuner_under_test.presence import references as refs

    support = {key: set() for key in values}
    for period in profile.get("periods", []):
        if period["source"] == "radar":
            continue
        for key, bins in refs.decode(period["data"]).items():
            if key in support:
                support[key].update(int(value) for value in bins)
    return [
        key
        for key, value in values.items()
        if support[key] and min(abs(value - seen) for seen in support[key]) > 6
    ]


def random_profile(rng, observations):
    """A profile built by observations that mostly extend the current period."""
    from tuner_under_test.presence import references as refs

    profile = refs.empty()
    t = NOW - 40 * DAY * rng.random()
    source, state = "human", "present"
    for _ in range(observations):
        if rng.random() < 0.12:
            source = rng.choice(("human", "entity", "radar", "radar"))
            state = rng.choice(refs.STATES)
            t += rng.choice((600, 4000))
        t += rng.choice((3, 6, 6, 9))
        values = {
            key: rng.choice((rng.randrange(101), rng.randrange(20), 255, float("nan")))
            for key in rng.sample(HISTORY_KEYS[:4], rng.randrange(1, 4))
        }
        refs.observe(profile, values, state, source, rng.choice((0.3, 0.7, 0.95, 1.0)), t)
    return profile, t


class ReferenceTests(unittest.TestCase):
    def test_distributions_and_unfamiliar_match_the_plain_computation(self):
        """Equivalence, bit for bit: random profiles grown one observation at a time, read
        after each (as every sample does), at several minutes."""
        from tuner_under_test.presence import references as refs

        rng = random.Random(4150)
        with_references = 0
        for _ in range(12):
            profile, end = random_profile(rng, rng.randrange(0, 260))
            for step in range(25):
                now = end + step * rng.choice((1, 6, 70))
                new = {"g0_move": rng.randrange(101), "g3_still": rng.randrange(101)}
                refs.observe(profile, new, rng.choice(refs.STATES), "radar", 0.8, now)
                reading = refs.distributions(profile, now)
                self.assertEqual(reading, old_distributions(profile, now))
                with_references += bool(reading[0])
                values = {key: rng.randrange(101) for key in rng.sample(HISTORY_KEYS, 6)}
                self.assertEqual(refs.unfamiliar(profile, values), old_unfamiliar(profile, values))
        self.assertGreater(with_references, 30, "many reads have references to compare")

    def settled_profile(self):
        """Human and radar periods of both states, 4000 s apart, on a minute: the human ones
        hold enough samples for the references to exist (the radar's alone never do)."""
        from tuner_under_test.presence import references as refs

        profile, start = refs.empty(), NOW - NOW % 60
        for index in range(30):
            begin = start + index * 4000
            for sample in range(25):
                values = {"g0_move": (index + sample) % 40, "g1_still": index % 7}
                refs.observe(
                    profile, values, refs.STATES[index % 2], "human", 0.95, begin + sample * 6
                )
                refs.observe(
                    profile, values, refs.STATES[index % 2], "radar", 0.9, begin + sample * 6
                )
        return profile

    def test_samples_extending_one_period_read_like_the_plain_computation(self):
        """The folded older periods are reused by every sample of the newest one, so a read
        must not change them (a pool shared with the cache would count a sample twice)."""
        from tuner_under_test.presence import references as refs

        profile = self.settled_profile()
        last = profile["periods"][-1]
        self.assertTrue(old_distributions(profile, last["end"])[0], "references exist")
        for step in range(1, 8):
            now = last["end"] + 3
            refs.observe(profile, {"g0_move": 40 + step}, last["state"], "radar", 0.9, now)
            self.assertEqual(refs.distributions(profile, now), old_distributions(profile, now))
            values = {"g0_move": 60 + step * 5, "g1_still": 3, "g2_move": 50}
            self.assertEqual(refs.unfamiliar(profile, values), old_unfamiliar(profile, values))
        self.assertEqual(len(profile["periods"]), 60)

    def test_support_of_a_shared_older_set_is_not_changed_by_a_newer_period(self):
        from tuner_under_test.presence import references as refs

        older = (refs.encode({"g0_move": {"10": 3, "20": 1}}),)
        first = older + (refs.encode({"g0_move": {"90": 2}}),)
        second = older + (refs.encode({"g0_move": {"50": 2}, "g1_still": {"5": 1}}),)
        refs._support(first)
        self.assertEqual(
            refs._support(second), {"g0_move": {10, 20, 50}, "g1_still": {5}}, "no 90 from before"
        )
        self.assertEqual(refs._support(older), {"g0_move": {10, 20}})

    def test_a_new_sample_folds_only_the_newest_period(self):
        """Regression guard: a tick must not fold every period again."""
        from tuner_under_test.presence import references as refs

        profile = self.settled_profile()
        last = profile["periods"][-1]
        refs.distributions(profile, last["end"])
        refs.observe(profile, {"g0_move": 50}, last["state"], "radar", 0.9, last["end"] + 3)
        with patch.object(refs, "_fold", wraps=refs._fold) as fold:
            reading = refs.distributions(profile, last["end"] + 3)
        self.assertEqual(fold.call_count, 2, "the newest period of each source")
        self.assertEqual(reading, old_distributions(profile, last["end"] + 3))


# --- Room truth memo. ---


class EditCostTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def test_edits_never_copy_derived_histograms_back_into_history(self):
        """A label edit uses recorded duration directly, without retaining a histogram copy."""
        self.runtime._ensure_histograms(self.device)
        start = self.now - 7200
        self.runtime.label_history_range("a", start, start + 600, "present")
        self.runtime.label_history_range("a", start + 900, start + 1500, "not_present")
        self.assertNotIn("history_legacy_histograms", self.device)


class TimingFieldTests(unittest.TestCase):
    def test_the_cached_field_matches_the_plain_classification(self):
        import re
        import types

        from tuner_under_test.calibration import timing_config

        def plain(entity):
            names = timing_config._names(entity)
            for field, suffix in (
                ("on_delay", "presence_on_delay"),
                ("off_delay", "presence_off_delay"),
            ):
                if any(name.endswith(suffix) for name in names):
                    return field
            if any(re.search(r"(?:^|[._])timeout$", name) for name in names):
                if not any("training" in name for name in names):
                    return "timeout"
            return None

        rng = random.Random(4170)
        words = ["presence_on_delay", "presence_off_delay", "timeout", "training", "x", "_", "."]
        for _ in range(2000):
            entity = types.SimpleNamespace(
                original_name=rng.choice((None, "", "Timeout", "Presence On Delay")),
                translation_key=rng.choice(
                    (None, "timeout", "presence_off_delay", "training_timeout")
                ),
                entity_id="number." + "".join(rng.choices(words, k=rng.randrange(1, 4))),
            )
            self.assertEqual(timing_config._field(entity), plain(entity))


class NoLoopRecountTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def history(self, days=3):
        """A reading every minute for ``days``, each with a confident present guess."""
        rng = random.Random(4190)
        rows = [
            (self.now - days * DAY + 600 + i * 60, rng.randbytes(18) + b"\x01d")
            for i in range(days * 1380)
        ]
        harness.record_rows(self.runtime, "a", rows)

    async def edit(self):
        websocket = harness.sys.modules["tuner_under_test.runtime.websocket"]
        start = self.now - DAY
        message = {"device_id": "a", "start": start, "end": start + 3600, "state": "present"}
        fields = ("device_id", "start", "end", "state")
        return await websocket._websocket_result(
            self.runtime, "label_history_range", fields, message
        )

    async def test_a_label_edit_without_kept_counts_counts_in_the_executor(self):
        """Regression: the first edit after a restart, trim or discarded cleanup recounted
        all history on the event loop (1.2 s at 30 days; 9 s on a Pi)."""
        import threading

        from tuner_under_test.worker import recount

        self.history()
        loop_thread, where = threading.current_thread(), []
        real = recount.calculate

        def spy(*args, **kwargs):
            where.append(threading.current_thread() is loop_thread)
            return real(*args, **kwargs)

        with patch.object(recount, "calculate", spy):
            result = await self.edit()
            await self.edit()
        # Readings a minute apart are separate observations of half a sample (3 s) each:
        # the hour holds 60 of them, 180 recorded seconds, 30 six-second units.
        self.assertEqual(result["labelled_samples"], 30)
        self.assertEqual(where, [False], "counted once, off the loop")

    async def counted_on(self, action):
        """Threads that counted all history while ``action`` ran: True for the event loop."""
        import threading

        from tuner_under_test.history import label_counts
        from tuner_under_test.worker import recount

        loop_thread, where = threading.current_thread(), []

        def spying(real):
            def spy(*args, **kwargs):
                where.append(threading.current_thread() is loop_thread)
                return real(*args, **kwargs)

            return spy

        with (
            patch.object(recount, "calculate", spying(recount.calculate)),
            patch.object(label_counts, "count", spying(label_counts.count)),
        ):
            await action()
            await asyncio.gather(*self.runtime._label_rebuilds.values())
        return where

    async def route(self, method, fields, **message):
        websocket = harness.sys.modules["tuner_under_test.runtime.websocket"]
        return await websocket._websocket_result(self.runtime, method, fields, message)

    async def test_a_label_state_change_without_kept_counts_counts_in_the_executor(self):
        """Regression: the first presence label change after a restart, trim or discarded
        cleanup recounted all history on the event loop (2.6 s for a week on a desktop)."""
        from tuner_under_test.history import labels

        self.history()
        start = self.now - DAY
        self.device["history_labels"] = [{"start": start, "end": start + 3600, "state": "present"}]
        fields = ("device_id", "state", "timeout_seconds")
        where = await self.counted_on(
            lambda: self.route("set_training_state", fields, device_id="a", state="present")
        )
        self.assertEqual(where, [False], "counted once, off the loop")
        self.assertEqual(self.device["training_state"], "present")
        rebuilt = self.device["histograms"]
        self.runtime._label_counts.clear()
        self.assertEqual(rebuilt, labels._rebuild_histograms(self.runtime, self.device))
        self.assertTrue(any(any(series["present"]) for series in rebuilt.values()))

    async def test_a_label_change_while_counting_joins_the_one_pending_rebuild(self):
        """Regression: the label revision bumped right after a room label made the pending
        rebuild skip, leaving the histograms missing until some later change."""
        from tuner_under_test.history import labels

        self.history()
        start = self.now - DAY
        self.device["history_labels"] = [{"start": start, "end": start + 3600, "state": "present"}]
        fields = ("device_id", "state", "timeout_seconds")
        await self.route("set_training_state", fields, device_id="a", state="present")
        pending = list(self.runtime._label_rebuilds.values())
        self.device["label_revision"] = self.device.get("label_revision", 0) + 1
        await self.route("set_training_state", fields, device_id="a", state="not_present")
        self.assertEqual(list(self.runtime._label_rebuilds.values()), pending, "one count")
        await asyncio.gather(*pending)
        self.assertEqual(self.runtime._label_rebuilds, {})
        rebuilt = self.device["histograms"]
        self.runtime._label_counts.clear()
        self.assertEqual(rebuilt, labels._rebuild_histograms(self.runtime, self.device))

    async def test_clearing_a_radar_cancels_its_pending_rebuild(self):
        self.history()
        fields = ("device_id", "state", "timeout_seconds")
        await self.route("set_training_state", fields, device_id="a", state="present")
        (pending,) = self.runtime._label_rebuilds.values()
        await self.route("clear_samples", ("device_id",), device_id="a")
        await asyncio.gather(pending, return_exceptions=True)
        self.assertTrue(pending.cancelled())
        self.assertEqual(self.device["histograms"], {})
        self.assertNotIn("a", self.runtime._label_counts)

    async def test_pausing_recording_without_kept_counts_counts_in_the_executor(self):
        self.history()
        where = await self.counted_on(
            lambda: self.route(
                "set_recording", ("device_id", "enabled"), device_id="a", enabled=False
            )
        )
        self.assertEqual(where, [False], "counted once, off the loop")
        self.assertFalse(self.device["recording_enabled"])

    async def test_a_label_timeout_without_kept_counts_counts_in_the_executor(self):
        self.history()
        self.device.update(
            training_state="present",
            training_label_start=time.time() - 60,
            training_expires_at=time.time() - 1,
        )
        where = await self.counted_on(lambda: self.runtime._timeout_worker("a", 0))
        self.assertEqual(where, [False], "counted once, off the loop")
        self.assertEqual(self.device["training_state"], "unknown")

    async def test_clearing_a_radar_counts_none_of_the_history_it_removes(self):
        self.history()
        where = await self.counted_on(
            lambda: self.route("clear_samples", ("device_id",), device_id="a")
        )
        self.assertEqual(where, [], "nothing counted for history that is being removed")
        self.assertEqual(self.device["histograms"], {})
        self.assertEqual(self.device["training_state"], "unknown")

    @patch.object(time, "time", return_value=NOW)
    async def test_cleanup_discarded_by_new_samples_still_keeps_the_counts(self, clock):
        """Regression: a cleanup result dropped because a sample arrived meanwhile also
        dropped the counts it had made."""
        from tuner_under_test.history import dayfile, spans

        self.now = NOW
        store = self.runtime._days
        store.append("a", dayfile.Change(NOW - 10 * DAY, {0: 1}))
        store.append("a", dayfile.End(NOW - 10 * DAY + 6))
        store.append("a", dayfile.Change(NOW - 3 * DAY, {0: 5}))
        store.append("a", dayfile.Guess(NOW - 3 * DAY, "present", 20, guesses.EXTERNAL))
        self.device["history_labels"] = [
            {"start": NOW - 3 * DAY, "end": NOW + 60, "state": "present"}
        ]
        preparations, injected = 0, False

        async def executor(request):
            nonlocal preparations, injected
            if request["operation"] == "maintenance":
                preparations += 1
                if not injected:
                    self.runtime._record_history_sample("a", {"g0_move": 9}, self.now)
                    clock.return_value = NOW + 6
                    injected = True

        self.worker_transport.after = executor
        await self.runtime.async_clean_history(persist=False)
        self.assertTrue(injected, "arrival races the actual staged native cleanup")
        self.assertEqual(preparations, 2, "changed history discards the first cleanup plan")
        self.assertIn("a", self.runtime._label_counts)
        self.assertEqual(list(spans.events(self.runtime, "a"))[-1].values[0], 9)
        self.assertEqual(self.runtime._label_counts["a"].raw["g0_move"]["present"][9], 1)
