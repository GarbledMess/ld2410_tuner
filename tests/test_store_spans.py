"""The physical span reader over the day store."""

from types import SimpleNamespace

import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile, daystore, spans

START = 1_760_486_400.0  # 2025-10-15T00:00:00Z
NEXT = START + 86_400


def view(tmp_path, records, until=None):
    days = daystore.DayStore(tmp_path / "history")
    days.open(now=START)
    for record in records:
        days.append("radar", record)
    days.flush(now=max(record.time for record in records) + 1)
    found = SimpleNamespace(data={"devices": {"radar": {}}}, _days=days)
    if until is not None:
        found._history_until = until
    return found


def change(at, value):
    return dayfile.Change(at, {0: value})


def triples(items):
    return [(span.start, span.end, span.values[0]) for span in items]


def test_store_spans_are_clipped_to_the_window_and_keep_the_held_value(tmp_path):
    source = view(tmp_path, [change(START + 10, 5), change(START + 20, 6)], until=START + 30)
    assert triples(spans.events(source, "radar", start=START + 15, end=START + 25)) == [
        (START + 15, START + 20, 5),
        (START + 20, START + 25, 6),
    ]
    assert spans.has_events(source, "radar", start=START, end=START + 5)
    assert not spans.has_events(source, "radar", start=NEXT, end=NEXT + 5)


def test_coverage_across_midnight_has_no_gap_or_duplicate(tmp_path):
    source = view(tmp_path, [change(NEXT - 5, 7), change(NEXT + 5, 8)], until=NEXT + 9)
    items = triples(spans.events(source, "radar", start=NEXT - 9, end=NEXT + 9))
    assert items == [(NEXT - 5, NEXT, 7), (NEXT, NEXT + 5, 7), (NEXT + 5, NEXT + 9, 8)]
    assert spans.coverage(spans.events(source, "radar")) == [(NEXT - 5, NEXT + 9)]


def test_ended_coverage_is_never_bridged(tmp_path):
    source = view(
        tmp_path,
        [change(START + 1, 5), dayfile.End(START + 3), change(START + 9, 5)],
        until=START + 10,
    )
    assert spans.coverage(spans.events(source, "radar")) == [
        (START + 1, START + 3),
        (START + 9, START + 10),
    ]


def test_live_day_is_read_up_to_the_view_time(tmp_path):
    source = view(tmp_path, [change(START + 1, 5)], until=START + 4)
    assert triples(spans.events(source, "radar")) == [(START + 1, START + 4, 5)]


def test_radar_without_a_store_or_history_reads_nothing(tmp_path):
    empty = SimpleNamespace(data={"devices": {"radar": {}}}, _days=None)
    assert list(spans.events(empty, "radar")) == []
    assert not spans.has_events(empty, "radar")
    source = view(tmp_path, [change(START + 1, 5)], until=START + 2)
    assert list(spans.events(source, "another")) == []
