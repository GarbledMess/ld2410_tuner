"""The day store: one folder per radar, append-only live logs, finished columnar days."""

import datetime
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile, daystore

DAY = datetime.date(2025, 10, 15)
START = 1_760_486_400.0  # 2025-10-15T00:00:00Z
NEXT = START + 86_400


def change(at, **gates):
    return dayfile.Change(at, {int(key[1:]): value for key, value in gates.items()})


def store(tmp_path, **kwargs):
    days = daystore.DayStore(tmp_path / "history", **kwargs)
    days.open(now=START)
    return days


def first_gate(day):
    return [day.vector(index)[0] for index in range(len(day.times))]


def test_append_flush_and_read_back(tmp_path):
    days = store(tmp_path)
    days.append("radar", change(START + 1, g0=10))
    days.append("radar", change(START + 2, g0=20))
    assert first_gate(days.day("radar", DAY, until=START + 5)) == [10, 20]
    days.flush(now=START + 5)
    again = store(tmp_path)
    day = again.day("radar", DAY, until=START + 5)
    assert first_gate(day) == [10, 20]
    assert day.ends[-1] == START + 2  # Reopened: coverage ends at the last stored record.


def test_unchanged_observations_continue_open_coverage_after_midnight(tmp_path):
    days = store(tmp_path)
    days.append("radar", change(NEXT - 2, g0=10))
    days.append("radar", dayfile.Guess(NEXT - 2, "present", 16, 0))
    days.observe("radar", NEXT + 5)
    following = days.day("radar", DAY + datetime.timedelta(days=1), until=NEXT + 5)
    assert following is not None
    assert first_gate(following) == [10]
    assert list(following.times) == [NEXT]
    assert list(following.ends) == [NEXT + 5]
    assert following.guesses == [(NEXT, NEXT + 5, "present", 16, 0)]
    from tuner_under_test.history import guesses, spans

    view = SimpleNamespace(_days=days, _history_until=NEXT + 5)
    assert guesses.of(view, "radar").runs == [(NEXT - 2, NEXT + 5, "present", 16, 0)]
    assert spans.coverage(spans.events(view, "radar")) == [(NEXT - 2, NEXT + 5)]


def test_flush_writes_only_new_bytes(tmp_path):
    days = store(tmp_path)
    days.append("radar", change(START + 1, g0=10))
    first = days.flush(now=START + 2)
    days.append("radar", change(START + 3, g0=11))
    second = days.flush(now=START + 4)
    log = days.folder("radar") / "2025-10-15.log"
    assert log.stat().st_size == first + second
    assert second == len(dayfile.encode_records([change(START + 3, g0=11)], START + 1))
    assert days.flush(now=START + 5) == 0


def test_midnight_rollover_carries_vector_and_finishes_previous_day(tmp_path):
    days = store(tmp_path)
    days.append("radar", change(NEXT - 1, g0=30))
    days.append("radar", change(NEXT + 60, g0=31))
    days.flush(now=NEXT + 61)
    assert (days.folder("radar") / "2025-10-15.day").exists()
    assert not (days.folder("radar") / "2025-10-15.log").exists()
    yesterday = days.day("radar", DAY)
    today = days.day("radar", DAY + datetime.timedelta(days=1), until=NEXT + 61)
    assert list(yesterday.ends) == [NEXT]
    assert list(today.times) == [NEXT, NEXT + 60]
    assert first_gate(today) == [30, 31]


def test_silent_day_still_gets_its_file_with_the_carried_vector(tmp_path):
    days = store(tmp_path)
    days.append("radar", change(NEXT - 1, g0=30))
    days.flush(now=NEXT + 86_400 + 5)
    second = days.day("radar", DAY + datetime.timedelta(days=1))
    assert list(second.times) == [NEXT] and list(second.ends) == [NEXT + 86_400]


def test_crash_between_compaction_steps_reads_each_record_once(tmp_path):
    days = store(tmp_path)
    days.append("radar", change(START + 1, g0=10))
    days.append("radar", dayfile.End(START + 2))
    days.flush(now=NEXT + 1)  # Finishes 2025-10-15.
    folder = days.folder("radar")
    (folder / "2025-10-15.log").write_bytes(
        dayfile.encode_records([change(START + 1, g0=10), dayfile.End(START + 2)], START)
    )
    (folder / "2025-10-15.day.tmp").write_bytes(b"partial")
    again = store(tmp_path)
    assert first_gate(again.day("radar", DAY)) == [10]
    assert sorted(path.name for path in folder.iterdir()) == ["2025-10-15.day"]


def test_truncated_log_tail_after_crash(tmp_path):
    days = store(tmp_path)
    days.append("radar", change(START + 1, g0=10))
    days.append("radar", change(START + 2, g0=20))
    days.flush(now=START + 3)
    log = days.folder("radar") / "2025-10-15.log"
    log.write_bytes(log.read_bytes()[:-1])
    day = store(tmp_path).day("radar", DAY, until=START + 10)
    assert first_gate(day) == [10]
    assert list(day.ends) == [START + 1]


def test_unicode_and_punctuated_device_ids_get_safe_folders(tmp_path):
    days = store(tmp_path)
    for device_id in ("sÃƒÆ’Ã‚Â¡lon/radar:1", "..", "a b"):
        days.append(device_id, change(START + 1, g0=1))
    days.flush(now=START + 2)
    names = sorted(path.name for path in (tmp_path / "history").iterdir())
    assert all("/" not in name and name not in (".", "..") for name in names)
    assert len(names) == 3
    assert first_gate(store(tmp_path).day("sÃƒÆ’Ã‚Â¡lon/radar:1", DAY)) == [1]


def test_device_without_history_reads_empty(tmp_path):
    days = store(tmp_path)
    assert days.day("never", DAY) is None
    assert days.days("never") == []


def test_delete_replace_and_size_accounting(tmp_path):
    days = store(tmp_path)
    days.append("radar", change(START + 1, g0=10))
    days.append("radar", change(NEXT + 1, g0=11))
    days.flush(now=NEXT + 2)
    assert days.days("radar") == [DAY, DAY + datetime.timedelta(days=1)]
    before = days.size()
    replacement = dayfile.day_from_log([change(START + 5, g0=12)], None, START, NEXT)
    days.replace("radar", DAY, replacement)
    assert first_gate(days.day("radar", DAY)) == [12]
    days.delete("radar", [DAY])
    assert days.days("radar") == [DAY + datetime.timedelta(days=1)]
    assert days.size() < before
    days.remove_device("radar")
    assert days.days("radar") == [] and not days.folder("radar").exists()


def test_cache_is_bounded(tmp_path):
    days = store(tmp_path, cache_bytes=1)
    for offset in range(3):
        days.append("radar", change(START + offset * 86_400 + 1, g0=offset))
    days.flush(now=START + 3 * 86_400)
    for offset in range(3):
        days.day("radar", DAY + datetime.timedelta(days=offset))
    assert days.cached_days() <= 1


def test_guess_run_continues_across_midnight(tmp_path):
    days = store(tmp_path)
    days.append("radar", change(NEXT - 10, g0=5))
    days.append("radar", dayfile.Guess(NEXT - 10, "present", 18, 1))
    days.flush(now=NEXT + 30)
    assert days.day("radar", DAY).guesses == [(NEXT - 10, NEXT, "present", 18, 1)]
    today = days.day("radar", DAY + datetime.timedelta(days=1), until=NEXT + 30)
    assert today.guesses == [(NEXT, NEXT + 30, "present", 18, 1)]


def test_home_assistant_down_over_midnight_ends_coverage_at_last_record(tmp_path):
    days = store(tmp_path)
    days.append("radar", change(START + 100, g0=5))
    days.flush(now=START + 101)
    again = daystore.DayStore(tmp_path / "history")
    again.open(now=NEXT + 3600)
    assert (again.folder("radar") / "2025-10-15.day").exists()
    assert list(again.day("radar", DAY).ends) == [START + 100]


def test_record_before_the_day_being_recorded_is_refused(tmp_path):
    days = store(tmp_path)
    days.append("radar", change(NEXT + 1, g0=5))
    with pytest.raises(ValueError):
        days.append("radar", change(NEXT - 1, g0=6))


def test_ended_day_reads_before_it_is_written(tmp_path):
    days = store(tmp_path)
    days.append("radar", change(START + 1, g0=5))
    days.append("radar", change(NEXT + 1, g0=6))
    assert list(days.day("radar", DAY).ends) == [NEXT]


def test_live_day_cannot_be_rewritten_and_empty_store_has_no_size(tmp_path):
    days = store(tmp_path)
    assert days.size() == 0
    days.append("radar", change(START + 1, g0=5))
    with pytest.raises(ValueError):
        days.delete("radar", [DAY])
    with pytest.raises(ValueError):
        days.replace("radar", DAY, days.day("radar", DAY, until=START + 2))
    days.flush(now=NEXT + 1)
    days.day("radar", DAY)
    days.remove_device("radar")
    assert days.cached_days() == 0


def test_a_day_without_any_coverage_gets_no_file(tmp_path):
    days = store(tmp_path)
    days.append("radar", change(START + 1, g0=5))
    days.append("radar", dayfile.End(START + 2))
    days.flush(now=NEXT + 86_400 + 5)
    assert days.days("radar") == [DAY, DAY + datetime.timedelta(days=2)]
    assert sorted(path.name for path in days.folder("radar").iterdir()) == ["2025-10-15.day"]


def test_mark_changes_with_every_record_and_finished_day_change(tmp_path):
    days = store(tmp_path)
    marks = [days.mark("radar")]
    days.append("radar", change(START + 1, g0=5))
    marks.append(days.mark("radar"))
    days.append("radar", change(START + 2, g0=5))  # Same vector: still a new record time.
    marks.append(days.mark("radar"))
    days.flush(now=NEXT + 1)
    marks.append(days.mark("radar"))
    days.delete("radar", [DAY])
    marks.append(days.mark("radar"))
    assert len(set(marks)) == len(marks)
    assert days.mark("radar") == days.mark("radar")


def test_a_read_as_of_a_time_excludes_later_records(tmp_path):
    days = store(tmp_path)
    days.append("radar", change(START + 1, g0=5))
    days.append("radar", dayfile.Guess(START + 1, "present", 18, 1))
    days.append("radar", change(START + 9, g0=6))
    days.append("radar", dayfile.Guess(START + 9, "not_present", 18, 1))
    frozen = days.day("radar", DAY, until=START + 5)
    assert list(frozen.times) == [START + 1] and list(frozen.ends) == [START + 5]
    assert frozen.guesses == [(START + 1, START + 5, "present", 18, 1)]


def test_a_read_without_a_time_never_ends_before_the_last_record(tmp_path, monkeypatch):
    days = store(tmp_path)
    monkeypatch.setattr(daystore.time, "time", lambda: START + 2)
    days.append("radar", change(START + 1, g0=5))
    days.append("radar", change(START + 9, g0=6))
    assert list(days.day("radar", DAY).ends) == [START + 9, START + 9]


def test_staged_trim_is_invisible_until_commit_and_discard_is_safe(tmp_path):
    days = store(tmp_path)
    days.replace(
        "radar",
        DAY,
        dayfile.day_from_log([change(START, g0=10), dayfile.End(START + 100)], None, START, NEXT),
    )
    original = days.size()
    updated = dayfile.day_from_log(
        [change(START, g0=10), dayfile.End(START + 50)], None, START, NEXT
    )
    staged = days.stage([("radar", DAY, updated)])
    assert days.size() == original
    assert days.days("radar") == [DAY]
    assert days.day("radar", DAY).ends[-1] == START + 100
    days.discard(staged)
    assert not list(days.folder("radar").glob("*.trim"))
    staged = days.stage([("radar", DAY, updated)])
    days.commit(staged)
    assert days.day("radar", DAY).ends[-1] == START + 50
    assert not list(days.folder("radar").glob("*.trim"))


def test_restart_discards_unpublished_trim_and_failed_stage_leaves_no_temporary(tmp_path):
    days = store(tmp_path)
    day = dayfile.day_from_log([change(START, g0=10), dayfile.End(START + 100)], None, START, NEXT)
    days.replace("radar", DAY, day)
    days.stage([("radar", DAY, day)])
    recovered = store(tmp_path)
    assert recovered.day("radar", DAY).ends[-1] == START + 100
    assert not list(days.folder("radar").glob("*.trim"))
    days.append("live", change(START, g0=1))
    with pytest.raises(ValueError, match="cannot be rewritten"):
        days.stage([("radar", DAY, day), ("live", DAY, day)])
    assert not list(days.folder("radar").glob("*.trim"))


def test_truncated_live_log_is_repaired_before_appending_after_restart(tmp_path):
    days = store(tmp_path)
    records = [change(START + 1, g0=10), change(START + 2, g0=20)]
    days.append("radar", records[0])
    days.append("radar", records[1])
    days.flush(now=START + 3)
    log = days.folder("radar") / "2025-10-15.log"
    log.write_bytes(log.read_bytes()[:-1])
    recovered = store(tmp_path)
    assert dayfile.decode_log(log.read_bytes(), START) == [records[0], dayfile.End(START + 1)]
    recovered.append("radar", change(START + 4, g0=30))
    recovered.append("radar", dayfile.End(START + 5))
    recovered.flush(now=START + 5)
    again = store(tmp_path)
    day = again.day("radar", DAY, until=START + 10)
    assert first_gate(day) == [10, 30]
    assert list(day.times) == [START + 1, START + 4]
    assert list(day.ends) == [START + 1, START + 5]
    assert dayfile.decode_log(log.read_bytes(), START) == [
        records[0],
        dayfile.End(START + 1),
        change(START + 4, g0=30),
        dayfile.End(START + 5),
    ]


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_interrupted_live_log_repair_preserves_original_and_recovers_next_start(
    tmp_path, monkeypatch, failure
):
    from pathlib import Path

    days = store(tmp_path)
    days.append("radar", change(START + 1, g0=10))
    days.append("radar", change(START + 2, g0=20))
    days.flush(now=START + 3)
    log = days.folder("radar") / "2025-10-15.log"
    log.write_bytes(log.read_bytes()[:-1])
    original = log.read_bytes()
    write, replace = Path.write_bytes, Path.replace

    def fail_write(path, data):
        if path.name.endswith(".log.tmp"):
            write(path, data[:2])
            raise OSError("interrupted recovery write")
        return write(path, data)

    def fail_replace(path, target):
        if path.name.endswith(".log.tmp"):
            raise OSError("interrupted recovery replace")
        return replace(path, target)

    with monkeypatch.context() as edits:
        edits.setattr(
            Path,
            "write_bytes" if failure == "write" else "replace",
            fail_write if failure == "write" else fail_replace,
        )
        with pytest.raises(OSError, match="interrupted recovery"):
            store(tmp_path)
    assert log.read_bytes() == original
    recovered = store(tmp_path)
    assert not list(log.parent.glob("*.tmp"))
    recovered.append("radar", change(START + 4, g0=30))
    recovered.flush(now=START + 5)
    assert first_gate(store(tmp_path).day("radar", DAY, until=START + 10)) == [10, 30]


@pytest.mark.parametrize("closing", [False, True])
def test_clear_during_detached_flush_cannot_restore_deleted_recordings(
    tmp_path, monkeypatch, closing
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    days = store(tmp_path)
    days.append("radar", change(START + 1, g0=77))
    entered, resume = threading.Event(), threading.Event()
    method = "_finish" if closing else "_write_log"
    original = getattr(days, method)

    def delayed(*args):
        entered.set()
        assert resume.wait(5), "flush barrier timed out"
        return original(*args)

    monkeypatch.setattr(days, method, delayed)
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(days.flush, NEXT + 1 if closing else START + 2)
        assert entered.wait(5), "flush never reached detached write"
        try:
            days.remove_device("radar")
            cleared_mark, cleared_rewrites = days.mark("radar"), days.rewrites("radar")
        finally:
            resume.set()
        future.result(timeout=5)
    assert days.mark("radar") == cleared_mark
    assert days.rewrites("radar") == cleared_rewrites
    assert days.cached_days() == 0
    assert days.days("radar") == []
    assert not days.folder("radar").exists()
    assert store(tmp_path).day("radar", DAY, until=START + 10) is None


@pytest.mark.parametrize("closing", [False, True])
def test_stale_flush_cannot_overwrite_recording_restarted_after_clear(
    tmp_path, monkeypatch, closing
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    days = store(tmp_path)
    days.append("radar", change(START + 1, g0=77))
    entered, resume = threading.Event(), threading.Event()
    method = "_finish" if closing else "_write_log"
    original = getattr(days, method)

    def delayed(*args):
        entered.set()
        assert resume.wait(5)
        return original(*args)

    monkeypatch.setattr(days, method, delayed)
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(days.flush, NEXT + 1 if closing else START + 2)
        assert entered.wait(5)
        try:
            days.remove_device("radar")
            monkeypatch.setattr(days, method, original)
            days.append("radar", change(START + 4, g0=99))
            days.append("radar", dayfile.End(START + 5))
            days.flush(now=START + 5)
            fresh_mark = days.mark("radar")
        finally:
            resume.set()
        future.result(timeout=5)
    assert days.mark("radar") == fresh_mark
    day = store(tmp_path).day("radar", DAY, until=START + 10)
    assert first_gate(day) == [99]
    assert list(day.times) == [START + 4] and list(day.ends) == [START + 5]


def test_clear_never_waits_for_detached_day_encoding(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    days = store(tmp_path)
    days.append("radar", change(START + 1, g0=77))
    entered, resume = threading.Event(), threading.Event()
    original = dayfile.encode_day

    def delayed(day):
        entered.set()
        assert resume.wait(5)
        return original(day)

    monkeypatch.setattr(dayfile, "encode_day", delayed)
    with ThreadPoolExecutor(max_workers=2) as workers:
        flush = workers.submit(days.flush, NEXT + 1)
        assert entered.wait(5)
        try:
            workers.submit(days.remove_device, "radar").result(timeout=1)
        finally:
            resume.set()
        flush.result(timeout=5)
    assert days.days("radar") == [] and days.cached_days() == 0
    assert not list(days.root.glob("*.tmp"))


def test_startup_removes_interrupted_root_day_publication_only(tmp_path):
    days = store(tmp_path)
    days.root.mkdir(parents=True)
    abandoned = days.root / "abandoned.tmp"
    abandoned.write_bytes(b"partial day encoding")
    staged_trim = days.root / "pending.trim"
    staged_trim.write_bytes(b"unrelated staged trim")
    recovered = store(tmp_path)
    assert not abandoned.exists()
    assert staged_trim.read_bytes() == b"unrelated staged trim"
    assert recovered.days("radar") == []


def test_a_window_inside_the_live_day_needs_no_folder_listing(tmp_path, monkeypatch):
    """The poll asks for the last few seconds every tick: blocking disk I/O on the event loop."""
    from pathlib import Path
    from types import SimpleNamespace

    from tuner_under_test.history import spans

    store = daystore.DayStore(tmp_path / "history")
    store.open(now=START)
    store.append("radar", dayfile.Change(START + 10, {0: 5}))
    store.append("radar", dayfile.Change(START + 86_400 + 10, {0: 6}))
    store.flush(now=START + 86_400 + 15)  # Yesterday finished: the folder has files.
    view = SimpleNamespace(_days=store)
    listed = spans._store_days(view, "radar", START + 86_400 + 20, START + 86_400 + 25)
    monkeypatch.setattr(Path, "iterdir", lambda self: (_ for _ in ()).throw(AssertionError))
    assert spans._store_days(view, "radar", START + 86_400 + 20, START + 86_400 + 25) == listed
    assert listed == [daystore.date_of(START + 86_400)]
