"""Precise event cells retain duration when the bitmask replay is used by search."""

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.calibration.duration import DurationGroup
from tuner_under_test.rooms import replay


class Cells(list):
    def __init__(self, intervals):
        super().__init__(
            (start, {"g0_move": value}, "present", 0.5) for start, _, value in intervals
        )
        self.ends = [end for _, end, _ in intervals]

    def __getitem__(self, item):
        if isinstance(item, slice):
            rows = super().__getitem__(item)
            return Cells(
                [
                    (row[0], end, row[1]["g0_move"])
                    for row, end in zip(rows, self.ends[item], strict=True)
                ]
            )
        return super().__getitem__(item)


def test_single_stable_cell_has_all_observed_time():
    result = DurationGroup(Cells([(0, 600, 20)])).measure(1)
    assert result["observed_seconds"] == 600
    assert result["active_seconds"] == 600
    assert result["weighted_seconds"] == 300


def test_narrow_pulse_is_precise_in_mask_duration_and_room_projection():
    rows = Cells([(100, 100.125, 0), (100.125, 100.175, 20), (100.175, 101, 0)])
    group = DurationGroup(rows)
    measured = group.measure(2)
    assert measured["observed_seconds"] == 1
    assert measured["active_seconds"] == pytest.approx(0.05)
    projected = replay.from_bits(group, group, 2)
    assert projected["coverage"] == [(100, 101)]
    assert projected["lower"] == [(100.125, 100.175)]


def test_duplicate_cells_and_long_stability_do_not_create_timing_sessions():
    timing = {"timeout": 2, "on_delay": 1, "off_delay": 3}
    sparse = DurationGroup(Cells([(0, 600, 20)]), timing).measure(1)
    dense = DurationGroup(Cells([(0, 200, 20), (200, 400, 20), (400, 600, 20)]), timing).measure(7)
    assert sparse == dense
    assert sparse["observed_seconds"] == 600
    assert sparse["active_seconds"] == 599


def test_explicit_gap_resets_hold_and_never_adds_unobserved_time():
    rows = Cells([(0, 1, 20), (10, 20, 0)])
    group = DurationGroup(rows, {"timeout": 20, "on_delay": 0, "off_delay": 5})
    result = group.measure(1)
    assert result["observed_seconds"] == 11
    assert result["active_seconds"] == 1


def test_recent_window_and_weighted_rank_use_exact_end():
    group = DurationGroup(Cells([(0, 600, 20)]))
    assert group.recent_start == 480
    assert group.measure(1, recent=True)["active_seconds"] == 120
    assert group.weighted(1) == (300, 300)


def test_timed_full_cell_weight_does_not_skip_its_on_delay():
    group = DurationGroup(Cells([(0, 600, 20)]), {"timeout": 2, "on_delay": 1, "off_delay": 3})
    assert group.weighted(1) == (300, 299.5)


def test_overlapping_cells_are_rejected_instead_of_double_counted():
    with pytest.raises(ValueError, match="overlap"):
        DurationGroup(Cells([(0, 2, 20), (1, 3, 20)]))


def test_mask_replay_matches_complete_physical_replay_on_random_traces():
    import random

    from tuner_under_test.calibration.event_replay import replay as physical_replay
    from tuner_under_test.history.spans import Span

    rng = random.Random(51)
    for _ in range(40):
        source, bits, physical, start = [], 0, [], 0
        for i in range(20):
            end = start + rng.choice((0.125, 0.5, 1, 30))
            energy = rng.choice((0, 20))
            source.append((start, end, energy))
            physical.append(Span(start, end, (energy,) + (None,) * 17, "event"))
            bits |= (energy > 10) << i
            start = end + rng.choice((0, 0, 1))
        timing = {"timeout": 1, "on_delay": 0.25, "off_delay": 0.5}
        group = DurationGroup(Cells(source), timing)
        oracle = physical_replay(physical, {"g0_move": 10}, timing)
        result = replay.from_bits(group, group, bits)
        assert result["coverage"] == list(oracle.coverage)
        assert result["lower"] == result["upper"] == list(oracle.active)


def test_timing_projection_uses_precise_boundaries_without_sample_uncertainty():
    from tuner_under_test.calibration.timing import GroupTiming

    rows = Cells([(0, 1, 20), (1, 2, 0), (2, 3, 0)])
    group = GroupTiming(rows, {"timeout": 1, "on_delay": 0, "off_delay": 0})
    assert group.project(1) == 3
    assert group.eligible == 7


@pytest.mark.parametrize("intervals", [[(0, 0, 20)], [(0, float("inf"), 20)]])
def test_invalid_physical_cells_are_not_recording_coverage(intervals):
    with pytest.raises(ValueError):
        DurationGroup(Cells(intervals))
