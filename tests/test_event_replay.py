"""Physical event replay: no truth labels or acquisition cadence assumptions."""

import random

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history.spans import Span


def span(start, end, energy, *, other=0, mode="event"):
    return Span(start, end, (energy, other, *([0] * 16)), mode)


def replay(items, timing=None, **kwargs):
    from tuner_under_test.calibration.event_replay import replay as run

    return run(items, {"g0_move": 10}, timing, **kwargs)


def test_subsecond_pulse_is_exact_with_known_zero_hold():
    result = replay(
        [span(100, 100.125, 0), span(100.125, 100.175, 10.5), span(100.175, 101, 0)],
        {"timeout": 0, "on_delay": 0, "off_delay": 0},
    )
    assert result.coverage == ((100, 101),)
    assert result.active == ((100.125, 100.175),)
    assert result.basis == "physical_span_replay"


def test_strict_threshold_and_stable_600_second_single_vector():
    assert replay([span(0, 600, 10)]).active == ()
    assert replay([span(0, 600, 11)]).active == ((0, 600),)
    assert replay([span(0, 600, 11)], {"timeout": 2, "on_delay": 3, "off_delay": 4}).active == (
        (3, 600),
    )


def test_duplicate_unchanged_boundaries_do_not_change_replay():
    whole = replay([span(0, 600, 20)], {"timeout": 2, "on_delay": 3, "off_delay": 4})
    split = replay(
        [span(i, i + 1, 20) for i in range(600)], {"timeout": 2, "on_delay": 3, "off_delay": 4}
    )
    assert whole == split


def test_unknown_timeout_ignores_filters_and_query_clips_after_replay():
    source = [span(0, 20, 20)]
    assert replay(source, {"timeout": None, "on_delay": 100, "off_delay": 100}).active == ((0, 20),)
    assert replay(
        source, {"timeout": 0, "on_delay": 5, "off_delay": 0}, start=7, end=12
    ).active == ((7, 12),)
    assert replay(
        source, {"timeout": 0, "on_delay": 5, "off_delay": 0}, start=2, end=12
    ).active == ((5, 12),)


def test_hold_and_off_never_extend_beyond_coverage_or_bridge_gap():
    source = [span(0, 1, 20), span(1, 2, 0), span(10, 11, 0), span(11, 12, 20)]
    result = replay(source, {"timeout": 100, "on_delay": 0, "off_delay": 100})
    assert result.coverage == ((0, 2), (10, 12))
    assert result.active == ((0, 2), (11, 12))


def test_unavailable_required_gate_resets_timers_and_unselected_gate_does_not():
    from tuner_under_test.calibration.event_replay import replay as run

    source = [span(0, 2, 20), span(2, 4, None), span(4, 10, 20)]
    result = replay(source, {"timeout": 0, "on_delay": 3, "off_delay": 0})
    assert result.coverage == ((0, 2), (4, 10))
    assert result.active == ((7, 10),)
    assert replay([span(0, 5, 20, other=None)]).active == ((0, 5),)
    assert run([span(0, 5, 20, other=None)], {"g0_move": 10, "g0_still": 100}).coverage == ()


def test_on_delay_requires_uninterrupted_hardware_and_hold_can_bridge_hit_dip():
    source = [span(0, 1, 20), span(1, 1.5, 0), span(1.5, 3, 20), span(3, 8, 0)]
    assert replay(source, {"timeout": 0, "on_delay": 2, "off_delay": 0}).active == ()
    assert replay(source, {"timeout": 1, "on_delay": 2, "off_delay": 0}).active == ((2, 4),)


def test_off_delay_is_cancelled_by_short_hardware_rise_while_active():
    source = [span(0, 3, 20), span(3, 4, 0), span(4, 4.25, 20), span(4.25, 8, 0)]
    result = replay(source, {"timeout": 0, "on_delay": 2, "off_delay": 2})
    assert result.active == ((2, 6.25),)


def test_on_boundary_equal_to_hardware_end_retains_off_delay():
    source = [span(0, 2, 20), span(2, 5, 0)]
    assert replay(source, {"timeout": 0, "on_delay": 2, "off_delay": 1}).active == ((2, 3),)
    assert replay(source, {"timeout": 0, "on_delay": 2, "off_delay": 0}).active == ()


def test_empty_and_clipped_outside_windows():
    assert replay([]).coverage == ()
    assert replay([]).active == ()
    assert replay([span(0, 1, 20)], start=2, end=3).active == ()
    assert replay([span(0, 1, 20)], start=0.5, end=0.5).coverage == ()


@pytest.mark.parametrize(
    "source",
    [
        [span(1, 0, 20)],
        [span(0, 2, 20), span(1, 3, 0)],
        [span(float("nan"), 1, 20)],
        [span(0, float("inf"), 20)],
        [span(0, 1, float("nan"))],
    ],
)
def test_malformed_and_overlapping_physical_spans_raise(source):
    with pytest.raises(ValueError):
        replay(source)


@pytest.mark.parametrize(
    "timing",
    [
        {"timeout": -1},
        {"timeout": float("inf")},
        {"timeout": 0, "on_delay": -1, "off_delay": 0},
        {"timeout": 0, "on_delay": 0, "off_delay": float("nan")},
    ],
)
def test_invalid_known_timing_raises(timing):
    with pytest.raises(ValueError):
        replay([span(0, 1, 20)], timing)


def test_invalid_pattern_and_window_raise():
    from tuner_under_test.calibration.event_replay import replay as run

    for pattern in ({"arbitrary_gate": 10}, {"g0_move": float("nan")}):
        with pytest.raises(ValueError):
            run([], pattern)
    for window in ({"start": 2, "end": 1}, {"start": float("nan")}):
        with pytest.raises(ValueError):
            replay([], **window)


def scalar_trace(energies, hold, on, off, step=0.125):
    active, on_start, off_start, expiry = False, None, None, -1.0
    intervals = []
    for index, energy in enumerate(energies):
        timestamp = index * step
        if energy > 10:
            expiry = timestamp + step + hold
        hardware = energy > 10 or timestamp < expiry
        if hardware:
            off_start = None
            on_start = timestamp if on_start is None else on_start
        else:
            on_start = None
            off_start = timestamp if off_start is None else off_start
        if not active and hardware and timestamp - on_start >= on:
            active = True
        if active and not hardware and timestamp - off_start >= off:
            active = False
        if active:
            if intervals and intervals[-1][1] == timestamp:
                intervals[-1] = (intervals[-1][0], timestamp + step)
            else:
                intervals.append((timestamp, timestamp + step))
    return tuple(intervals)


def test_random_grid_trace_matches_independent_scalar_clock_oracle():
    rng = random.Random(813)
    for _ in range(20):
        energies = [20 if rng.random() < 0.35 else 0 for _ in range(80)]
        hold, on, off = (rng.choice((0, 0.125, 0.375)) for _ in range(3))
        # Avoid the separate equal-end timer race case; this oracle ticks after energy edges.
        on = 0
        result = replay(
            [span(i / 8, (i + 1) / 8, value) for i, value in enumerate(energies)],
            {"timeout": hold, "on_delay": on, "off_delay": off},
        )
        assert result.active == scalar_trace(energies, hold, on, off)


def test_all_standard_gates_are_available_without_package_metadata():
    from tuner_under_test.calibration.event_replay import replay as run

    values = tuple(20 for _ in HISTORY_KEYS)
    result = run([Span(0, 1, values, "sampled")], dict.fromkeys(HISTORY_KEYS, 10))
    assert result.active == ((0, 1),)
    assert result.modes == ("sampled",)


def test_nonzero_on_off_grid_trace_matches_scalar_clock_without_equal_end_race():
    rng = random.Random(217)
    for _ in range(30):
        energies = [20 if rng.random() < 0.45 else 0 for _ in range(80)]
        hold, off = (rng.choice((0, 0.125, 0.375)) for _ in range(2))
        on = rng.choice((0.1875, 0.4375))
        source = [span(i / 8, (i + 1) / 8, value) for i, value in enumerate(energies)]
        result = replay(source, {"timeout": hold, "on_delay": on, "off_delay": off})
        twice = [value for energy in energies for value in (energy, energy)]
        assert result.active == scalar_trace(twice, hold, on, off, step=0.0625)


def test_partial_unknown_filters_retain_known_hardware_hold():
    source = [span(0, 1, 20), span(1, 5, 0)]
    assert replay(source, {"timeout": 1, "on_delay": None, "off_delay": 9}).active == ((0, 2),)


@pytest.mark.parametrize(
    "source",
    [[Span(0, 1, (20,), "event")], [span(0, 1, "bad")], [span(None, 1, 20)], [span(0, 1, 101)]],
)
def test_invalid_span_types_layout_and_ranges_raise_value_error(source):
    with pytest.raises(ValueError):
        replay(source)


def test_zero_duration_transition_does_not_manufacture_activity():
    assert replay([span(0, 0, 20), span(0, 1, 0)]).active == ()
