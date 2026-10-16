"""Packed room readings retain the exact physical endpoints needed by replay."""

import test_tuner
from tuner_under_test.rooms.evidence import Rows


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
