"""Bounded cores preserve class time; timing context never contributes extra mass."""

import weakref
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.calibration import evidence_budget, evidence_plan
from tuner_under_test.history import dayfile, spans
from tuner_under_test.history.label_lookup import index_reader


def fixture(count=4000, step=1):
    records = [dayfile.Change(i * step, {0: 10 + i % 50}) for i in range(count)]
    records.append(dayfile.End(count * step))
    day = dayfile.day_from_log(records, None, 0, 86400)
    store = SimpleNamespace(
        days=lambda key: [date(1970, 1, 1)], day=lambda *a, **kw: day, identity=lambda *a: None
    )
    view = SimpleNamespace(_days=store, _history_until=count * step)
    read = index_reader(
        (
            [0, count * step / 2, count * step],
            [("present", "human"), ("not_present", "automatic"), None],
        )
    )
    return view, read


def build(view, read, limit=50, timing=None):
    return evidence_plan.plan(view, "a", ["g0_move"], 0, 86400, timing or {}, read, limit=limit)


def test_below_budget_returns_none_without_vectors_or_chunk_summaries():
    view, read = fixture(count=100)
    with (
        patch.object(dayfile.Day, "vector", side_effect=AssertionError("vector replay")),
        patch.object(evidence_budget, "chunks_of", side_effect=AssertionError("unneeded chunks")),
    ):
        assert build(view, read, limit=100) is None


def test_dense_plan_is_deterministic_preserves_each_stratums_observed_mass():
    view, read = fixture()
    with patch.object(dayfile.Day, "vector", side_effect=AssertionError("vector replay")):
        result = build(view, read)
        assert result == build(view, read)
    assert result.status == "bounded"
    assert result.audit["context_rows"] <= result.audit["allowed_rows"]
    assert result.audit["original_rows"] == 4000
    for state in (("present", "human"), ("not_present", "automatic")):
        assert sum(
            (w.end - w.start) * w.scale for w in result.cores if w.state == state
        ) == pytest.approx(2000)
    assert result.seconds([(0, 4000)]) == pytest.approx(4000)
    assert result.seconds([(0, 4000), (0, 4000)]) == pytest.approx(4000)
    assert result.scale_at(-1) == 0
    assert result.weights([-1, result.cores[0].start]) == [0, result.cores[0].scale]


def test_halo_context_is_physical_bounded_and_not_scored_as_core():
    view, read = fixture()
    result = build(view, read, timing={"timeout": 3, "on_delay": 2, "off_delay": 1})
    assert result.status == "bounded"
    assert result.audit["halo_seconds"] == 6
    assert result.audit["context_rows"] <= result.audit["allowed_rows"]
    assert all(0 <= low < high <= 4000 for low, high in result.contexts)
    assert result.seconds([(0, 4000)]) == pytest.approx(4000)
    for core in result.cores:
        assert any(
            low <= max(0, core.start - 6) and high >= min(4000, core.end + 6)
            for low, high in result.contexts
        )


def test_huge_timing_halo_reports_unsupported_without_truncated_context():
    view, read = fixture(count=8000)
    result = build(view, read, limit=10, timing={"timeout": 10000})
    assert result.status == "unsupported"
    assert result.cores == () and result.contexts == ()
    assert result.audit["reason"] == "timing_context_exceeds_budget"


def test_query_and_frozen_bounds_never_leak_future_evidence():
    view, read = fixture()
    view._history_until = 1500
    view._physical_history_start = 100
    view._attribution_until = 1400
    result = evidence_plan.plan(view, "a", ["g0_move"], 200, 3000, {}, read, limit=10)
    assert result is not None
    assert all(200 <= low < high <= 1400 for low, high in result.contexts)
    assert result.seconds([(0, 5000)]) == pytest.approx(1200)


def test_gaps_and_all_missing_key_time_are_not_scored_or_filled():
    view, read = fixture(count=4000)
    day = dayfile.day_from_log(
        [
            dayfile.Change(0, {0: 10}),
            dayfile.End(10),
            dayfile.Change(20, {0: None, 1: 9}),
            dayfile.End(25),
            *[dayfile.Change(30 + i, {0: i % 50}) for i in range(3000)],
            dayfile.End(3030),
        ],
        None,
        0,
        86400,
    )
    view._days.day = lambda *a, **kw: day
    result = build(view, read, limit=10, timing={"timeout": 5})
    assert result.status == "bounded"
    assert result.seconds([(10, 30)]) == 0
    assert result.seconds([(0, 4000)]) == pytest.approx(3010)
    assert all(not (low < 20 and high > 10) for low, high in result.contexts)


def test_invalid_budget_and_unbounded_timing_are_rejected_explicitly():
    view, read = fixture()
    with pytest.raises(ValueError):
        build(view, read, limit=0)
    with pytest.raises(ValueError):
        build(view, read, timing={"timeout": float("inf")})


def test_fractional_cutoffs_do_not_use_label_before_held_window_start():
    view, read = fixture()
    edge = 100.005
    read = index_reader(
        ([0, edge, 2000], [("present", "human"), ("not_present", "automatic"), None])
    )
    result = evidence_plan.plan(view, "a", ["g0_move"], edge, 500.005, {}, read, limit=10)
    assert result.status == "bounded"
    assert all(core.state == ("not_present", "automatic") for core in result.cores)
    assert result.seconds([(0, 4000)]) == pytest.approx(400)
    assert min(core.start for core in result.cores) >= edge


def test_plan_weighted_intervals_count_only_disjoint_scored_cores():
    cores = (
        evidence_budget.Window(1, 3, "present", 2),
        evidence_budget.Window(5, 7, "not_present", 3),
    )
    result = evidence_plan.Plan(cores, ((0, 8),), {"status": "bounded"})
    assert result.weights([0, 1, 2, 3, 4, 5, 7]) == [0, 2, 2, 0, 0, 3, 0]
    assert result.seconds([(0, 2), (1, 6)]) == 7
    assert result.seconds([(3, 5)]) == 0


def test_overlapping_contexts_count_a_carried_row_only_once():
    view, read = fixture()
    result = build(view, read, timing={"timeout": 2})
    real = SimpleNamespace(
        _days=view._days, _history_until=4000, _learning_windows={"a": result.contexts}
    )
    actual = list(spans.events(real, "a"))
    assert result.audit["context_rows"] == len(actual)


def test_epoch_millisecond_holds_preserve_mass_without_cancelling_large_timestamps():
    view, _read = fixture()
    base, step = 1760486400, 0.01337
    day = dayfile.day_from_log(
        [dayfile.Change(base + i * step, {0: i % 50}) for i in range(4000)]
        + [dayfile.End(base + 4000 * step)],
        None,
        base,
        base + 86400,
    )
    view._days.days = lambda key: [date(2025, 10, 15)]
    view._days.day = lambda *a, **kw: day
    view._history_until = base + 4000 * step
    read = index_reader(
        (
            [base, base + 2000 * step, base + 4000 * step],
            [("present", "human"), ("not_present", "human"), None],
        )
    )
    result = evidence_plan.plan(
        view, "a", ["g0_move"], base, base + 4000 * step, {}, read, limit=10
    )
    assert result.seconds([(base, base + 4000 * step)]) == pytest.approx(
        day.ends[-1] - day.times[0], rel=0, abs=1e-3
    )


def test_planning_does_not_retain_all_decoded_days_outside_store_cache():
    encoded = {}
    for offset in range(4):
        start = offset * 86400
        day = dayfile.day_from_log(
            [dayfile.Change(start + i, {0: i % 50}) for i in range(1500)]
            + [dayfile.End(start + 1500)],
            None,
            start,
            start + 86400,
        )
        encoded[date(1970, 1, 1) + timedelta(days=offset)] = dayfile.encode_day(day)
    alive = []

    def load(key, when, **kwargs):
        assert sum(ref() is not None for ref in alive) <= 1, "planner retained decoded days"
        current = dayfile.decode_day(encoded[when])
        alive.append(weakref.ref(current))
        return current

    view = SimpleNamespace(
        _days=SimpleNamespace(days=lambda key: list(encoded), day=load, identity=lambda *a: None),
        _history_until=4 * 86400,
    )
    read = index_reader(([0], [("present", "human")]))
    result = evidence_plan.plan(view, "a", ["g0_move"], 0, 4 * 86400, {}, read, limit=50)
    assert result.status == "bounded"
    assert result.seconds([(0, 4 * 86400)]) == pytest.approx(6000)


def test_unknown_only_dense_recording_reports_no_scored_evidence():
    view, _read = fixture()
    read = index_reader(([0], ["unknown"]))
    result = build(view, read)
    assert result.status == "unsupported"
    assert result.audit["reason"] == "no_scored_evidence"


def test_independent_provenance_strata_and_whole_windows_keep_their_own_mass():
    view, _read = fixture(count=8000)
    states = [
        ("present", "human"),
        ("present", "automatic"),
        ("not_present", "human"),
        ("not_present", "automatic"),
    ]
    read = index_reader(([0, 2000, 4000, 6000, 8000], [*states, None]))
    result = build(view, read, limit=1000)
    assert result.audit["strata"] == 4
    for state in states:
        assert sum(
            (core.end - core.start) * core.scale for core in result.cores if core.state == state
        ) == pytest.approx(2000)
    for core in result.cores:
        if core.scale == 1:
            assert result.seconds([(core.start, core.end)]) == core.end - core.start
