"""Work on the event loop stays small on a weak machine: equivalence of the fast paths with
the plain ones they replace, and guards that event-driven work never walks all history."""

import random
import struct
import time
import unittest
import zlib
from array import array
from copy import deepcopy
from unittest.mock import patch

import test_tuner as harness
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history import cleanup, codec, guesses, policy, recording

NOW = 2_000_000_000.0
DAY = 86400
# Byte -> gate value 0-100, or 255 (a missing gate) for one byte in 256.
VALUES = (bytes(range(101)) * 3)[:255] + bytes((255,))


def old_block_samples(block, cutoff):
    """``recording._block_samples`` before the block index (reference)."""
    start = float(block["start"])
    if float(block.get("end", start + 65535)) < cutoff:
        return
    count, version = int(block["count"]), block.get("version", 1)
    raw = codec.decode_payload(block["data"], version, count)
    if version not in codec.STRIDES:
        return
    stride = codec.STRIDES[version]
    if len(raw) < count * stride:
        return
    for index in range(count):
        pos = index * stride
        timestamp = start + struct.unpack(">H", raw[pos : pos + 2])[0]
        if timestamp >= cutoff:
            yield timestamp, raw[pos + 2 : pos + 2 + len(HISTORY_KEYS)]


def old_samples(runtime, device_id, since=0):
    """``_iter_history_samples`` before the block index: every block is visited (reference)."""
    device = runtime.data["devices"][device_id]
    cutoff = max(since, time.time() - policy.retention_seconds(runtime.data))
    for block in device.get("history", []):
        try:
            yield from old_block_samples(block, cutoff)
        except (KeyError, TypeError, ValueError, zlib.error, struct.error):
            continue
    for timestamp, row in recording.pending_samples(runtime, device_id):
        if timestamp >= cutoff:
            yield timestamp, row


def random_blocks(rng, start, count):
    samples = []
    t = start
    for _ in range(count):
        t += rng.choice((6, 6, 6, 7, 3600))
        samples.append((t + rng.random(), rng.randbytes(18)))
    return cleanup._encode(samples)


def odd_blocks(rng, blocks):
    """Unsorted, corrupt, NaN-ended, end-less and inconsistent blocks among good ones."""
    good = blocks[rng.randrange(len(blocks))] if blocks else {"start": NOW, "count": 0}
    odd = [
        {"start": "x"},
        {"start": NOW - DAY},
        "junk",
        None,
        {**good, "end": float("nan")},
        {key: value for key, value in good.items() if key != "end"},
        {**good, "end": good["start"] - 10 * DAY},
        {**good, "version": 9},
    ]
    return rng.sample(odd, rng.randrange(len(odd)))


class BlockIndexTests(unittest.TestCase):
    def runtime(self, history, pending=()):
        runtime = harness.mod.TunerRuntime(None, None, {"devices": {"a": {"history": history}}})
        runtime._history_runtime["a"] = {"samples": list(pending)}
        return runtime

    def test_indexed_reads_match_reading_every_block(self):
        """Equivalence: random histories (sorted or not, with damaged blocks), random
        cutoffs, before and after blocks are appended or replaced in place."""
        rng = random.Random(4120)
        with patch.object(time, "time", return_value=NOW):
            for _ in range(80):
                blocks = random_blocks(rng, NOW - 31 * DAY + rng.random() * DAY, rng.randrange(400))
                history = blocks + odd_blocks(rng, blocks)
                if rng.random() < 0.5:
                    rng.shuffle(history)
                pending = [(NOW - 5 + i, rng.randbytes(18)) for i in range(rng.randrange(3))]
                runtime = self.runtime(history, pending)
                device = runtime.data["devices"]["a"]
                for _ in range(4):
                    since = rng.choice((0, NOW - 3600, NOW - rng.random() * 40 * DAY, NOW + 1))
                    self.assertEqual(
                        list(runtime._iter_history_samples(device, since)),
                        list(old_samples(runtime, "a", since)),
                    )
                    history.extend(random_blocks(rng, NOW - rng.random() * 2 * DAY, 70))
                    if history and isinstance(history[0], dict) and "data" in history[0]:
                        history[0] = dict(history[0])  # Replaced in place, same times.

    def test_history_summary_matches_the_snapshot_expressions(self):
        rng = random.Random(4121)
        for _ in range(40):
            history = random_blocks(rng, NOW - 20 * DAY, rng.randrange(300))
            rng.shuffle(history)
            runtime = self.runtime(history)
            for _ in range(3):
                expected = (
                    len(history),
                    sum(int(b.get("count", 0)) for b in history),
                    min((float(b.get("start", 0)) for b in history), default=None),
                    max((float(b.get("start", 0)) for b in history), default=None),
                )
                self.assertEqual(recording.history_summary(runtime, "a"), expected)
                history.extend(random_blocks(rng, NOW - rng.random() * 30 * DAY, 90))
            runtime.data["devices"]["a"]["history"] = history[:5]
            self.assertEqual(
                recording.history_summary(runtime, "a")[:2],
                (5, sum(b["count"] for b in history[:5])),
            )

    def test_a_recent_read_visits_only_recent_blocks(self):
        """Regression guard: the room estimate's one-hour read (every poll) must not visit
        every stored block of 30 days of history."""
        rng = random.Random(4122)
        samples = [(NOW - 30 * DAY + i * 60, rng.randbytes(18)) for i in range(30 * 24 * 60)]
        history = cleanup._encode(samples)
        runtime = self.runtime(history)
        device = runtime.data["devices"]["a"]
        with (
            patch.object(time, "time", return_value=NOW),
            patch.object(recording, "_block_samples", wraps=recording._block_samples) as spy,
        ):
            rows = list(runtime._iter_history_samples(device, since=NOW - 3600))
            self.assertTrue(rows)
            self.assertLess(spy.call_count, 50)


def confident(rows):
    """``(history blocks, guess run chunks)`` of ``rows`` carrying a confident recorded guess
    (kept by cleanup's thinning)."""
    samples, runs = harness.split_guesses([(ts, row + b"\x01d") for ts, row in rows])
    return cleanup._encode(samples), guesses.encode(runs)


def thirty_days(rng, now, step=60):
    """History blocks and guess runs every ``step`` seconds over 30 days."""
    return confident(
        [
            (now - 30 * DAY + 600 + i * step, bytes(rng.randrange(101) for _ in range(18)))
            for i in range(int((30 * DAY - 1200) // step))
        ]
    )


class LabelEditTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    async def test_device_label_edit_reads_only_the_blocks_it_relabels(self):
        """Regression guard: a device label edit (and a Seen by chip tap, which is one) runs
        on the event loop; after cleanup it must not walk all 30 days of history."""
        rng = random.Random(4130)
        self.device["history"], self.device["guess_runs"] = thirty_days(rng, self.now)
        self.device["history_labels"] = [
            {"start": self.now - 20 * DAY, "end": self.now - 19 * DAY, "state": "present"}
        ]
        await self.runtime.async_clean_history(persist=False)
        start = self.now - 10 * DAY
        with patch.object(recording, "_block_samples", wraps=recording._block_samples) as spy:
            result = self.runtime.label_history_range("a", start, start + 3600, "not_present")
        self.assertEqual(result["labelled_samples"], 60)
        self.assertLess(spy.call_count, 40, "blocks read on the event loop")


def old_rebuild(runtime, device):
    """``labels._rebuild_histograms`` before kept counts: every retained sample (reference)."""
    from tuner_under_test.const import HISTOGRAM_BINS
    from tuner_under_test.history import labels

    rebuilt = {
        key: {"present": [0] * HISTOGRAM_BINS, "not_present": [0] * HISTOGRAM_BINS}
        for key in HISTORY_KEYS
    }
    label_at = labels._history_label_reader(device)
    device_id = next(key for key, item in runtime.data["devices"].items() if item is device)
    samples = list(old_samples(runtime, device_id))
    for timestamp, row in samples:
        label = label_at(timestamp)
        if label in {"present", "not_present"}:
            for index, key in enumerate(HISTORY_KEYS):
                if row[index] <= 100:
                    rebuilt[key][label][row[index]] += 1
    labels._restore_legacy_histograms(device, rebuilt)
    for series in rebuilt.values():
        runtime._compress_histogram(series["present"])
        runtime._compress_histogram(series["not_present"])
    return rebuilt


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class LabelCountTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def history(self, rng):
        """4 days every ~2 minutes with fractional times, so rounded block offsets put some
        samples just past their block's end and the retention edge cuts through blocks."""
        rows, t = [], self.clock.now - 4 * DAY
        while t < self.clock.now - 60:
            rows.append((t, rng.randbytes(18).translate(VALUES)))
            t += rng.choice((119.6, 120.4, 121.3, 3000.7))
        blocks = cleanup._encode(rows)
        return blocks

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
        elif action == 10 and device.get("history"):
            block = dict(rng.choice(device["history"]))
            block.update(rng.choice(({"end": float("nan")}, {"end": block["start"] - 50}, {})))
            device["history"] = [*device["history"], block]
        elif action == 11 and (forced is not None or rng.random() < 0.15):
            runtime.clear_samples("a")
            device["history"] = self.history(rng)
        elif action == 12:  # A trim or prune: the oldest blocks go.
            device["history"] = device["history"][len(device["history"]) // 4 :]

    async def test_kept_counts_rebuild_exactly_what_counting_everything_does(self):
        """Equivalence: every histogram rebuild from kept counts equals the old full rebuild,
        over random edits, live labels, recording, the retention edge moving through blocks,
        cleanups, confirmations, retention changes, odd blocks and clears."""
        from tuner_under_test.history import labels

        checked = []
        real = labels._rebuild_histograms

        def compare(runtime, device):
            rebuilt = real(runtime, device)
            self.assertEqual(rebuilt, old_rebuild(runtime, device))
            checked.append(bool(runtime._label_counts))
            return rebuilt

        for seed in range(3):
            rng = random.Random(4140 + seed)
            self.clock = Clock(NOW + seed * DAY)
            with (
                patch.object(time, "time", self.clock),
                patch.object(labels, "_rebuild_histograms", side_effect=compare),
            ):
                self.device["history"] = self.history(rng)
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
    now = int(now // 60) * 60
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


class RoomTruthTests(unittest.TestCase):
    def test_memo_answers_like_the_reader_and_reads_each_sample_once(self):
        from tuner_under_test.rooms import attribution

        rng = random.Random(4161)
        times = sorted({NOW + rng.randrange(5000) * 6 + rng.choice((0, 0.5)) for _ in range(2000)})
        answers = [None, ("present", "human", 1.0), ("not_present", "automatic", 0.7)]
        truth = {t: rng.choice(answers) for t in times}
        reads = []

        def expected(t):
            return truth.get(t, ("present", "estimate", 0.9))

        def read(t):
            reads.append(t)
            return expected(t)

        memo = attribution.Memo(read, array("d", times))
        elsewhere = {NOW - 5, NOW + 1e6, times[3] + 0.25}
        queries = [rng.choice(times) for _ in range(3000)] + list(elsewhere)
        for _ in range(2):
            for t in queries + times + times[::-1]:
                self.assertEqual(memo(t), expected(t))
        sample_reads = [t for t in reads if t in truth]
        self.assertEqual(len(sample_reads), len(set(sample_reads)), "each sample is read once")
        self.assertEqual(set(reads), set(times) | elsewhere, "and every other time once")
        self.assertEqual(len(reads), len(set(reads)))

    def test_memo_keeps_a_few_bytes_per_sample(self):
        """Regression guard: 430k samples (30 days at 6 s) must not cost a dict entry each."""
        import tracemalloc

        from tuner_under_test.rooms import attribution

        times = array("d", (NOW + i * 6.0 for i in range(200_000)))
        label = ("present", "human", 1.0)
        memo = attribution.Memo(lambda t: label, times)
        tracemalloc.start()
        try:
            for t in times:
                memo(t)
            kept = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        self.assertLess(kept / len(times), 8, "bytes kept per sample")


class EditCostTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def test_an_edit_copies_the_legacy_histograms_once(self):
        """Regression: every edit deep-copied the histograms for a ``setdefault`` that only
        the first edit needed."""
        from tuner_under_test.history import labels

        self.runtime._ensure_histograms(self.device)
        before = deepcopy(self.device["histograms"])
        start = self.now - 7200
        self.runtime.label_history_range("a", start, start + 600, "present")
        self.assertEqual(self.device["history_legacy_histograms"], before)
        kept = self.device["history_legacy_histograms"]
        with patch.object(labels, "deepcopy", wraps=labels.deepcopy) as copy:
            self.runtime.label_history_range("a", start + 900, start + 1500, "not_present")
        copy.assert_not_called()
        self.assertIs(self.device["history_legacy_histograms"], kept)


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
        rng = random.Random(4190)
        rows = [
            (self.now - days * DAY + 600 + i * 60, rng.randbytes(18)) for i in range(days * 1380)
        ]
        self.device["history"], self.device["guess_runs"] = confident(rows)

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

        from tuner_under_test.history import label_counts

        self.history()
        loop_thread, where = threading.current_thread(), []
        real = label_counts.count

        def spy(*args):
            where.append(threading.current_thread() is loop_thread)
            return real(*args)

        with patch.object(label_counts, "count", spy):
            result = await self.edit()
            await self.edit()
        self.assertEqual(result["labelled_samples"], 60)
        self.assertEqual(where, [False], "counted once, off the loop")

    async def test_cleanup_discarded_by_new_samples_still_keeps_the_counts(self):
        """Regression: a cleanup result dropped because a sample arrived meanwhile also
        dropped the counts it had made."""
        self.history()
        real = self.hass.async_add_executor_job

        async def executor(fn, *args):
            result = await real(fn, *args)
            if fn.__name__ == "_clean_and_count":
                self.runtime._record_history_sample("a", {"g0_move": 5}, self.now)
            return result

        self.hass.async_add_executor_job = executor
        await self.runtime.async_clean_history(persist=False)
        self.assertIn("a", self.runtime._label_counts)


class MemoNanTests(unittest.TestCase):
    def test_answers_with_nan_share_one_entry(self):
        """Regression: NaN never equals itself, so each sample added another distinct answer
        (unbounded); the answers now stay as few as the distinct ones."""
        from tuner_under_test.rooms import attribution

        times = array("d", (NOW + i * 6.0 for i in range(500)))
        memo = attribution.Memo(lambda t: ("present", "automatic", float("nan")), times)
        for t in times:
            self.assertEqual(memo(t)[:2], ("present", "automatic"))
        self.assertEqual(len(memo.answers), 1)


class FrozenBlock(dict):
    """A stored history block that refuses to change: blocks are shared between the live data,
    saved snapshots and the kept counts, so they must only ever be replaced."""

    def _refuse(self, *args, **kwargs):
        raise AssertionError("a stored history block was changed in place")

    __setitem__ = __delitem__ = update = pop = popitem = setdefault = clear = _refuse

    def __deepcopy__(self, memo):
        return deepcopy(dict(self), memo)


class StoredBlocksAreImmutableTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    async def test_no_operation_changes_a_stored_block_in_place(self):
        rng = random.Random(4195)
        rows = [(self.now - 3 * DAY + 600 + i * 60, rng.randbytes(18)) for i in range(3000)]
        history, runs = confident(rows)
        self.device["history"] = [FrozenBlock(block) for block in history]
        self.device["guess_runs"] = [FrozenBlock(chunk) for chunk in runs]
        runtime, start = self.runtime, self.now - 2 * DAY
        for step in range(70):  # Pending samples that flush into a new block.
            runtime._record_history_sample("a", {"g0_move": step % 90}, self.now - 600 + step * 6)
        runtime._flush_history_block("a")
        runtime.label_history_range("a", start, start + 7200, "present")
        label = self.device["history_labels"][0]
        runtime.edit_history_label(
            "a",
            label["start"],
            label["end"],
            start,
            start + 3600,
            "not_present",
            self.device["label_revision"],
        )
        harness.sys.modules["tuner_under_test.history.source_labels"].confirm_presence(
            runtime, "a", start + 7200, start + 9000, 77
        )
        await runtime.async_clean_history(persist=False)
        runtime.label_history_range("a", start, start + 600, "unknown")
        storage = harness.sys.modules["tuner_under_test.history.storage"]
        await storage.async_save(runtime)
        self.assertTrue(self.device["history"])
