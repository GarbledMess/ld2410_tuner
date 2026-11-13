"""Live-day reads copy only the rows their window needs, and read exactly what a whole-day
copy would.

The poll read the last 5 s of a radar by copying its whole live day (every row and gate column
recorded since midnight) on the event loop; late on a busy day that was millions of bytes per
radar every few seconds.
"""

import random
import time

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile, spans
from tuner_under_test.history.daystore import DayStore, bounds, date_of


def _recorded(seed, tmp_path, rows=400):
    """A live day with changes, coverage ends, guess runs and an open last row."""
    rng = random.Random(seed)
    store = DayStore(tmp_path)
    start = bounds(date_of(time.time()))[0] + 60
    moment = start
    for _ in range(rows):
        moment += rng.choice((0.04, 0.5, 2.0, 7.5))
        roll = rng.random()
        if roll < 0.05:
            store.append("r", dayfile.End(moment))
        elif roll < 0.15:
            state = rng.choice(("present", "not_present", None))
            store.append("r", dayfile.Guess(moment, state, rng.randint(0, 100), 0))
        else:
            gates = rng.sample(range(18), rng.randint(1, 4))
            store.append("r", dayfile.Change(moment, {g: rng.randint(0, 100) for g in gates}))
    store.observe("r", moment + 3)
    return store, start, moment + 3


@pytest.mark.parametrize("seed", range(12))
def test_a_windowed_live_view_reads_exactly_what_the_whole_view_reads(seed, tmp_path):
    store, start, now = _recorded(seed, tmp_path)
    builder = store._live["r"].builder
    rng = random.Random(seed)
    for _ in range(25):
        low = rng.uniform(start - 5, now + 5)
        high = low + rng.choice((0.01, 1, 5, 60, 3600))
        until = rng.choice((now, rng.uniform(low, now + 5)))
        whole = builder.view(until)
        windowed = builder.view(until, rows_since=low)
        assert list(spans.day_spans(windowed, low, high)) == list(spans.day_spans(whole, low, high))
        assert spans._column_bounds(windowed, low, high) == spans._column_bounds(whole, low, high)
        assert windowed.guesses == whole.guesses
        assert len(windowed.times) <= len(whole.times)


def test_a_recent_window_copies_only_its_rows_and_never_feeds_whole_day_summaries(tmp_path):
    store, _start, now = _recorded(1, tmp_path, rows=3000)
    builder = store._live["r"].builder
    windowed = builder.view(now, rows_since=now - 5)
    assert len(windowed.times) <= 5
    assert not hasattr(windowed, "_summary_owner")
    assert builder.view(now)._summary_owner is builder


def test_no_rows_still_gives_every_guess_run(tmp_path):
    store, _start, now = _recorded(2, tmp_path)
    builder = store._live["r"].builder
    assert builder.view(now, rows_since=float("inf")).guesses == builder.view(now).guesses
    date = date_of(now)
    assert store.guess_runs("r", date, until=now) == list(builder.view(now).guesses)


def test_the_poll_reads_its_seconds_from_a_store_window(tmp_path):
    store, _start, now = _recorded(3, tmp_path, rows=3000)
    view = type("View", (), {"_days": store})()
    seen = []
    day = store.day

    def recorded_day(*args, **kwargs):
        found = day(*args, **kwargs)
        seen.append(len(found.times))
        return found

    store.day = recorded_day
    found = list(spans.events(view, "r", start=now - 5, end=now))
    store.day = day
    whole = list(spans.day_spans(store.day("r", date_of(now), until=now), now - 5, now))
    assert found == whole
    assert max(seen) <= 5


def test_a_finished_day_with_nothing_late_is_read_without_rebuilding_its_runs(tmp_path):
    store, _start, now = _recorded(4, tmp_path)
    day = store._live["r"].builder.day()

    class Untouchable(list):
        def __iter__(self):
            raise AssertionError("runs rebuilt for an empty splice")

    bare = dayfile.Day(day.start, day.end, day.times, day.ends, day.columns, Untouchable())
    assert dayfile.splice(bare, []) is bare
