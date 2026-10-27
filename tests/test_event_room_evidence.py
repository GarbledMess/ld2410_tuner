"""Packed room readings retain the exact physical endpoints needed by replay."""

import pytest
import test_tuner
from tuner_under_test.rooms.evidence import Rows


def test_room_replay_does_not_read_rows_before_requested_start(monkeypatch):
    from test_rooms import member
    from tuner_under_test.history import spans
    from tuner_under_test.rooms import evidence

    item = member("synthetic", list(range(100)) * 2, [])
    original = spans._search_day
    windows = []

    def observe(day, start, end, indices):
        windows.append((start, end))
        yield from original(day, start, end, indices)

    monkeypatch.setattr(spans, "_search_day", observe)
    found = evidence.read(item["view"], "synthetic", ["g0_move"], 600, 1200)
    assert windows == [(600, 1200)]
    assert min(found["rows"].times) == 600


@pytest.mark.parametrize(
    ("timing", "warmup"),
    [
        ({"timeout": 5, "on_delay": 2, "off_delay": 3}, 10),
        ({"timeout": 5, "on_delay": None, "off_delay": 3}, 5),
        ({"timeout": None, "on_delay": 2, "off_delay": 3}, 0),
    ],
)
def test_assessment_reads_only_required_timing_warmup(monkeypatch, timing, warmup):
    from test_rooms import labels, member
    from tuner_under_test.history import spans
    from tuner_under_test.rooms import assessment

    item = member("synthetic", [40] * 100 + [0] * 100, labels(), timing)
    original = spans._search_day
    starts = []

    def observe(day, start, end, indices):
        starts.append(start)
        yield from original(day, start, end, indices)

    monkeypatch.setattr(spans, "_search_day", observe)
    assessment.calculate({"synthetic": item}, 600, 1200)
    assert starts == [600 - warmup]


@pytest.mark.parametrize("timing", [{}, {"timeout": 5, "on_delay": 2, "off_delay": 3}])
def test_fit_is_identical_with_extra_history_before_replay_warmup(timing):
    from test_rooms import member
    from tuner_under_test.rooms import adaptive, fitting

    labelled = [
        {"start": 0, "end": 1200, "state": "present"},
        {"start": 1200, "end": 1800, "state": "not_present"},
    ]
    results = []
    for old in ([], [99] * 90):
        item = member(
            "synthetic", old + [40] * 110 + [10] * 100, labelled, timing, offset=0 if old else 540
        )
        members = {"synthetic": item}
        results.append(
            fitting.fit(
                members,
                600,
                1800,
                quality_contexts=adaptive.collect_contexts({"zone": members}, 600, 1800),
            )
        )
    assert results[0] == results[1]


def test_warmup_is_replayed_but_not_scored_as_fit_evidence():
    from test_rooms import labels, member
    from tuner_under_test.rooms import assessment
    from tuner_under_test.rooms.objective import Objective

    item = member(
        "synthetic",
        [40] * 200 + [0] * 100,
        labels(1200, 1800),
        {"timeout": 5, "on_delay": 2, "off_delay": 3},
    )
    members = {"synthetic": item}
    report = assessment.calculate(members, 600, 1800)
    objective = Objective(members, 600, 1800)
    assert objective.rates({"synthetic": {"g0_move": 20}})["human"][0] == 0
    assert report["shared_recording_seconds"] == 1197


def test_packed_rows_preserve_event_ends_through_slicing_and_sorting():
    rows = Rows([10, 0], [bytes([20, 30])], {"g0_move": 0}, ends=[20, 10])
    ordered = rows.in_time_order()
    assert list(ordered.times) == [0, 10]
    assert ordered.ends == [10, 20]
    assert list(ordered.column("g0_move")) == [30, 20]
    assert ordered[1:].ends == [20]


def test_legacy_rows_keep_the_sampled_timing_contract():
    rows = Rows([0, 6], [bytes([20, 30])], {"g0_move": 0})
    assert rows.ends is None
    assert rows[:1].ends is None


def test_native_room_evidence_reads_stable_spans_and_keeps_float_threshold_semantics():
    from types import SimpleNamespace

    from tuner_under_test.rooms import evidence, replay

    view = SimpleNamespace(data={"devices": {"arbitrary": {}}}, _history_runtime={})
    test_tuner.recorded(view, "arbitrary", [(0, (10.1,) + (None,) * 17)], 600)
    result = evidence.read(view, "arbitrary", ["g0_move"], 0, 600)
    assert list(result["rows"].ends) == [600]
    assert replay.radar(result["rows"], {"g0_move": 10}, {})["lower"] == [(0, 600)]
    assert replay.radar(result["rows"], {"g0_move": 11}, {})["lower"] == []


def test_native_room_assessment_uses_duration_support_not_transition_count():
    from test_rooms import labels, member
    from tuner_under_test.rooms import assessment

    item = member("any-name", [], labels())
    events = [(0, (40,) + (None,) * 17), (600, (0,) + (None,) * 17)]
    test_tuner.recorded(item["view"], "any-name", events, 1200)
    result = assessment.calculate({"any-name": item}, 0, 1200)
    assert result["shared_recording_seconds"] == 1200
    assert result["room"]["score"] == 100
    assert result["room"]["outcomes"]["human"]["present"]["samples"] == 100


def test_label_change_does_not_reset_hold_or_pending_on_delay():
    from test_rooms import labels, member
    from tuner_under_test.rooms import assessment

    timing = {"timeout": 5, "on_delay": 2, "off_delay": 0}
    item = member("renamed-radar", [], labels(), timing)
    events = [(0, (0,) + (None,) * 17), (599, (40,) + (None,) * 17), (600, (0,) + (None,) * 17)]
    test_tuner.recorded(item["view"], "renamed-radar", events, 1200)
    result = assessment.calculate({"renamed-radar": item}, 600, 1200)
    empty = result["room"]["outcomes"]["human"]["not_present"]
    assert empty["seconds"] == 600
    assert empty["active_seconds"] == 4  # On at601, held until605.


def test_room_replay_does_not_apply_sample_gap_rules_to_event_cells():
    from tuner_under_test.rooms import replay

    rows = Rows([0], [bytes([40])], {"g0_move": 0}, ends=[600])
    assert replay.radar(rows, {"g0_move": 20}, {})["coverage"] == [(0, 600)]


def test_mixed_sampled_and_event_rows_keep_separate_geometry():
    from tuner_under_test.calibration.duration import DurationGroup
    from tuner_under_test.rooms import replay

    rows = Rows([0, 6, 10], [bytes([40, 0, 40])], {"g0_move": 0}, ends=[None, None, 600])
    group = DurationGroup(rows)
    result = replay.from_bits(group, group, 5)
    assert result["coverage"] == [(0, 6), (10, 600)]
    assert result["lower"] == [(0, 3), (10, 600)]


def test_room_objective_releases_replay_geometry_without_cyclic_collection():
    import gc
    import weakref

    from test_rooms import labels, member
    from tuner_under_test.rooms.objective import Objective

    gc.disable()
    try:
        item = member("synthetic", [40] * 100 + [0] * 100, labels())
        objective = Objective({"synthetic": item}, 0, 1200)
        objective.rank(objective.current)
        radar = objective.radars["synthetic"]
        references = [
            weakref.ref(objective),
            weakref.ref(objective.windows),
            weakref.ref(radar),
            weakref.ref(radar.positive),
            weakref.ref(radar.negative),
            weakref.ref(radar.projector),
        ]
        del radar, objective
        assert all(reference() is None for reference in references)
    finally:
        gc.enable()


def test_recorded_replay_rows_do_not_retain_per_row_byte_objects(monkeypatch):
    import tracemalloc

    from test_rooms import member
    from tuner_under_test.rooms import evidence

    count = 30000
    item = member("synthetic", list(range(100)) * (count // 100), [])
    view = item["view"]
    store = view._days
    decoded = {date: store.day("synthetic", date) for date in store.days("synthetic")}
    monkeypatch.setattr(store, "day", lambda device, date, until=None: decoded[date])
    tracemalloc.start()
    try:
        rows, barriers = evidence._recorded(view, "synthetic", ["g0_move"], count * 6)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert rows.column("g0_move") == bytes(range(100)) * (count // 100)
    assert list(rows.times) == [i * 6 for i in range(count)]
    assert list(rows.ends) == [*(i * 6 for i in range(1, count)), count * 6 - 3]
    assert barriers == []
    assert peak < 1_500_000


@pytest.mark.parametrize("supported", [frozenset(range(101)), frozenset((20, 80))])
def test_supported_masks_share_equal_tables_without_expanding_memory(supported):
    import tracemalloc
    from array import array

    from tuner_under_test.calibration.metrics import threshold_masks
    from tuner_under_test.rooms.objective import _supported_tables

    count = 100000
    column = bytes(i % 101 for i in range(count))
    rows = Rows(array("d", range(count)), [column], {"g0_move": 0})
    tables = threshold_masks(rows, "g0_move")
    allowed = sum(1 << i for i, value in enumerate(column) if value in supported)
    expected = tuple(bits & allowed for bits in tables)
    tracemalloc.start()
    try:
        actual = _supported_tables(rows, "g0_move", tables, supported)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert actual == expected
    assert peak < 400_000
