"""Synthetic production evidence at the captured row-source boundary."""

import tracemalloc
from array import array
from datetime import date
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from test_hybrid_rows import CapturedRowSource as NativeSource
from test_hybrid_rows import source as captured_source
from tuner_under_test.calibration import event_quality
from tuner_under_test.calibration.quality import QualityModel, learn
from tuner_under_test.engine.hybrid import adapters, quality
from tuner_under_test.engine.hybrid.types import RowBlock
from tuner_under_test.history import fit_rows, guesses
from tuner_under_test.rooms import evidence


class Source:
    def __init__(self, view, device_id, channels):
        self.view, self.device_id, self.channels = view, device_id, channels

    def blocks(self, lo, hi):
        for a, b, energy in self.view.blocks:
            a, b = max(lo, a), min(hi, b)
            if b > a:
                yield RowBlock(
                    array("d", [a]), array("d", [b]), (bytes([energy]),) + (b"\xff",) * 17
                )

    def rows_in(self, lo, hi):
        return sum(1 for _ in self.blocks(lo, hi))


def view(labels=(), runs=(), blocks=((0, 100, 20),)):
    return SimpleNamespace(
        data={"devices": {"r": {"history_labels": list(labels)}}, "room_snapshot": None},
        blocks=blocks,
        runs=runs,
        _history_until=100,
        _attribution_until=100,
    )


def label(a, b, state):
    return {"start": a, "end": b, "state": state}


@pytest.fixture(autouse=True)
def captured_boundary(monkeypatch):
    monkeypatch.setattr(adapters, "CapturedRowSource", Source)
    monkeypatch.setattr(quality, "CapturedRowSource", Source)
    monkeypatch.setattr(fit_rows, "CapturedRowSource", Source)
    monkeypatch.setattr(
        guesses,
        "of",
        lambda v, _id, since=float("-inf"): guesses.Guesses(
            run for run in v.runs if run[1] > since
        ),
    )


def problem(v, end=100, context=None):
    return adapters.device_problem(
        v, "r", ["g0_move"], {"g0_move": 40}, {}, 0.25, None, 0, end, quality_context=context
    )


def test_device_targets_preserve_own_then_room_then_automatic():
    v = view(
        [label(0, 10, "present"), label(10, 20, "unknown")],
        [(0, 100, "not_present", 20, guesses.EXTERNAL)],
    )
    v.data["room_snapshot"] = {
        "device_id": "r",
        "own": ["room"],
        "groups": {"room": {"device_ids": ["r"]}},
        "members": {"r": v.data["devices"]["r"]},
        "room_labels": {"room": [{**label(20, 30, "present"), "source": "manual"}]},
    }
    p = problem(v)
    assert [(s.start, s.end, s.state, s.source) for s in p.zones[0].truth] == [
        (0, 10, "present", "human"),
        (20, 30, "present", "human"),
        (30, 100, "not_present", "automatic"),
    ]


def test_raw_admission_precedes_correction_without_second_floor():
    v = view(
        runs=[
            (0, 50, "present", 11, guesses.EXTERNAL),
            (50, 100, "not_present", 10, guesses.EXTERNAL),
        ]
    )
    p = problem(v)
    (s,) = p.zones[0].truth
    assert s.confidence == pytest.approx(0.55 * 0.75)
    assert (s.start, s.end) == (0, 50)


def test_room_truth_order_is_unchanged():
    v = view([label(0, 20, "present"), label(20, 30, "unknown")])
    item = {"view": v, "thresholds": {"g0_move": 40}, "timing": {}}
    room = {
        "group_id": "z",
        "room_labels": {
            "z": [
                {**label(0, 10, "not_present"), "source": "manual"},
                {**label(0, 100, "not_present"), "source": "sensor", "confidence": 0.8},
            ]
        },
    }
    p = adapters.room_problem({"z": {"r": item}}, 0, 100, {"z": room}, 0.25)
    assert [(s.start, s.end, s.state, s.source) for s in p.zones[0].truth] == [
        (0, 10, "not_present", "human"),
        (10, 20, "present", "human"),
        (30, 100, "not_present", "automatic"),
    ]
    assert p.zones[0].truth[-1].confidence == pytest.approx(0.8 * 0.75)


def test_sensor_prior_and_dependencies_do_not_create_trials():
    v = view()
    item = {"view": v, "thresholds": {"g0_move": 40}, "timing": {}}
    room = {
        "group_id": "z",
        "room_labels": {"z": [{**label(0, 100, "present"), "source": "sensor", "confidence": 1}]},
    }
    p = adapters.room_problem({"z": {"r": item}}, 0, 100, {"z": room}, 0.25)
    model = p.policy.quality["models"]["r"]
    assert model.human_episodes("present") == model.episode_mass("present") == 0
    assert p.zones[0].truth[0].dependencies == ()


def test_one_row_source_per_unique_radar():
    v = view([label(0, 100, "present")])
    item = {"view": v, "thresholds": {"g0_move": 40}, "timing": {}}
    p = adapters.room_problem({"a": {"r": item}, "b": {"r": item}}, 0, 100, {}, 0.25)
    assert len(p.rows) == len(p.radars) == 1
    assert [z.members for z in p.zones] == [("r",), ("r",)]


def test_quality_collection_has_bounded_resident_histograms():
    v = view([label(i, i + 1, "present" if i % 2 else "not_present") for i in range(100)])
    context = quality.collect_quality(v, "r", ["g0_move"], 0, 100, None, None)
    assert context.histogram_slots <= 5 * 101
    model = quality.learn_quality(context)
    assert isinstance(model, QualityModel)
    assert model.human_episodes("present") == model.human_episodes("not_present") == 50
    assert model.regimes["g0_move"].occupied_episodes == 50


def test_cutoff_hides_future_energy_labels_and_late_guesses():
    v = view(
        [label(0, 100, "not_present")],
        [(50, 100, "present", 20, guesses.EXTERNAL)],
        [(0, 50, 20), (50, 100, 99)],
    )
    p = problem(v, end=50)
    context = p.policy.quality["models"]["r"].context
    assert context.raw_runs == ()
    assert context.gate_empty["g0_move"][99] == 0
    assert all(s.end <= 50 for s in p.zones[0].truth)
    assert max(b.ends[0] for b in p.rows["r"].blocks(0, 100)) == 50


def test_horizon_excludes_only_old_automatic_evidence():
    old = 29 * 86400
    v = view(
        [label(0, 10, "present")],
        [(10, old, "not_present", 20, guesses.EXTERNAL)],
        [(0, old + 100, 20)],
    )
    v._history_until = v._attribution_until = old + 100
    p = problem(v, end=old + 100)
    assert p.policy.automatic_horizon_days == 28
    assert p.zones[0].truth[0].start == 0
    assert p.zones[0].truth[-1].start == old + 100 - 28 * 86400


def test_explicit_legal_domains_and_invalid_keys():
    v = view()
    p = adapters.device_problem(
        v,
        "r",
        ["g0_move"],
        {"g0_move": 10.5},
        {},
        0.25,
        {"g0_move": {"min": 0, "max": 98, "step": 7}},
        0,
        100,
    )
    assert p.radars["r"].current == {0: 14}
    with pytest.raises(ValueError):
        adapters.device_problem(v, "r", ["invalid"], {}, {}, 0.25, None, 0, 100)


def test_streamed_quality_equals_existing_episode_formulas(monkeypatch):
    v = view(
        [
            label(0, 1200, "not_present"),
            label(1200, 2400, "present"),
            label(2400, 3600, "present"),
            label(3600, 4800, "not_present"),
        ],
        [(0, 4800, "present", 15, guesses.EXTERNAL)],
        [
            (0, 600, 20),
            (600, 1200, 21),
            (1200, 1800, 30),
            (1800, 2400, 255),
            (2400, 3000, 30),
            (3000, 3600, 40),
            (3600, 4800, 20),
        ],
    )
    v._history_until = v._attribution_until = 4800
    monkeypatch.setattr(event_quality.spans, "bounds", lambda *_: (0, 4800))
    monkeypatch.setattr(
        event_quality.spans,
        "events",
        lambda vv, _id, start, end: quality._physical(Source(vv, _id, (0,)), start, end),
    )
    streamed = quality.learn_quality(
        quality.collect_quality(v, "r", ["g0_move"], 0, 4800, None, None)
    )
    original = learn(event_quality.collect(v, "r", ["g0_move"], 0, 4800, None, None))
    assert streamed.regimes == original.regimes
    assert streamed.posteriors == original.posteriors
    assert streamed.context.episodes == original.context.episodes
    assert streamed.context.overlaps == original.context.overlaps
    assert streamed.context.automatic_episodes == original.context.automatic_episodes


def test_quality_histogram_memory_does_not_scale_as_episodes_times_bins():
    def peak(count):
        v = view(
            [label(i, i + 1, "present" if i % 2 else "not_present") for i in range(count)],
            blocks=[(0, count, 20)],
        )
        v._history_until = v._attribution_until = count
        tracemalloc.start()
        try:
            quality.collect_quality(v, "r", ["g0_move"], 0, count, None, None)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    assert peak(1000) - peak(10) < 700_000


def test_provided_quality_context_cannot_leak_future_training():
    v = view([label(0, 100, "not_present")], blocks=[(0, 50, 20), (50, 100, 99)])
    future = quality.collect_quality(v, "r", ["g0_move"], 0, 100, None, None)
    p = problem(v, end=50, context=future)
    assert p.policy.quality["models"]["r"].context.gate_empty["g0_move"][99] == 0


def test_confidence_fragments_share_original_episode_identity():
    v = view(
        runs=[(0, 50, "present", 11, guesses.EXTERNAL), (50, 100, "present", 20, guesses.EXTERNAL)]
    )
    p = problem(v)
    assert len(p.zones[0].truth) == 2
    assert p.zones[0].truth[0].episode_id == p.zones[0].truth[1].episode_id
    assert p.zones[0].truth[0].dependencies == ("r",)
    assert len(p.policy.quality["models"]["r"].context.automatic_episodes) == 1


def test_device_sensor_prior_has_no_member_dependencies():
    v = view()
    v.data["room_snapshot"] = {
        "device_id": "r",
        "own": ["room"],
        "groups": {"room": {"device_ids": ["r"]}},
        "members": {"r": v.data["devices"]["r"]},
        "room_labels": {
            "room": [{**label(0, 100, "present"), "source": "sensor", "confidence": 1}]
        },
    }
    p = problem(v)
    assert p.zones[0].truth[0].dependencies == ()


def test_event_quality_streaming_helper_keeps_old_collector_available():
    v = view([label(0, 100, "present")])
    context = event_quality.collect_streamed(v, "r", ["g0_move"], 0, 100, None, None)
    assert quality.learn_quality(context).human_episodes("present") == 1


def test_cutoff_day_neither_reads_nor_poisons_complete_day_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "CapturedRowSource", NativeSource)
    src, days, _ = captured_source(tmp_path)
    full = SimpleNamespace(_days=days, _history_until=86400)
    cutoff = SimpleNamespace(_days=days, _history_until=1)
    d = date(1970, 1, 1)
    assert adapters._Days({"synthetic": full}).digest("synthetic", d, (0,)).channels[0][0] == 2
    assert adapters._Days({"synthetic": cutoff}).digest("synthetic", d, (0,)).channels[0][0] == 0

    cold = tmp_path / "cold"
    cold.mkdir()
    _, days, _ = captured_source(cold)
    cutoff._days = full._days = days
    assert adapters._Days({"synthetic": cutoff}).digest("synthetic", d, (0,)).channels[0][0] == 0
    assert adapters._Days({"synthetic": full}).digest("synthetic", d, (0,)).channels[0][0] == 2


def test_fractional_empty_histograms_keep_existing_summation_units(monkeypatch):
    v = view(
        [label(0, 100, "not_present")], blocks=[(i / 10, (i + 1) / 10, 20) for i in range(1000)]
    )
    monkeypatch.setattr(event_quality.spans, "bounds", lambda *_: (0, 100))
    monkeypatch.setattr(
        event_quality.spans,
        "events",
        lambda vv, _id, start, end: quality._physical(Source(vv, _id, (0,)), start, end),
    )
    streamed = quality.collect_quality(v, "r", ["g0_move"], 0, 100, None, None)
    original = event_quality.collect(v, "r", ["g0_move"], 0, 100, None, None)
    assert streamed.gate_empty == original.gate_empty


def test_attribution_baseline_respects_snapshot_physical_window():
    v = view()
    v._physical_history_start, v._attribution_since = 50, 60
    cells = list(evidence._stream_cells(v, "r", [], 0, 100))
    assert cells[0][0] == 60
    p = problem(v)
    assert next(p.rows["r"].blocks(0, 100)).times[0] == 50
    assert p.policy.quality["models"]["r"].context.episodes == ()
