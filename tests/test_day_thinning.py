"""Finished-day retention removes elapsed ranges without bridging their gaps."""

import datetime
import unittest

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import budget, cleanup, dayfile, daystore, policy

START = 1_760_486_400.0
DATE = datetime.date(2025, 10, 15)


def fixture():
    return dayfile.day_from_log(
        [
            dayfile.Change(START, {0: 10, 1: 40.125}),
            dayfile.Guess(START, "present", 14, 0),
            dayfile.Change(START + 30, {0: 80}),
            dayfile.End(START + 120),
        ],
        None,
        START,
        START + policy.DAY,
    )


def rows(day):
    return [
        (a - START, b - START, day.vector(i)[:2])
        for i, (a, b) in enumerate(zip(day.times, day.ends, strict=True))
    ]


def test_low_confidence_unlabelled_range_becomes_a_gap():
    original = fixture()
    found = cleanup.cut(original, [(START + 20, START + 50)])
    assert rows(found) == [(0, 20, (10, 40.125)), (50, 120, (80, 40.125))]
    assert found.guesses == [
        (START, START + 20, "present", 14, 0),
        (START + 50, START + 120, "present", 14, 0),
    ]
    assert rows(original) == [(0, 30, (10, 40.125)), (30, 120, (80, 40.125))]


def test_cut_across_label_edge_keeps_both_sides_exact():
    found = cleanup.cut(fixture(), [(START + 25, START + 45)])
    assert rows(found) == [(0, 25, (10, 40.125)), (45, 120, (80, 40.125))]
    assert rows(cleanup.cut(found, [(START, START + 20), (START + 110, START + 200)])) == [
        (20, 25, (10, 40.125)),
        (45, 110, (80, 40.125)),
    ]


def test_human_labelled_time_protected_until_human_days(tmp_path):
    store = daystore.DayStore(tmp_path / "history")
    store.replace("arbitrary radar", DATE, fixture())
    data = {
        "devices": {
            "arbitrary radar": {
                "history_labels": [{"start": START + 40, "end": START + 80, "state": "present"}]
            }
        }
    }
    rules = {**policy.DEFAULTS, "human_days": 60}
    plan = budget.plan(store, data, START + 31 * policy.DAY, rules)
    # The recording ends at START + 120: the empty rest of the day is not cut (nothing to cut).
    assert plan == [("arbitrary radar", DATE, [(START, START + 40), (START + 80, START + 120)])]


def test_live_day_is_never_rewritten(tmp_path):
    store = daystore.DayStore(tmp_path / "history")
    store.append("radar", dayfile.Change(START, {0: 10}))
    assert (
        budget.plan(store, {"devices": {"radar": {}}}, START + 31 * policy.DAY, policy.DEFAULTS)
        == []
    )


def test_ceiling_trims_lowest_confidence_then_oldest(tmp_path):
    store = daystore.DayStore(tmp_path / "history")
    day = fixture()
    day.guesses = [
        (START, START + 30, "present", 18, 0),
        (START + 30, START + 120, "present", 12, 0),
    ]
    store.replace("radar", DATE, day)
    data = {"devices": {"radar": {}}}
    target = store.size() + budget.storage_bytes(data) - 1
    cuts = budget.plan(store, data, START + policy.DAY, policy.DEFAULTS, target=target)
    assert cuts[0][0:2] == ("radar", DATE)
    assert cuts[0][2][0] == (START + 30, START + 120)


def test_ceiling_trims_even_confident_automatic_time_before_human_time(tmp_path):
    store = daystore.DayStore(tmp_path / "history")
    day = fixture()
    day.guesses = [(START, START + 120, "present", 20, 0)]  # Certain, but automatic.
    store.replace("radar", DATE, day)
    labels = [{"start": START, "end": START + 20, "state": "present"}]  # The oldest time.
    data = {"devices": {"radar": {"history_labels": labels}}}
    target = store.size() + budget.storage_bytes(data) - 1
    cuts = budget.plan(store, data, START + policy.DAY, policy.DEFAULTS, target=target)
    assert cuts == [("radar", DATE, [(START + 20, START + 120)])]


def test_trim_now_target_and_report_counts_seconds_removed(tmp_path):
    store = daystore.DayStore(tmp_path / "history")
    store.replace("radar", DATE, fixture())
    data = {
        "devices": {
            "radar": {
                "history_labels": [{"start": START + 40, "end": START + 80, "state": "present"}]
            }
        }
    }
    data["storage_settings"] = {**policy.DEFAULTS, "human_days": 60}
    prepared, changes, report = budget.prepare_days(store, data, START + 31 * policy.DAY, True)
    assert report["removed_automatic_seconds"] == 80
    assert report["removed_human_seconds"] == 0
    assert report["removed_automatic"] == 80
    assert report["history_basis"] == "observed_seconds"
    assert rows(changes[0][2]) == [(40, 80, (80, 40.125))]
    assert (
        prepared["devices"]["radar"]["history_labels"] == data["devices"]["radar"]["history_labels"]
    )
    assert prepared["devices"]["radar"]["label_revision"] == 1
    assert "histograms" not in data["devices"]["radar"], "planning leaves input metadata untouched"
    assert rows(store.day("radar", DATE)) == rows(fixture()), "planning never edits storage"


class TestDayStorage(unittest.IsolatedAsyncioTestCase):
    """Exercise the real save/trim boundary with ended synthetic coverage."""

    def setUp(self):
        import time
        from unittest.mock import patch

        test_tuner.RuntimeTests.setUp(self)
        self.now = START + 10 * policy.DAY + 1000
        clock = patch.object(time, "time", return_value=self.now)
        self.clock = clock.start()
        self.addCleanup(clock.stop)

    asyncTearDown = test_tuner.RuntimeTests.asyncTearDown

    def recorded(self):
        from tuner_under_test.history import ingest

        self.runtime._record_history_sample("a", {"g0_move": 10}, START)
        ingest.end(self.runtime, "a", START + 120)

    async def test_trim_removes_finished_day_and_reports_recorded_seconds(self):
        self.recorded()
        report = await self.runtime.trim_storage()
        assert report["removed_automatic_seconds"] == 120
        assert self.runtime._days.day("a", DATE) is None
        saved = self.runtime.store.async_save.call_args.args[0]["devices"]["a"]
        assert "history" not in saved and "guess_runs" not in saved

    async def test_label_edit_while_preparing_cannot_remove_newly_human_time(self):
        self.recorded()
        raced = False

        async def race(request):
            nonlocal raced
            if not raced and request["operation"] == "maintenance":
                raced = True
                await self.runtime.label_history_range("a", START, START + 120, "present")

        self.worker_transport.after = race
        report = await self.runtime.trim_storage()
        assert raced
        assert report["removed_human_seconds"] == 0
        assert rows(self.runtime._days.day("a", DATE)) == [(0, 120, (10, None))]

    async def test_trim_updates_live_and_saved_histograms_from_retained_time(self):
        self.recorded()
        await self.runtime.label_history_range("a", START, START + 120, "present")
        self.runtime.data["storage_settings"] = {
            **policy.DEFAULTS,
            "human_days": 9,
            "automatic_days": 8,
        }
        report = await self.runtime.trim_storage()
        assert report["removed_human_seconds"] == 120
        assert sum(self.device["histograms"]["g0_move"]["present"]) == 0
        saved = self.runtime.store.async_save.call_args.args[0]["devices"]["a"]
        assert saved["label_revision"] == self.device["label_revision"]
        assert saved["histograms"] == self.device["histograms"]

    async def test_trim_histograms_are_the_label_edit_histograms_and_compact(self):
        """One histogram rule: a trim stores what a label edit rebuilds, rounded to a
        thousandth of a sample so the saved file stays small."""
        from tuner_under_test.history import ingest, labels

        self.runtime._record_history_sample("a", {"g0_move": 10}, START)
        ingest.end(self.runtime, "a", START + 121)
        # 121 s labelled: 20.1666... samples, which a stored histogram keeps to 0.001.
        await self.runtime.label_history_range("a", START, START + 121, "present")
        self.runtime.data["storage_settings"] = {**policy.DEFAULTS, "automatic_days": 8}
        await self.runtime.trim_storage()
        self.runtime._label_counts.clear()
        assert self.device["histograms"] == labels._rebuild_histograms(self.runtime, self.device)
        values = [
            value
            for series in self.device["histograms"].values()
            for histogram in series.values()
            for value in histogram
        ]
        assert values and all(round(value, 3) == value for value in values)

    async def test_hourly_cleanup_uses_the_same_day_retention_without_writing_json_history(self):
        self.recorded()
        changed = await self.runtime.async_clean_history(persist=False)
        assert changed
        assert self.runtime._days.day("a", DATE) is None
        assert "history" not in self.device and "guess_runs" not in self.device
        self.runtime.store.async_save.assert_not_awaited()

    async def test_unchanged_live_poll_during_trim_keeps_its_new_duration(self):
        self.recorded()
        self.device["history_labels"] = [
            {"start": self.now - 60, "end": self.now + 60, "state": "present"}
        ]
        for timestamp in (self.now - 60, self.now - 6):
            self.runtime._record_history_sample("a", {"g0_move": 20}, timestamp)
        raced = False

        async def race(request):
            nonlocal raced
            if not raced and request["operation"] == "maintenance":
                raced = True
                self.clock.return_value = self.now + 6
                self.runtime._record_history_sample("a", {"g0_move": 20}, self.now + 6)

        self.worker_transport.after = race
        await self.runtime.trim_storage()
        assert raced
        assert sum(self.device["histograms"]["g0_move"]["present"]) == 11
        saved = self.runtime.store.async_save.call_args.args[0]["devices"]["a"]
        assert sum(saved["histograms"]["g0_move"]["present"]) == 11


def test_ceiling_breaks_equal_confidence_ties_by_oldest_recorded_time(tmp_path):
    store = daystore.DayStore(tmp_path / "history")
    for date, begin in ((DATE, START), (DATE + datetime.timedelta(days=1), START + policy.DAY)):
        day = dayfile.day_from_log(
            [
                dayfile.Change(begin, {0: 10}),
                dayfile.Guess(begin, "present", 14, 0),
                dayfile.End(begin + 120),
            ],
            None,
            begin,
            begin + policy.DAY,
        )
        store.replace("radar", date, day)
    data = {"devices": {"radar": {}}}
    target = store.size() + budget.storage_bytes(data) - 1
    found = budget.plan(store, data, START + 2 * policy.DAY, policy.DEFAULTS, target)
    assert found[0] == ("radar", DATE, [(START, START + 120)])


def test_age_confidence_cutoff_keeps_high_confidence_time_exactly(tmp_path):
    store = daystore.DayStore(tmp_path / "history")
    day = fixture()
    day.guesses = [
        (START, START + 30, "present", 14, 0),
        (START + 30, START + 120, "present", 18, 0),
    ]
    store.replace("radar", DATE, day)
    data = {"devices": {"radar": {}}}
    found = budget.plan(store, data, START + 20 * policy.DAY, policy.DEFAULTS)
    assert rows(cleanup.cut(day, found[0][2])) == [(30, 120, (80, 40.125))]


def test_ceiling_planning_encodes_each_changed_day_once_per_cut(tmp_path):
    """Regression: every candidate cut re-encoded every day cut so far (quadratic)."""
    from unittest.mock import patch

    store = daystore.DayStore(tmp_path / "history")
    days = 12
    for index in range(days):
        begin = START + index * policy.DAY
        records = [dayfile.Change(begin + i * 60, {0: i % 7 + 1}) for i in range(10)]
        day = dayfile.day_from_log(
            [*records, dayfile.End(begin + 600)], None, begin, begin + policy.DAY
        )
        day.guesses = [
            (begin + i * 60, begin + i * 60 + 60, "present", 10 + i, 0) for i in range(10)
        ]
        store.replace("radar", DATE + datetime.timedelta(days=index), day)
    data = {"devices": {"radar": {}}}
    target = budget.storage_bytes(data) + 1  # Everything must go: every candidate is cut.
    with patch.object(dayfile, "encode_day", wraps=dayfile.encode_day) as encoded:
        budget.plan(store, data, START + days * policy.DAY, policy.DEFAULTS, target=target)
    candidates = days * 10
    assert encoded.call_count <= 2 * days + candidates


def test_cleanup_settles_after_the_first_run(tmp_path):
    """A second cleanup must change nothing: ranges already cut are gaps, not new cuts."""
    store = daystore.DayStore(tmp_path / "history")
    store.open(now=START)
    for record in (
        dayfile.Change(START, {0: 10}),
        dayfile.Guess(START, "present", 20, 0),  # 100 % confident: kept for 30 days.
        dayfile.Guess(START + 3600, None, 0, 0),  # Unguessed: thinned after 7 days.
        dayfile.Change(START + 3601, {0: 30}),
        dayfile.End(START + 7200),
    ):
        store.append("radar", record)
    store.append("radar", dayfile.Change(START + policy.DAY + 1, {0: 1}))
    store.flush(now=START + policy.DAY + 2)
    data = {"devices": {"radar": {}}}
    now = START + 10 * policy.DAY
    prepared, changes, _report = budget.prepare_days(store, data, now, clean=True)
    assert [date for _key, date, _day in changes] == [DATE]
    store.commit(store.stage(changes))
    assert store.day("radar", DATE).ends[-1] == START + 3600  # The unguessed hour is gone.
    _prepared, again, report = budget.prepare_days(store, prepared, now + 3600, clean=True)
    assert again == []
    assert report["removed_automatic_seconds"] == 0


def test_an_ordinary_save_serialises_the_store_once(tmp_path, monkeypatch):
    """Saves measured the JSON store three times and copied it again: on a small machine
    that alone took longer than the recording interval."""
    from tuner_under_test.history import storage

    store = daystore.DayStore(tmp_path / "history")
    data = {"devices": {"radar": {"histograms": {"g0_move": {"present": [1.0] * 101}}}}}
    measured = []
    real = budget.storage_bytes
    monkeypatch.setattr(budget, "storage_bytes", lambda d: measured.append(1) or real(d))
    monkeypatch.setattr(budget, "copy_data", lambda d: (_ for _ in ()).throw(AssertionError))
    prepared, changes, report, staged = storage._prepare_day_save(store, data, START, False, None)
    assert (prepared, changes, staged) == (data, [], [])
    assert report["used_bytes"] == report["before_bytes"] == store.size() + real(data)
    assert len(measured) == 1


def test_hourly_planning_reads_no_unchanged_day(tmp_path, monkeypatch):
    from tuner_under_test.history import day_results

    day_results.clear()
    store = daystore.DayStore(tmp_path / "history")
    store.open(now=START)
    for record in (
        dayfile.Change(START, {0: 10}),
        dayfile.Guess(START, "present", 20, 0),
        dayfile.Guess(START + 3600, None, 0, 0),
        dayfile.Change(START + 3601, {0: 30}),
        dayfile.End(START + 7200),
    ):
        store.append("radar", record)
    store.append("radar", dayfile.Change(START + policy.DAY + 1, {0: 1}))
    store.flush(now=START + policy.DAY + 2)
    data = {"devices": {"radar": {}}}
    now = START + 10 * policy.DAY
    first = budget.plan(store, data, now, policy.DEFAULTS)
    store._cache.clear()
    monkeypatch.setattr(store, "day", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert budget.plan(store, data, now + 3600, policy.DEFAULTS) == first


def test_removed_time_is_read_from_the_cut_ranges_alone():
    """What a cut removes, by label, is the time it covers: the day's time before less its
    time after, without reading every span of the day twice."""
    import random
    from array import array

    from tuner_under_test.history import spans

    rnd = random.Random(5)
    for _ in range(20):
        rows = 400
        times = array("d", sorted(rnd.uniform(0, 86_000) for _ in range(rows)))
        ends = array("d", (min(t + rnd.choice((0.5, 6, 30)), 86_400) for t in times))
        ends = array(
            "d", ((min(end, times[i + 1]) if i + 1 < rows else end) for i, end in enumerate(ends))
        )
        columns = [bytes(rnd.choice((255, 3, 40)) for _ in range(rows)) for _ in range(18)]
        day = dayfile.Day(0.0, 86_400.0, times, ends, columns, [])
        labels = []
        for start in sorted(rnd.uniform(0, 86_000) for _ in range(6)):
            labels.append(
                {
                    "start": start,
                    "end": start + rnd.uniform(10, 4000),
                    "state": rnd.choice(("present", "not_present", "unknown")),
                }
            )
        device = {"history_labels": labels, "training_state": "unknown"}
        cuts = [
            (low, low + rnd.uniform(1, 9000))
            for low in sorted(rnd.uniform(-100, 86_000) for _ in range(3))
        ]
        before, after = (
            budget._day_seconds(day, device),
            budget._day_seconds(cleanup.cut(day, cuts), device),
        )
        removed = budget._removed_seconds(day, cuts, device)
        assert removed == pytest.approx(
            [b - a for b, a in zip(before, after, strict=True)], abs=1e-6
        )
        assert sum(removed) > 0 or not list(spans.day_spans(day))
