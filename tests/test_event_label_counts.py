"""Human histograms count recorded physical duration (day files), split exactly at labels."""

import asyncio
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_tuner as harness
from tuner_under_test.history import dayfile, daystore, label_counts, labels

DAY = 86400


def vector(value):
    return (value,) + (None,) * 17


def view(stretches=(), manual=(), open_until=None):
    """A radar recorded over ``stretches`` ``[(events, end)]`` and labelled ``manual``; with
    ``open_until`` the last stretch is still being recorded, read up to that time."""
    device = {
        "history_labels": list(manual),
        "training_state": "unknown",
        "history_legacy_histograms": {},
    }
    runtime = SimpleNamespace(
        data={"devices": {"arbitrary": device}},
        _label_counts={},
        _history_runtime={},
        _compress_histogram=labels._compress_histogram,
    )
    for index, (events, end) in enumerate(stretches):
        last = index == len(stretches) - 1
        harness.recorded(runtime, "arbitrary", events, end, open_until=open_until if last else None)
    return runtime, device


def counted(runtime, device, now):
    with patch.object(label_counts.time, "time", return_value=now):
        return label_counts.current(runtime, device)


def test_stable_native_span_splits_exactly_at_human_label_boundaries():
    runtime, device = view(
        [([(100, vector(10.125))], 160)],
        [
            {"start": 100, "end": 115, "state": "present"},
            {"start": 115, "end": 160, "state": "not_present"},
        ],
    )
    found = counted(runtime, device, 200)
    assert found["g0_move"]["present"][11] == 15 / 6
    assert found["g0_move"]["not_present"][11] == 45 / 6
    assert sum(found["g0_still"]["present"]) == 0


def test_a_live_pulse_is_counted_once_before_and_after_its_day_is_finished():
    events = [(100, vector(0)), (100.125, vector(40.125)), (100.175, vector(0))]
    manual = [{"start": 100, "end": 120, "state": "present"}]
    runtime, device = view([(events, None)], manual, open_until=106)
    first = deepcopy(counted(runtime, device, 200))
    assert first["g0_move"]["present"][41] == pytest.approx(0.05 / 6)
    runtime._history_until = 112  # Still recording: observed up to 112 s.
    second = deepcopy(counted(runtime, device, 200))
    assert sum(second["g0_move"]["present"]) == pytest.approx(2)
    runtime._days.append("arbitrary", dayfile.End(112))
    runtime._days.flush(now=DAY + 1)  # The day ends and is written as a finished file.
    for _ in range(2):
        assert counted(runtime, device, 200)["g0_move"]["present"] == pytest.approx(
            second["g0_move"]["present"], abs=1e-12
        )


def test_native_relabel_unknown_gap_and_unavailability_add_no_phantom_mass():
    runtime, device = view(
        [
            ([(100, vector(10)), (106, vector(None))], 112),
            ([(130, vector(10))], 136),
        ],
        [{"start": 100, "end": 136, "state": "present"}],
    )
    assert counted(runtime, device, 200)["g0_move"]["present"][10] == 2
    device["history_labels"] = labels.apply_range(device["history_labels"], 103, 133, "not_present")
    found = counted(runtime, device, 200)
    assert found["g0_move"]["present"][10] == 1
    assert found["g0_move"]["not_present"][10] == 1
    device["history_labels"] = labels.apply_range(device["history_labels"], 100, 136, "unknown")
    assert sum(counted(runtime, device, 200)["g0_move"]["present"]) == 0
    assert sum(counted(runtime, device, 200)["g0_move"]["not_present"]) == 0


def test_retention_clips_recorded_duration_at_the_exact_cutoff():
    runtime, device = view(
        [([(100, vector(20))], 160)], [{"start": 0, "end": 200, "state": "present"}]
    )
    retention = 30 * DAY  # The default human retention.
    assert counted(runtime, device, 100 + retention)["g0_move"]["present"][20] == 10
    assert counted(runtime, device, 130 + retention)["g0_move"]["present"][20] == 5


def test_compressing_native_histogram_keeps_fractional_pulse_mass():
    histogram = [0] * 101
    histogram[0], histogram[41] = 5001.0, 0.05 / 6
    total = sum(histogram)
    labels._compress_histogram(histogram)
    assert histogram[41] == pytest.approx(0.05 / 6 * 5000 / total)
    legacy = [0] * 101
    legacy[0], legacy[41] = 5001, 1
    labels._compress_histogram(legacy)
    assert legacy[41] == 0


def test_native_normalization_is_bounded_and_independent_of_delivery_density():
    pulse = [(0, vector(0)), (1000, vector(40.125)), (1000.05, vector(0))]
    repeated = [*pulse, *((start, vector(0)) for start in range(3600, DAY, 3600))]
    histograms = []
    for events in (pulse, repeated):  # Unchanged repeated readings store nothing more.
        runtime, device = view([(events, DAY - 1)], [{"start": 0, "end": DAY, "state": "present"}])
        with patch.object(label_counts.time, "time", return_value=DAY):
            histogram = labels._rebuild_histograms(runtime, device)["g0_move"]["present"]
        assert sum(histogram) == pytest.approx(5000)
        assert histogram[41] > 0
        histograms.append(histogram)
    assert histograms[0] == pytest.approx(histograms[1], abs=1e-12)


def test_dense_half_second_changes_in_same_histogram_bin_keep_exact_support():
    manual = [{"start": 0, "end": 120, "state": "present"}]
    stable = [([(0, vector(10.1))], 120)]
    dense = [([(i * 0.5, vector(10.1 if i % 2 else 10.9)) for i in range(240)], 120)]
    divided = [([(i * 0.5, vector(10.1))], (i + 1) * 0.5) for i in range(240)]
    for stretches in (stable, dense, divided):
        runtime, device = view(stretches, manual)
        assert counted(runtime, device, 200)["g0_move"]["present"][11] == pytest.approx(20)


def test_a_cached_edit_recounts_only_the_day_it_touches():
    stretches = [([(day * DAY, vector(10))], day * DAY + 600) for day in range(3)]
    runtime, device = view(stretches, [{"start": 0, "end": 3 * DAY, "state": "present"}])
    runtime._days.flush(now=3 * DAY + 1)  # Every day finished.
    now = 3 * DAY + 10
    assert counted(runtime, device, now)["g0_move"]["present"][10] == 300
    device["history_labels"] = labels.apply_range(
        device["history_labels"], DAY + 9, DAY + 10, "unknown"
    )
    with patch.object(label_counts, "_native_spans", wraps=label_counts._native_spans) as read:
        assert counted(runtime, device, now)["g0_move"]["present"][10] == pytest.approx(299 + 5 / 6)
    finished = [call for call in read.call_args_list if call.args and call.args[0] is not None]
    assert len(finished) == 1  # Of the finished days, only the touched one is read again.
    runtime._days.delete("arbitrary", [daystore.date_of(0)])
    assert counted(runtime, device, now)["g0_move"]["present"][10] == pytest.approx(199 + 5 / 6)


def test_a_corrupt_day_file_does_not_poison_valid_counts():
    stretches = [([(0, vector(10))], 6), ([(DAY, vector(20))], DAY + 6)]
    runtime, device = view(stretches, [{"start": 0, "end": 2 * DAY, "state": "present"}])
    runtime._days.flush(now=2 * DAY + 1)
    second = runtime._days.folder("arbitrary") / f"{daystore.date_of(DAY).isoformat()}.day"
    second.write_bytes(b"corrupt")
    runtime._days = daystore.DayStore(runtime._days.root)  # A restart: nothing cached.
    runtime._days.open(now=2 * DAY + 10)
    found = counted(runtime, device, 2 * DAY + 10)
    assert found["g0_move"]["present"][10] == 1
    assert sum(found["g0_move"]["present"]) == 1


class NativeLabelRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clock = patch.object(labels.time, "time", return_value=1_800_000_000.0)
        clock.start()
        self.addCleanup(clock.stop)
        harness.RuntimeTests.setUp(self)

    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def native(self, start, value=10.125):
        """Recording from ``start``: still open, read up to each operation's time."""
        self.runtime._days.append("a", dayfile.Change(start, {0: value}))

    async def test_current_label_changes_count_only_the_exact_observed_intervals(self):
        with patch.object(labels.time, "time", return_value=self.now):
            self.runtime.set_training_state("a", "present")
        self.native(self.now)
        with patch.object(labels.time, "time", return_value=self.now + 12):
            self.runtime.set_training_state("a", "not_present")
            # Counts are not kept yet: the histograms follow once the worker has counted.
            await asyncio.gather(*self.runtime._label_rebuilds.values())
        assert self.device["histograms"]["g0_move"]["present"][11] == pytest.approx(2, abs=1e-3)
        with patch.object(labels.time, "time", return_value=self.now + 30):
            self.runtime.set_training_state("a", "unknown")
            await harness.settled(self.runtime)
        assert self.device["histograms"]["g0_move"]["present"][11] == pytest.approx(2, abs=1e-3)
        assert self.device["histograms"]["g0_move"]["not_present"][11] == pytest.approx(3)

    async def test_retrospective_response_reports_physical_observation_not_event_count(self):
        self.native(self.now - 60)
        with patch.object(labels.time, "time", return_value=self.now):
            result = await self.runtime.label_history_range(
                "a", self.now - 45, self.now - 15, "present"
            )
            await harness.settled(self.runtime)
        assert result["labelled_samples"] == 30 / 6
        assert result["observed_seconds"] == pytest.approx(30)
        assert result["support_basis"] == "observed_native_duration"
        assert self.device["histograms"]["g0_move"]["present"][11] == pytest.approx(5)

    async def test_ensure_counts_the_live_day_and_clear_forgets_it(self):
        self.now = 1_800_000_000.0  # A fixed within-day clock, independent of UTC midnight.
        with patch.object(labels.time, "time", return_value=self.now):
            self.native(self.now - 60)
            self.device["history_labels"] = [
                {"start": self.now - 60, "end": self.now, "state": "present"}
            ]
            await label_counts.ensure(self.runtime, "a")
            present = self.runtime._label_counts["a"].raw["g0_move"]["present"][11]
            assert present == pytest.approx(10, abs=1e-3)  # Recorded from the millisecond of now.
            self.runtime.clear_samples("a")
            assert "a" not in self.runtime._label_counts
            assert not sum(label_counts.current(self.runtime, self.device)["g0_move"]["present"])

    async def test_timeout_rebuild_stops_at_expiry_despite_later_native_observations(self):
        self.native(self.now)
        self.device.update(
            training_state="present",
            training_label_start=self.now,
            training_expires_at=self.now + 12,
            history_legacy_histograms={},
        )
        with patch.object(labels.time, "time", return_value=self.now + 30):
            await labels._timeout_worker(self.runtime, "a", 0)
            await asyncio.gather(*self.runtime._label_rebuilds.values())
        assert self.device["training_state"] == "unknown"
        # Recorded from the millisecond of ``now``: within a millisecond of 12 s.
        assert self.device["histograms"]["g0_move"]["present"][11] == pytest.approx(2, abs=1e-3)

    async def test_unavailable_native_span_reports_no_observed_support(self):
        self.native(self.now - 60, None)
        with patch.object(labels.time, "time", return_value=self.now):
            result = await self.runtime.label_history_range("a", self.now - 60, self.now, "present")
            await harness.settled(self.runtime)
        assert result["observed_seconds"] == 0
        assert sum(self.device["histograms"]["g0_move"]["present"]) == 0
