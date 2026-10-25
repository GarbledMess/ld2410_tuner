"""Exact synthetic characterization of the reference rebuild's indexed computation."""

from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile, summaries
from tuner_under_test.history.label_lookup import index_reader
from tuner_under_test.presence import reference_training as training
from tuner_under_test.presence import references


def legacy_fold(view, own, room, automatic, profiles, cutoff):
    """Original indexed algorithm, including its order and floating point arithmetic."""
    from tuner_under_test.history import spans

    native = {source: references.SpanFold(profile) for source, profile in profiles.items()}
    edges = training._native_edges([r for r in (own, room, automatic) if r is not None])
    for day in spans._store_days(view, "a", cutoff, view._history_until):
        summary = summaries.of(view, "a", day)
        if summary is not None:
            legacy_summary(
                summary, cutoff, view._history_until, edges, (own, room, automatic), native
            )
    for fold in native.values():
        fold.close()


def synthetic(case):
    events = []
    for i in range(100):
        start = i * 1800 + (0.125 if case == "fractional" else 0)
        events.extend(
            [
                dayfile.Change(start, {0: 10.25 + i % 9, 1: i % 101}),
                dayfile.Change(start + 0.375, {0: None}),
                dayfile.End(start + (1.125 if case == "sparse" else 1799.875)),
            ]
        )
    days = {}
    for index in range(3):
        low = index * 86400
        days[date(1970, 1, index + 1)] = dayfile.day_from_log(events, None, low, low + 86400)
    view = SimpleNamespace(
        _days=SimpleNamespace(
            days=lambda key: list(days),
            day=lambda key, day, **kw: days[day],
            live_date=lambda key: None,
        ),
        _history_until=180000,
    )
    states = ["present" if case == "one_class" or i % 3 else "not_present" for i in range(100)]
    if case == "multi_source":
        states = [
            "unknown" if i % 7 == 0 else None if i % 3 else state for i, state in enumerate(states)
        ]
    own = index_reader(([i * 1800 for i in range(100)], states))
    room = index_reader(
        ([0, 72000, 73000], [("present", "automatic", 0.8), None, ("not_present", "estimate", 0.6)])
    )
    return view, own, room


@pytest.mark.parametrize("case", ["dense", "sparse", "one_class", "fractional", "multi_source"])
@pytest.mark.parametrize("cutoff", [0, 0.25, 1799.75, 90000])
def test_rebuild_encoded_profiles_equal_original_indexed_algorithm(case, cutoff):
    view, own, room = synthetic(case)
    expected = {source: references.empty() for source in ("human", "entity", "radar")}
    actual = deepcopy(expected)
    legacy_fold(view, own, room, room, expected, cutoff)
    training._fold_native(view, "a", own, room, room, actual, cutoff)
    assert actual == expected


def test_batch_histograms_equal_indexed_arithmetic():
    from tuner_under_test.presence.reference_summary import BatchSummary

    view, _own, _room = synthetic("fractional")
    day = view._days.day("a", date(1970, 1, 1))
    windows = [
        (0, 0),
        (0.25, 1799.75),
        (1800.25, 1800.75),
        (11, 21),
        (0, 86400),
        (90000, 91000),
        (3600, 7200),
    ]
    expected = summaries.Summary(day)
    actual = BatchSummary(day)
    assert actual.histograms_many(windows) == [expected.histograms(*w) for w in windows]


def test_rebuild_encodes_only_surviving_nonempty_histograms():
    from unittest.mock import patch

    view, own, _room = synthetic("one_class")
    result = {"human": references.empty()}
    with patch.object(references, "encode", wraps=references.encode) as encode:
        training._fold_native(view, "a", own, None, None, result, 0)
    assert sum(bool(call.args[0]) for call in encode.call_args_list) == references.MAX_PERIODS


def legacy_summary(summary, cutoff, end, edges, readers, native):
    from tuner_under_test.history.spans import Span

    for start, stop in summary.coverage_ranges(cutoff, end):
        cell = Span(start, stop, (), "event")
        for low, high in training._native_pieces(cell, edges):
            found = training._span_evidence(*readers, low)
            if found:
                legacy_piece(summary, low, high, found, native)


def legacy_piece(summary, start, end, found, native):
    from math import floor

    while start < end:
        stop = min(end, (floor(start / references.PERIOD_SECONDS) + 1) * references.PERIOD_SECONDS)
        histograms = summary.histograms(start, stop)
        for state, source, confidence in found:
            native[source].add_histograms(start, stop, histograms, state, source, confidence)
        start = stop


def test_batch_irregular_legacy_columns_match_every_encoded_bin_exactly():
    import random

    from tuner_under_test.presence.reference_summary import BatchSummary

    randomizer = random.Random(2410)
    events = [
        dayfile.Change(
            i * 0.001 + (i % 3) * 0.0001, {0: 10.25 + i % 20, 1: None if i % 7 else 5.125}
        )
        for i in range(20000)
    ] + [dayfile.End(20)]
    day = dayfile.day_from_log(events, None, 0, 86400)
    windows = [
        tuple(sorted((randomizer.uniform(-1, 21), randomizer.uniform(-1, 21)))) for _ in range(100)
    ]
    old = summaries.Summary(day)
    new = BatchSummary(day)
    assert new.histograms_many(windows) == [old.histograms(*w) for w in windows]


def test_batch_rebuild_preserves_existing_period_continuation_and_overlap():
    view, own, room = synthetic("fractional")
    expected = {source: references.empty() for source in ("human", "entity", "radar")}
    for source, profile in expected.items():
        fold = references.SpanFold(profile)
        fold.add(0, 0.25, {"g0_move": 11}, "present", source, 0.9)
        fold.close()
    actual = deepcopy(expected)
    legacy_fold(view, own, room, room, expected, 0.25)
    training._fold_native(view, "a", own, room, room, actual, 0.25)
    assert actual == expected
