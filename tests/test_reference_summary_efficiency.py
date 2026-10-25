"""Aggregate reference rebuilds preserve physical replay without replaying rows."""

from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile, spans
from tuner_under_test.history.label_lookup import index_reader
from tuner_under_test.presence import reference_training as training
from tuner_under_test.presence import references


def fixture():
    events = [
        dayfile.Change(1798, {0: 10.125, 1: 8}),
        dayfile.Change(1799.25, {0: 22.25}),
        dayfile.Change(1800.5, {0: None}),
        dayfile.End(1802),
        dayfile.Change(1803, {0: 9.25}),
        dayfile.Change(1804, {0: None, 1: None}),
        dayfile.Change(1805, {1: 30.25}),
        dayfile.End(1810),
    ]
    day = dayfile.day_from_log(events, None, 0, 86400)
    store = SimpleNamespace(
        days=lambda key: [date(1970, 1, 1)], day=lambda *a, **kw: day, live_date=lambda *a: None
    )
    view = SimpleNamespace(_days=store, _history_until=1810)
    own = index_reader(
        ([1798, 1799.75, 1800.25, 1803.5, 1806], ["present", "unknown", None, "not_present", None])
    )
    room = index_reader(
        (
            [1798, 1803, 1807],
            [("present", "estimate", 0.6), ("not_present", "automatic", 0.9), None],
        )
    )
    automatic = index_reader(
        (
            [1798, 1801, 1808],
            [("present", "estimate", 0.65), ("not_present", "automatic", 0.8), None],
        )
    )
    return view, own, room, automatic


def profiles():
    return {source: references.empty() for source in ("human", "entity", "radar")}


def replay(view, own, room, automatic, result, cutoff):
    folds = {source: references.SpanFold(profile) for source, profile in result.items()}
    edges = training._native_edges([own, room, automatic])
    for cell in spans.events(view, "a", start=cutoff):
        for low, high in training._native_pieces(cell, edges):
            for state, source, confidence in training._span_evidence(own, room, automatic, low):
                folds[source].add(
                    low, high, training._native_values(cell.values), state, source, confidence
                )
    for fold in folds.values():
        fold.close()


def decoded(result):
    result = deepcopy(result)
    for profile in result.values():
        for period in profile["periods"]:
            period["data"] = references.decode(period["data"])
    return result


@pytest.mark.parametrize("cutoff", [1798, 1799.5, 1800, 1804.5, 1809, 1811])
def test_summary_fold_matches_span_replay_for_edges_gaps_missing_and_sources(cutoff):
    view, own, room, automatic = fixture()
    expected, actual = profiles(), profiles()
    replay(view, own, room, automatic, expected, cutoff)
    training._fold_native(view, "a", own, room, automatic, actual, cutoff)
    assert decoded(actual) == decoded(expected)


def test_summary_fold_never_replays_full_vectors():
    view, own, room, automatic = fixture()
    expected, actual = profiles(), profiles()
    replay(view, own, room, automatic, expected, 1798)
    with patch.object(spans, "events", side_effect=AssertionError("reference row replay")):
        training._fold_native(view, "a", own, room, automatic, actual, 1798)
    assert decoded(actual) == decoded(expected)


@pytest.mark.parametrize("bounds", [(float("nan"), 1), (0, float("inf")), (2, 1), (1799, 1801)])
def test_aggregate_rejects_invalid_or_cross_period_windows(bounds):
    fold = references.SpanFold(references.empty())
    with pytest.raises(ValueError):
        fold.add_histograms(*bounds, [[1.0] * 101] * 18, "present", "human", 1)


@pytest.mark.parametrize(
    "state,source,confidence",
    [
        ("unknown", "human", 1),
        ("present", "other", 1),
        ("present", "human", 0),
        ("present", "human", float("nan")),
    ],
)
def test_aggregate_rejects_unadmitted_evidence(state, source, confidence):
    result = references.empty()
    fold = references.SpanFold(result)
    fold.add_histograms(0, 1, [[1.0] * 101] * 18, state, source, confidence)
    fold.close()
    assert result == references.empty()


def test_empty_aggregate_does_not_create_reference_period():
    result = references.empty()
    fold = references.SpanFold(result)
    fold.add_histograms(0, 1, [[0.0] * 101] * 18, "present", "human", 1)
    fold.close()
    assert result == references.empty()


def test_aggregate_continues_existing_period_and_respects_profile_limits():
    expected, actual = profiles(), profiles()
    for result in (expected, actual):
        fold = references.SpanFold(result["human"])
        fold.add(1797, 1798, {"g0_move": 11}, "present", "human", 0.5)
        fold.close()
    view, own, room, automatic = fixture()
    replay(view, own, room, automatic, expected, 1798)
    training._fold_native(view, "a", own, room, automatic, actual, 1798)
    assert decoded(actual) == decoded(expected)


def test_irregular_millisecond_histograms_have_bounded_rounding_only():
    events = [
        dayfile.Change(i * 0.001 + (i % 3) * 0.0001, {0: 10 + i % 20, 1: 3 + i % 4})
        for i in range(20000)
    ] + [dayfile.End(20)]
    day = dayfile.day_from_log(events, None, 0, 86400)
    view = SimpleNamespace(
        _days=SimpleNamespace(
            days=lambda key: [date(1970, 1, 1)], day=lambda *a, **kw: day, live_date=lambda *a: None
        ),
        _history_until=20,
    )
    own = index_reader(([0], ["present"]))
    expected, actual = profiles(), profiles()
    replay(view, own, None, None, expected, 0)
    training._fold_native(view, "a", own, None, None, actual, 0)
    expected, actual = decoded(expected), decoded(actual)
    for source in expected:
        assert len(actual[source]["periods"]) == len(expected[source]["periods"])
        for before, after in zip(
            expected[source]["periods"], actual[source]["periods"], strict=True
        ):
            old_bins, new_bins = before.pop("data"), after.pop("data")
            assert before == after
            assert old_bins.keys() == new_bins.keys()
            for gate in old_bins:
                assert new_bins[gate] == pytest.approx(old_bins[gate], rel=0, abs=1e-3)
                assert {k: round(v, 3) for k, v in new_bins[gate].items()} == {
                    k: round(v, 3) for k, v in old_bins[gate].items()
                }


@pytest.mark.parametrize("state", ["present", "not_present", "unknown"])
def test_fully_labelled_rebuild_never_prepares_unused_room_targets(state):
    view, _own, _room, _automatic = fixture()
    device = {"history_labels": [{"start": 1798, "end": 1810, "state": state}]}
    view.data = {"devices": {"a": device}}
    expected = profiles()
    from tuner_under_test.history.label_lookup import _history_label_reader

    replay(view, _history_label_reader(device), None, None, expected, 0)
    with (
        patch.object(training.time, "time", return_value=1810),
        patch(
            "tuner_under_test.rooms.attribution.room_targets",
            side_effect=AssertionError("unused room work"),
        ),
        patch(
            "tuner_under_test.rooms.attribution.auto_targets",
            side_effect=AssertionError("unused automatic work"),
        ),
    ):
        training.rebuild_human(view, device)
    assert decoded({"human": device["reference_profile"]}) == decoded({"human": expected["human"]})


@pytest.mark.parametrize("hole", [(1799.25, 1799.250001), (1803, 1803.5)])
def test_any_unlabelled_observed_time_keeps_room_preparation(hole):
    view, _own, _room, _automatic = fixture()
    device = {
        "history_labels": [
            {"start": 1798, "end": hole[0], "state": "present"},
            {"start": hole[1], "end": 1810, "state": "not_present"},
        ]
    }
    view.data = {"devices": {"a": device}}
    with (
        patch.object(training.time, "time", return_value=1810),
        patch("tuner_under_test.rooms.attribution.room_targets", return_value=None) as room,
        patch("tuner_under_test.rooms.attribution.auto_targets", return_value=None) as automatic,
    ):
        training.rebuild_human(view, device)
    room.assert_called_once()
    automatic.assert_called_once()


@pytest.mark.parametrize(
    "end_attribute,end", [("_history_until", 1800), ("_attribution_until", 1800)]
)
def test_own_label_shortcut_ignores_unobserved_future(end_attribute, end):
    view, _own, _room, _automatic = fixture()
    setattr(view, end_attribute, end)
    device = {"history_labels": [{"start": 1798, "end": end, "state": "present"}]}
    view.data = {"devices": {"a": device}}
    from tuner_under_test.history.label_lookup import _history_label_reader

    with patch.object(training.time, "time", return_value=1810):
        assert training._fully_labelled(view, "a", _history_label_reader(device), 1798)


@pytest.mark.parametrize("missing", [True, False])
def test_absent_or_empty_recording_cannot_prove_full_label_coverage(missing):
    view, _own, _room, _automatic = fixture()
    if missing:
        view._days.day = lambda *a, **kw: None
    else:
        empty = dayfile.day_from_log([], None, 0, 86400)
        view._days.day = lambda *a, **kw: empty
    own = index_reader(([0], ["present"]))
    with patch.object(training.time, "time", return_value=1810):
        assert not training._fully_labelled(view, "a", own, 0)


def test_label_holes_in_unrecorded_gaps_do_not_require_room_evidence():
    view, _own, _room, _automatic = fixture()
    own = index_reader(
        (
            [1798, 1802, 1803, 1804, 1805, 1810],
            ["present", None, "unknown", None, "not_present", None],
        )
    )
    with patch.object(training.time, "time", return_value=1810):
        assert training._fully_labelled(view, "a", own, 0)


def test_own_label_shortcut_removes_stale_owned_room_periods():
    view, _own, _room, _automatic = fixture()
    device = {"history_labels": [{"start": 1798, "end": 1810, "state": "present"}]}
    view.data = {"devices": {"a": device}}
    old = references.empty()
    fold = references.SpanFold(old)
    fold.add(1798, 1810, {"g0_move": 10}, "present", "entity", 0.9)
    fold.close()
    old["periods"][0]["room"] = True
    device["reference_profile"] = old
    with (
        patch.object(training.time, "time", return_value=1810),
        patch(
            "tuner_under_test.rooms.attribution.room_targets", side_effect=AssertionError("unused")
        ),
        patch(
            "tuner_under_test.rooms.attribution.auto_targets", side_effect=AssertionError("unused")
        ),
    ):
        training.rebuild_human(view, device)
    assert device["reference_profile"]["periods"]
    assert all(not period.get("room") for period in device["reference_profile"]["periods"])


def test_unfamiliar_channels_are_those_far_from_every_value_seen():
    """The nearest seen value, found by bisection in a kept sorted support, is as far as the
    nearest of all of them: the same channels are unfamiliar."""
    import random
    from unittest.mock import patch

    from tuner_under_test.const import HISTORY_KEYS
    from tuner_under_test.presence import references

    def reference(profile, values):  # Every seen value, every time.
        support = references._support(
            tuple(p["data"] for p in profile.get("periods", []) if p["source"] != "radar")
        )
        return [
            key
            for key, value in values.items()
            if support.get(key) and min(abs(value - seen) for seen in support[key]) > 6
        ]

    rnd = random.Random(8)
    for _ in range(40):
        periods = []
        for source in ("human", "entity", "radar"):
            for _ in range(rnd.randint(0, 4)):
                histograms = {
                    key: {str(v): 1.0 for v in rnd.sample(range(101), rnd.randint(1, 6))}
                    for key in rnd.sample(HISTORY_KEYS, 5)
                }
                periods.append({"source": source, "data": references.encode(histograms)})
        profile = {"periods": periods}
        for _ in range(20):
            values = {key: rnd.choice((0, 6, 7, 50.5, 93, 100, 12.75)) for key in HISTORY_KEYS}
            assert references.unfamiliar(profile, values) == reference(profile, values)
    with patch.object(references, "_support", wraps=references._support) as gathered:
        for _ in range(10):
            references.unfamiliar(profile, values)
    assert gathered.call_count <= 1, "an unchanged profile's support is kept"
