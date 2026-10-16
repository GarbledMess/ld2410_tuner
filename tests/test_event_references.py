"""Native references count observed seconds, independently of telemetry frequency."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_tuner
from tuner_under_test.history.label_lookup import _history_label_reader
from tuner_under_test.presence import reference_training as training
from tuner_under_test.presence import references


def values(value):
    return (value, *([None] * 17))


def histograms(profile):
    totals = {}
    for period in profile["periods"]:
        for key, bins in references.decode(period["data"]).items():
            target = totals.setdefault((period["source"], period["state"], key), {})
            for energy, count in bins.items():
                target[energy] = target.get(energy, 0) + count
    return totals


def test_stable_native_span_and_duplicate_delivery_have_same_duration_support():
    whole, dense = references.empty(), references.empty()
    one, many = references.SpanFold(whole), references.SpanFold(dense)
    one.add(0, 600, {"g0_move": 10.01}, "present", "human", 1)
    for index in range(600):
        many.add(index, index + 1, {"g0_move": 10.01}, "present", "human", 1)
    one.close()
    many.close()
    assert whole["periods"][0]["count"] == 100
    assert dense["periods"][0]["count"] == 100
    assert histograms(whole)[("human", "present", "g0_move")]["11"] == 100
    assert histograms(dense)[("human", "present", "g0_move")]["11"] == pytest.approx(100)
    assert len(whole["periods"]) == len(dense["periods"]) == 1


def test_fixed_epoch_periods_and_fractional_pulse_mass():
    profile = references.empty()
    fold = references.SpanFold(profile)
    fold.add(1799.975, 1800.025, {"g0_move": 19.1}, "present", "human", 1)
    fold.close()
    assert [(p["start"], p["end"]) for p in profile["periods"]] == [
        (1799.975, 1800),
        (1800, 1800.025),
    ]
    assert sum(p["count"] for p in profile["periods"]) == pytest.approx(0.05 / 6)
    assert histograms(profile)[("human", "present", "g0_move")]["20"] == pytest.approx(0.05 / 6)


def test_gaps_state_source_boundaries_and_missing_gates_do_not_manufacture_support():
    profile = references.empty()
    fold = references.SpanFold(profile)
    fold.add(0, 6, {"g0_move": 10, "g0_still": None}, "present", "human", 1)
    fold.add(6, 12, {"g0_move": 10}, "not_present", "human", 1)
    fold.add(12, 18, {"g0_move": 10}, "present", "entity", 0.9)
    fold.add(18, 24, {"g0_move": None}, "present", "human", 1)
    fold.add(30, 36, {"g0_move": 10}, "present", "human", 1)
    fold.close()
    assert len(profile["periods"]) == 4
    assert all("g0_still" not in references.decode(p["data"]) for p in profile["periods"])
    assert sum(p["count"] for p in profile["periods"]) == 4


def fixture(labels=(), pending=False):
    """A radar recorded at 10.01 over [0, 600) s: an ended stretch, or (``pending``) one still
    being recorded and read up to 600 s."""
    device = {"history_labels": list(labels)}
    runtime = SimpleNamespace(
        data={"devices": {"a": device}},
        _history_runtime={},
        _iter_history_samples=lambda device, **kwargs: iter(()),
        _history_columns=lambda device, *args: iter(()),
        _manual_history_state=lambda device, timestamp: _history_label_reader(device)(timestamp),
    )
    events = [(0.0, values(10.01))]
    test_tuner.recorded(runtime, "a", events, 600.0, open_until=600.0 if pending else None)
    return runtime, device


@pytest.mark.parametrize("pending", [False, True])
def test_rebuild_native_human_labels_exactly_splits_edges_and_exclusions(pending):
    runtime, device = fixture(
        [
            {"start": 0, "end": 300.25, "state": "present"},
            {"start": 300.25, "end": 360.75, "state": "unknown"},
            {"start": 360.75, "end": 600, "state": "not_present"},
        ],
        pending,
    )
    with (
        patch.object(training.time, "time", return_value=600),
        patch("tuner_under_test.rooms.attribution.room_targets", return_value=None),
        patch("tuner_under_test.rooms.attribution.auto_targets", return_value=None),
    ):
        training.rebuild_human(runtime, device)
    periods = device["reference_profile"]["periods"]
    assert [(p["start"], p["end"], p["state"]) for p in periods] == [
        (0, 300.25, "present"),
        (360.75, 600, "not_present"),
    ]
    assert sum(p["count"] for p in periods) == pytest.approx((600 - 60.5) / 6)


def test_native_manual_edit_rebuild_replaces_prior_human_histograms():
    runtime, device = fixture([{"start": 0, "end": 600, "state": "present"}])
    with (
        patch.object(training.time, "time", return_value=600),
        patch("tuner_under_test.rooms.attribution.room_targets", return_value=None),
        patch("tuner_under_test.rooms.attribution.auto_targets", return_value=None),
    ):
        training.rebuild_human(runtime, device)
        device["history_labels"][0]["state"] = "not_present"
        training.rebuild_human(runtime, device)
    assert {p["state"] for p in device["reference_profile"]["periods"]} == {"not_present"}
    assert sum(p["count"] for p in device["reference_profile"]["periods"]) == 100


def test_native_reference_confidence_mean_uses_duration_and_close_is_idempotent():
    profile = references.empty()
    fold = references.SpanFold(profile)
    fold.add(0, 6, {"g0_move": 10}, "present", "entity", 0.6)
    fold.add(6, 60, {"g0_move": 20}, "present", "entity", 1)
    fold.close()
    expected = profile["periods"][0].copy()
    assert expected["confidence"] == pytest.approx(0.96)
    fold.close()
    assert profile["periods"][0] == expected


def test_a_rebuild_counts_recorded_history_once():
    runtime, device = fixture([{"start": 0, "end": 600, "state": "present"}])
    with (
        patch.object(training.time, "time", return_value=600),
        patch("tuner_under_test.rooms.attribution.room_targets", return_value=None),
        patch("tuner_under_test.rooms.attribution.auto_targets", return_value=None),
    ):
        training.rebuild_human(runtime, device)
    assert sum(p["count"] for p in device["reference_profile"]["periods"]) == 100


def test_live_native_observation_preserves_human_precedence_and_raw_auto_admission():
    runtime, device = fixture(
        [{"start": 0, "end": 30, "state": "present"}, {"start": 30, "end": 60, "state": "unknown"}]
    )
    with (
        patch("tuner_under_test.rooms.attribution.room_targets", return_value=None),
        patch.object(training, "_room_quiet", return_value=False),
    ):
        training.observe_span(
            runtime, device, 0, 60, values(10.01), "not_present", 0.95, external=True
        )
        training.observe_span(runtime, device, 60, 66, values(20.01), "present", 0.5, external=True)
        training.observe_span(runtime, device, 66, 72, values(20.01), "present", 0.8, external=True)
    periods = device["reference_profile"]["periods"]
    assert [(p["start"], p["end"], p["source"], p["state"]) for p in periods] == [
        (0, 30, "human", "present"),
        (66, 72, "entity", "present"),
    ]
    assert periods[1]["confidence"] == 0.8


def test_native_own_guesses_cannot_bootstrap_independent_references():
    runtime, device = fixture()
    with (
        patch("tuner_under_test.rooms.attribution.room_targets", return_value=None),
        patch.object(training, "_room_quiet", return_value=False),
    ):
        training.observe_span(runtime, device, 0, 600, values(90), "present", 0.95)
    assert not device.get("reference_profile", {}).get("periods")


def test_nested_room_source_reader_boundaries_split_native_reference_periods():
    runtime, device = fixture()

    def room(timestamp):
        return ("not_present", "human", 1.0) if timestamp < 100 else None

    def automatic(timestamp):
        return (
            ("present", "automatic", 0.8)
            if 100 <= timestamp < 300
            else ("not_present", "estimate", 0.7)
            if timestamp >= 300
            else None
        )

    room.parts = (SimpleNamespace(edges=[100]),)
    automatic.parts = (SimpleNamespace(parts=(SimpleNamespace(edges=[100, 300]),)),)
    with (
        patch.object(training.time, "time", return_value=600),
        patch("tuner_under_test.rooms.attribution.room_targets", return_value=room),
        patch("tuner_under_test.rooms.attribution.auto_targets", return_value=automatic),
    ):
        training.rebuild_human(runtime, device)
    periods = device["reference_profile"]["periods"]
    assert [(p["start"], p["end"], p["source"], p["state"]) for p in periods] == [
        (0, 100, "human", "not_present"),
        (100, 300, "entity", "present"),
        (300, 600, "radar", "not_present"),
    ]
    assert all(p.get("room") for p in periods if p["source"] != "human")


def test_native_minimum_independent_support_does_not_depend_on_delivery_density():
    whole, dense = references.empty(), references.empty()
    one, many = references.SpanFold(whole), references.SpanFold(dense)
    one.add(0, 120, {"g0_move": 10}, "present", "human", 1)
    for index in range(240):
        many.add(index / 2, (index + 1) / 2, {"g0_move": 10}, "present", "human", 1)
    one.close()
    many.close()
    assert "present" in references.distributions(whole, 120)[0].get("g0_move", {})
    assert "present" in references.distributions(dense, 120)[0].get("g0_move", {})


def test_native_own_automatic_confidence_matches_existing_fixed_trust_contract():
    runtime, device = fixture([{"start": 0, "end": 6, "state": "present"}])
    with (
        patch("tuner_under_test.rooms.attribution.room_targets", return_value=None),
        patch.object(training, "_room_quiet", return_value=False),
    ):
        training.observe_span(runtime, device, 0, 6, values(10), None, 0)
        training.observe_span(runtime, device, 6, 12, values(40), "present", 0.95)
        training.observe_span(runtime, device, 12, 18, values(40), "present", 0.6)
    radar = [p for p in device["reference_profile"]["periods"] if p["source"] == "radar"]
    assert [(p["start"], p["end"], p["confidence"]) for p in radar] == [(12, 18, 0.95)]


def test_native_own_present_observation_keeps_existing_quiet_veto():
    runtime, device = fixture([{"start": 0, "end": 6, "state": "present"}])
    device["histograms"] = {"g0_move": {"not_present": [20 if i == 10 else 0 for i in range(101)]}}
    with (
        patch("tuner_under_test.rooms.attribution.room_targets", return_value=None),
        patch.object(training, "_room_quiet", return_value=False),
    ):
        training.observe_span(runtime, device, 0, 6, values(10), None, 0)
        training.observe_span(runtime, device, 6, 12, values(10), "present", 0.6)
        training.observe_span(runtime, device, 12, 18, values(20.01), "present", 0.6)
    radar = [p for p in device["reference_profile"]["periods"] if p["source"] == "radar"]
    assert [(p["start"], p["end"]) for p in radar] == [(12, 18)]
    assert references.decode(radar[0]["data"])["g0_move"] == {"21": 1}


def test_native_reference_periods_remain_bounded_and_ignore_unadmitted_inputs():
    profile = references.empty()
    fold = references.SpanFold(profile)
    for i in range(references.MAX_PERIODS + 10):
        fold.add(i * 1800, (i + 1) * 1800, {"g0_move": 10}, "present", "human", 1)
    fold.close()
    assert len(profile["periods"]) == references.MAX_PERIODS
    count = sum(p["count"] for p in profile["periods"])
    for state, source, confidence in [
        ("unknown", "human", 1),
        ("present", "invalid", 1),
        ("present", "human", float("nan")),
        ("present", "human", 0),
    ]:
        fold.add(100000, 100006, {"g0_move": 10}, state, source, confidence)
    fold.add(100000, 100006, {"g0_move": None, "arbitrary": 30}, "present", "human", 1)
    fold.close()
    assert sum(p["count"] for p in profile["periods"]) == count
    with pytest.raises(ValueError):
        fold.add(2, 1, {"g0_move": 10}, "present", "human", 1)


def test_native_rebuild_clips_retention_before_duration_folding():
    runtime, device = fixture([{"start": 0, "end": 600, "state": "present"}])
    with (
        patch.object(training.time, "time", return_value=600),
        patch("tuner_under_test.history.policy.retention_seconds", return_value=300),
        patch("tuner_under_test.rooms.attribution.room_targets", return_value=None),
        patch("tuner_under_test.rooms.attribution.auto_targets", return_value=None),
    ):
        training.rebuild_human(runtime, device)
    periods = device["reference_profile"]["periods"]
    assert [(p["start"], p["end"], p["count"]) for p in periods] == [(300, 600, 50)]


def test_live_span_splits_at_room_label_edges_without_running_full_attribution():
    runtime, device = fixture()
    runtime.data["room_labels"] = {
        "zone:any": [{"start": 30, "end": 900, "state": "not_present", "source": "manual"}]
    }
    device["reference_profile"] = references.empty()
    device["reference_profile"]["periods"].append(
        {"start": -1, "end": -1, "source": "human", "state": "present", "count": 0, "data": ""}
    )

    def full_attribution(*_args, **_kwargs):
        raise AssertionError("live span observation must stay incremental")

    with (
        patch("tuner_under_test.rooms.attribution.room_targets", full_attribution),
        patch.object(training, "_room_quiet", side_effect=lambda _r, _d, t: t >= 30),
    ):
        training.observe_span(runtime, device, 0, 60, values(40.01), "present", 0.95, external=True)
    found = [
        (p["start"], p["end"], p["source"], p["state"])
        for p in device["reference_profile"]["periods"]
        if p["start"] >= 0
    ]
    assert found == [(0, 30, "entity", "present"), (30, 60, "human", "not_present")]


def test_a_period_continued_by_later_folds_keeps_all_its_evidence():
    """Live observation opens a new fold per poll; continuing a period must keep its histogram
    (it is stored in six-second units, accumulated in seconds)."""
    once, piecewise = references.empty(), references.empty()
    whole = references.SpanFold(once)
    whole.add(0, 60, {"g0_move": 10}, "present", "human", 1)
    whole.close()
    for start in range(0, 60, 6):
        fold = references.SpanFold(piecewise)
        fold.add(start, start + 6, {"g0_move": 10}, "present", "human", 1)
        fold.close()
    assert (
        histograms(piecewise) == histograms(once) == {("human", "present", "g0_move"): {"10": 10}}
    )
