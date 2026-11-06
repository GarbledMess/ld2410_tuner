"""A day's attribution part read from columns: identical to the span walk, without its work."""

import datetime
import random
import re
from array import array
from bisect import bisect_right
from itertools import accumulate
from math import ceil, inf
from operator import neg
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.calibration.constants import ATTRIBUTION_SUSTAIN_SECONDS
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history import dayfile, spans
from tuner_under_test.rooms import attribution, attribution_days, event_attribution

FLOOR = attribution_days.FLOOR
LEVELS = attribution_days.LEVELS
LEGACY_AT_OR_ABOVE = [re.compile(rb"[\x%02x-\xff]+" % level) for level in range(LEVELS)]
DAY = 1_790_035_200
DATE = datetime.datetime.fromtimestamp(DAY, datetime.UTC).date()


# The span walk this replaced, kept verbatim as the reference --------------------------------


def legacy_places(cells, edges):
    places = []
    for low, _high, _values in cells:
        index = bisect_right(edges, low)
        places.append((index, index > 0 and edges[index - 1] == low))
    return places


def legacy_read_cells(read, cells, places):
    answers, last, answer = [], None, None
    for (low, _high, _values), place in zip(cells, places, strict=True):
        if place != last:
            answer, last = read(low), place
        answers.append(answer)
    return answers


def legacy_levels(column):
    try:
        return bytes(column)
    except (TypeError, ValueError):
        return bytes(0 if value is None else max(0, ceil(value)) for value in column)


def legacy_minima(levels, ends, position, starts=None):
    running = list(accumulate(levels, min))
    found, index = [], 0
    while index < len(running) and (level := running[index]) > FLOOR:
        index = bisect_right(running, -level, key=neg)
        last = position(index - 1)
        found.append((level, ends[last] if starts is None else starts[last]))
    return tuple(found)


def legacy_sustained(levels, starts, ends, level):
    for match in LEGACY_AT_OR_ABOVE[level].finditer(levels):
        if ends[match.end() - 1] - starts[match.start()] >= ATTRIBUTION_SUSTAIN_SECONDS:
            return True
    return False


def legacy_threshold(levels, starts, ends):
    low, high = FLOOR + 1, LEVELS - 1
    best = -1
    while low <= high:
        level = (low + high) // 2
        if legacy_sustained(levels, starts, ends, level):
            best, low = level - 1, level + 1
        else:
            high = level - 1
    return best


def legacy_gate(levels, starts, ends):
    leading = legacy_minima(levels, ends, lambda index: index)
    trailing = legacy_minima(levels[::-1], ends, lambda index: len(levels) - 1 - index, starts)
    return min(levels), leading, trailing, legacy_threshold(levels, starts, ends)


def legacy_run(cells, owned):
    starts, ends = [cell[0] for cell in cells], [cell[1] for cell in cells]
    columns = zip(*(cell[2] for cell in cells), strict=True)
    gates = [legacy_gate(legacy_levels(column), starts, ends) for column in columns]
    packed = attribution_days._packed(gates)
    return cells[0][0], cells[-1][1], "present" in owned, "not_present" in owned, packed


def legacy_measure(runtime, device_id, low, high, edges, reads, own):
    physical = spans.events(runtime, device_id, start=low, end=high)
    cells = list(event_attribution._cells(physical, edges))
    places = legacy_places(cells, edges)
    quiet = legacy_read_cells(attribution.quiet_reader(own, reads), cells, places)
    bins = [[0.0] * 101 for _ in HISTORY_KEYS]
    for (start, end, values), still in zip(cells, quiet, strict=True):
        if still:
            event_attribution._add_readings(bins, values, end - start)
    owned = legacy_read_cells(own, cells, places)
    runs = tuple(
        tuple(
            legacy_run(cells[first:stop], owned[first:stop])
            for first, stop in event_attribution._episodes(
                cells, legacy_read_cells(read, cells, places)
            )
        )
        for read in reads.values()
    )
    kept = {gate: array("d", histogram) for gate, histogram in enumerate(bins) if any(histogram)}
    return kept, runs


# A small day with everything that can vary --------------------------------------------------


class Days:
    def __init__(self, day):
        self.found = day

    def days(self, _device_id):
        return [DATE]

    def live_date(self, _device_id):
        return None

    def day(self, _device_id, _date, until=None):
        return self.found


def _column(rng, rows, *, floats, missing):
    if floats:
        values = [
            float("nan") if rng.random() < missing else rng.randrange(0, 10000) / 100
            for _ in range(rows)
        ]
        return array("f", values)
    levels = rng.choice((30, 60, 100))
    return bytes(255 if rng.random() < missing else rng.randrange(0, levels) for _ in range(rows))


def _rows(rng, rows):
    times, ends, at = array("d"), array("d"), DAY + rng.choice((0, 3.5))
    for _ in range(rows):
        at += rng.choice((0, 0, 0, 0, 0, 0, 2.25, 40))  # Mostly contiguous, some gaps.
        times.append(at)
        at += rng.choice((1, 1.5, 6, 6, 0.125, 31, 0)) + rng.randrange(0, 3) / 1000
        ends.append(at)
    return times, ends


def _day(rng, rows):
    times, ends = _rows(rng, rows)
    missing = rng.choice((0, 0, 0.1, 1))
    columns = []
    for gate in range(18):
        share = 1 if missing == 1 and gate else missing
        columns.append(_column(rng, rows, floats=rng.random() < 0.2, missing=share))
    if missing == 1:
        columns[0] = bytes(255 if rng.random() < 0.3 else 90 for _ in range(rows))
    return dayfile.Day(DAY, DAY + 86400, times, ends, columns, [])


def _edges(rng, times, ends):
    edges = set()
    for _ in range(rng.choice((0, 3, 40))):
        at = rng.randrange(len(times))
        edges.add(
            rng.choice(
                (
                    times[at],
                    ends[at],
                    (times[at] + ends[at]) / 2,
                    times[at] + (ends[at] - times[at]) / 3,
                    ends[at] + 0.5,
                )
            )
        )
    edges.update((DAY - 500.0, DAY + 90000.0))
    return sorted(edges)


def _labels(rng, edges, states):
    """A reader over ``edges`` (as readers here are: it can change only at them)."""
    answers = [rng.choice(states) for _ in range(len(edges) + 1)]

    def read(t):
        return answers[bisect_right(edges, t)]

    read.edges = edges
    return read


TRUTH = (
    None,
    ("present", "human", 1.0),
    ("present", "automatic", 0.7),
    ("not_present", "human", 1.0),
    ("unknown", "human", 1.0),
)


def _case(seed):
    rng = random.Random(seed)
    day = _day(rng, rng.choice((0, 1, 2, 30, 300)))
    edges = _edges(rng, day.times, day.ends) if len(day.times) else [DAY - 500.0]
    own = _labels(rng, edges, (None, None, "present", "not_present", "unknown"))
    reads = {"a": _labels(rng, edges, TRUTH), "b": _labels(rng, edges, TRUTH)}
    view = SimpleNamespace(
        _days=Days(day),
        _physical_history_start=rng.choice((-inf, DAY + 10.25)),
        _attribution_until=rng.choice((inf, day.times[-1] if len(day.times) else inf)),
    )
    return view, edges, reads, own


def _both(view, window, edges, reads, own):
    new = attribution_days._measure(view, "r", *window, edges, reads, own)
    return repr(new), repr(legacy_measure(view, "r", *window, edges, reads, own))


@pytest.mark.parametrize("seed", range(150))
def test_part_is_identical_to_the_span_walk(seed):
    view, edges, reads, own = _case(seed)
    new, old = _both(view, (DAY, DAY + 86400), edges, reads, own)
    assert new == old


def test_cases_reach_every_shape():
    """The seeds above really cover runs, quiet readings, cuts, gaps and unreadable rows."""
    found = {"runs": 0, "quiet": 0, "floats": 0, "gaps": 0, "unreadable": 0, "zero": 0}
    for seed in range(150):
        view, edges, reads, own = _case(seed)
        kept, runs = legacy_measure(view, "r", DAY, DAY + 86400, edges, reads, own)
        day = view._days.found
        found["runs"] += sum(map(len, runs))
        found["quiet"] += len(kept)
        found["floats"] += any(not isinstance(column, bytes) for column in day.columns)
        found["gaps"] += any(a != b for a, b in zip(day.ends, day.times[1:], strict=False))
        found["zero"] += any(a == b for a, b in zip(day.times, day.ends, strict=True))
        found["unreadable"] += isinstance(day.columns[1], (bytes, bytearray)) and any(
            all(
                (column[row] == 255) if isinstance(column, bytes) else column[row] != column[row]
                for column in day.columns
            )
            for row in range(len(day.times))
        )
    assert all(found.values()), found


def test_cut_day_part_is_identical_to_the_span_walk():
    for seed in range(40):
        view, edges, reads, own = _case(seed)
        new, old = _both(view, (DAY + 20.5, DAY + 400.25), edges, reads, own)
        assert new == old


def test_learning_windows_part_is_identical_to_the_span_walk():
    for seed in range(40):
        view, edges, reads, own = _case(seed)
        view._learning_windows = {"r": [(DAY + 5.5, DAY + 60), (DAY + 90, DAY + 300)]}
        new, old = _both(view, (DAY, DAY + 86400), edges, reads, own)
        assert new == old


def test_overlapping_coverage_is_still_refused():
    times, ends = array("d", (DAY, DAY + 3.0)), array("d", (DAY + 6.0, DAY + 9.0))
    columns = [bytes((40, 50)) for _ in range(18)]
    day = dayfile.Day(DAY, DAY + 86400, times, ends, columns, [])
    view = SimpleNamespace(_days=Days(day))
    read = _labels(random.Random(1), [DAY - 1.0], (TRUTH[1],))
    with pytest.raises(ValueError, match="overlapping coverage"):
        legacy_measure(view, "r", DAY, DAY + 86400, [DAY - 1.0], {"a": read}, read)
    with pytest.raises(ValueError, match="overlapping coverage"):
        attribution_days._measure(view, "r", DAY, DAY + 86400, [DAY - 1.0], {"a": read}, read)


def _busy_day(rows=7200):
    times = array("d", (DAY + 6.0 * at for at in range(rows)))
    ends = array("d", (DAY + 6.0 * (at + 1) for at in range(rows)))
    columns = [bytes((at * 7 + gate) % 60 + 10 for at in range(rows)) for gate in range(18)]
    return dayfile.Day(DAY, DAY + 86400, times, ends, columns, [])


def _busy_view():
    edges = [DAY + 500.25 + 600.5 * at for at in range(60)]
    answers = [(TRUTH[1], TRUTH[3])[at % 2] for at in range(len(edges) + 1)]
    calls = [0]

    def read(t):
        calls[0] += 1
        return answers[bisect_right(edges, t)]

    read.edges = edges

    def own(_t):
        calls[0] += 1

    own.edges = []
    return SimpleNamespace(_days=Days(_busy_day())), edges, read, own, calls


def test_part_makes_no_python_object_per_row(monkeypatch):
    view, edges, read, own, _calls = _busy_view()
    made = []
    original = spans.Span

    def counted(*args):
        made.append(args)
        return original(*args)

    monkeypatch.setattr(spans, "Span", counted)
    attribution_days._measure(view, "r", DAY, DAY + 86400, edges, {"a": read}, own)
    assert made == []


def test_part_reads_truth_per_stretch_not_per_row():
    view, edges, read, own, calls = _busy_view()
    attribution_days._measure(view, "r", DAY, DAY + 86400, edges, {"a": read}, own)
    # Own, truth and the quiet reader (itself own then truth), over two places an edge.
    assert calls[0] <= 4 * 2 * (len(edges) + 1)


def test_rows_above_a_ceiling_are_found_without_a_running_minimum(monkeypatch):
    """A run's minima take one search per level reached, not one comparison per reading."""
    view, edges, read, own, _calls = _busy_view()
    monkeypatch.setattr(
        attribution_days,
        "accumulate",
        lambda *_args: pytest.fail("Per-reading minimum"),
        raising=False,
    )
    attribution_days._measure(view, "r", DAY, DAY + 86400, edges, {"a": read}, own)


def test_day_without_present_or_quiet_truth_is_empty():
    view, edges, _read, own, _calls = _busy_view()

    def nothing(_t):
        return None

    nothing.edges = edges
    found = attribution_days._measure(view, "r", DAY, DAY + 86400, edges, {"a": nothing}, own)
    assert found == ({}, ((),))
