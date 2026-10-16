"""Real folds revisit thousands of hit masks; ranking must reuse their duration replays."""

import random
from copy import deepcopy
from types import SimpleNamespace

import old_search
import test_tuner as harness
from tuner_under_test.calibration import event_fitting
from tuner_under_test.calibration.duration import (
    DurationGroup,
    DurationReplay,
    _add_window,
    _clip,
    _confidence_time,
)
from tuner_under_test.calibration.intervals import WeightedTime
from tuner_under_test.rooms import fitting


def search_scene(seed, weight=0.5):
    rng = random.Random(seed)
    groups = []
    for number, label in enumerate(("present", "not_present", "present", "not_present")):
        groups.append(
            [
                (
                    number * 700 + i * 6,
                    {"g0_move": rng.choice((0, 20, 60)), "g0_still": rng.choice((0, 30, 80))},
                    label,
                    rng.choice((0.7, 0.9, 1.0)),
                )
                for i in range(100)
            ]
        )
    view = SimpleNamespace(data={"devices": {"d": {}}, "room_snapshot": None})
    harness.record_samples(
        view,
        "d",
        [row[:3] for group in groups[:2] for row in group],
        [row for group in groups[2:] for row in group],
    )
    return event_fitting._objective(
        view,
        "d",
        ["g0_move", "g0_still"],
        {"g0_move": 25, "g0_still": 35},
        {"timeout": 5, "on_delay": 1, "off_delay": 1},
        weight,
        {},
        0,
        view._history_until,
    )


def count_ranks(search):
    search.ranked = 0
    original = search.rank

    def counted(*args, **kwargs):
        search.ranked += 1
        return original(*args, **kwargs)

    search.rank = counted
    return search


def exhaustive_search(objective):
    candidates = {
        (device, key): list(radar.allowed[key])
        for device, radar in objective.radars.items()
        for key in radar.current
    }
    best = deepcopy(objective.current)
    quiet = {device: dict.fromkeys(gates, 100) for device, gates in best.items()}
    for seed in (objective.current, objective.preferred, quiet):
        candidate = deepcopy(seed)
        for _ in range(4):
            if not old_search.sweep(objective, candidate, candidates):
                break
        if objective.rank(candidate) < objective.rank(best):
            best = candidate
    return best


def test_gate_search_omits_dominated_equal_hit_masks_without_changing_the_answer():
    fast = count_ranks(search_scene(9021))
    plain = count_ranks(search_scene(9021))
    assert fitting.search(fast) == exhaustive_search(plain)
    assert fast.ranked < plain.ranked / 2


def test_deduplicated_gate_sweeps_match_exhaustive_search_at_every_weight():
    for seed in range(4):
        for weight in (0, 0.5, 1):
            assert fitting.search(search_scene(9050 + seed, weight)) == exhaustive_search(
                search_scene(9050 + seed, weight)
            )


def test_confidence_endpoints_are_integrated_once_across_candidate_intervals():
    confidence = _confidence_time(rows(500, 0, "present", 0.83))
    intervals = [(index * 6 + 0.25, index * 6 + 4.75) for index in range(400)]
    first = [confidence.between(start, end) for start, end in intervals]
    misses = confidence.at.cache_info().misses
    assert [confidence.between(start, end) for start, end in intervals] == first
    assert confidence.at.cache_info().misses == misses


def test_remembered_endpoint_integrals_are_byte_identical_to_plain_prefix_integrals():
    rng = random.Random(8160)
    observed = [(index * 6 + rng.random(), {}, "present", rng.random()) for index in range(500)]
    confidence = _confidence_time(observed)
    plain = WeightedTime(confidence.times, confidence.weights)
    for _ in range(300):
        first, last = sorted((rng.uniform(-5, 3005), rng.uniform(-5, 3005)))
        assert confidence.between(first, last) == plain.between(first, last)


def rows(count, start, state, confidence=None):
    return [
        (
            start + index * 6,
            {"g0_move": index % 101},
            state,
            confidence if confidence is not None else 1.0,
        )
        for index in range(count)
    ]


def test_revisiting_rank_after_a_large_gate_sweep_needs_no_duration_replay():
    replay = DurationReplay(rows(3000, 0, "present"), rows(3000, 20000, "not_present"))
    first = replay.rank(1, 1)
    for index in range(1, 2500):
        replay.rank(1 << index, 1 << index)
    misses = (replay.present.measure.cache_info().misses, replay.absent.measure.cache_info().misses)
    assert replay.rank(1, 1) == first
    assert misses == (
        replay.present.measure.cache_info().misses,
        replay.absent.measure.cache_info().misses,
    )


def test_revisiting_automatic_rank_needs_no_duration_replay():
    replay = DurationReplay(rows(3000, 0, "present", 0.83), rows(3000, 20000, "not_present", 0.91))
    first = replay.automatic_rank(1, 1)
    for index in range(1, 400):
        replay.automatic_rank(1 << index, 1 << index)
    for index in range(2500):
        replay.rank(1 << index, 1 << index)
    misses = (
        replay.present.weighted.cache_info().misses,
        replay.absent.weighted.cache_info().misses,
    )
    assert replay.automatic_rank(1, 1) == first
    assert misses == (
        replay.present.weighted.cache_info().misses,
        replay.absent.weighted.cache_info().misses,
    )


def test_new_mask_pairs_reuse_each_class_after_a_full_gate_sweep():
    replay = DurationReplay(rows(500, 0, "present"), rows(500, 4000, "not_present"))
    for index in range(400):
        replay.rank(1 << index, 1 << index)
    positive_misses = replay.present.measure.cache_info().misses
    replay.rank(1, 1 << 450)  # A new pair with an already-scored positive mask.
    assert replay.present.measure.cache_info().misses == positive_misses
    negative_misses = replay.absent.measure.cache_info().misses
    replay.rank(1 << 451, 1)  # A new pair with an already-scored negative mask.
    assert replay.absent.measure.cache_info().misses == negative_misses


def test_changing_one_session_reuses_the_other_sessions_interval_contribution():
    group = DurationGroup(rows(100, 0, "present", 0.83) + rows(100, 10000, "present", 0.91))
    first = group.measure(1 << 10)
    confidence = group.windows[0][2]
    info = confidence.at.cache_info()
    calls = info.hits + info.misses
    second = group.measure((1 << 10) | (1 << 110))
    info = confidence.at.cache_info()
    assert info.hits + info.misses == calls
    assert second["observed_seconds"] == first["observed_seconds"]
    assert second["active_seconds"] == 2 * first["active_seconds"]


def test_wholly_unscored_sessions_need_no_candidate_contribution_walk():
    observed = [row for block in range(100) for row in rows(3, block * 10000, "present", 0.83)]
    group = DurationGroup(observed, {"timeout": 20, "on_delay": 1, "off_delay": 1})
    original = group.part
    visited = []

    def counted(*args):
        visited.append(args[0])
        return original(*args)

    group.part = counted
    for recent in (False, True):
        for latest in (False, True):
            for mask in (0, 1, (1 << len(observed)) - 1):
                assert group.measure(mask, recent, latest) == plain_measure(
                    group, mask, recent, latest
                )
    assert visited == []


def test_human_summary_keeps_exact_fields_without_integrating_unused_confidence():
    positive = rows(100, 1_800_000_000, "present", 0.83)
    negative = rows(100, 1_800_010_000, "not_present", 0.91)
    timing = {"timeout": 7, "on_delay": 1.5, "off_delay": 2.5}
    replay, plain = (DurationReplay(positive, negative, timing) for _ in range(2))
    visits = []
    for group in (plain.present, plain.absent):
        group.measure = lambda bits, recent=False, latest=False, group=group, **kwargs: (
            plain_measure(group, bits, recent, latest)
        )
    for group in (replay.present, replay.absent):
        for _, _, confidence in group.windows:
            original = confidence.between

            def counted(start, end, original=original):
                visits.append((start, end))
                return original(start, end)

            confidence.between = counted
    rng = random.Random(8164)
    for _ in range(20):
        positive_mask, negative_mask = rng.getrandbits(100), rng.getrandbits(100)
        for recent in (False, True):
            assert replay.summary(positive_mask, negative_mask, recent) == plain.summary(
                positive_mask, negative_mask, recent
            )
    assert visits == []
    replay.automatic_rank(positive_mask, negative_mask)
    assert visits

    def unused(_timestamp):
        raise AssertionError("human duration ranks must not integrate confidence")

    for group in (replay.present, replay.absent):
        for _, _, confidence in group.windows:
            confidence.at = unused
    assert replay.rank(positive_mask, negative_mask) == plain.rank(positive_mask, negative_mask)


def test_weighted_candidate_summary_avoids_unused_duration_fields_and_matches_full_replay():
    rng = random.Random(8196)
    observed = [
        (
            1_800_000_000 + block * 10000 + index * 6 + rng.random(),
            {"g0_move": 20},
            "present",
            rng.random(),
        )
        for block in range(3)
        for index in range(100)
    ]
    for timing in (
        {},
        {"timeout": 0},
        {"timeout": 7, "on_delay": 1.5, "off_delay": 2.5},
        {"timeout": 6000, "on_delay": 6000, "off_delay": 0},
        {"timeout": 7, "on_delay": 1.5, "off_delay": -2.5},
    ):
        for absent in (False, True):
            group = DurationGroup(observed, timing, absent=absent)
            for _ in range(20):
                mask = rng.getrandbits(len(observed))
                for latest in (False, True):
                    full = plain_measure(group, mask, False, latest)
                    assert group.weighted(mask, latest) == (
                        full["weighted_seconds"],
                        full["weighted_active_seconds"],
                    )
            assert group.measure.cache_info().misses == 0
            assert group.part.cache_info().misses == 0


def test_weighted_empty_and_full_session_masks_need_no_interval_replay():
    observed = rows(100, 1_800_000_000, "present", 0.83) + rows(100, 1_800_010_000, "present", 0.91)
    for timing in ({}, {"timeout": 0}, {"timeout": 7, "on_delay": 1.5, "off_delay": 2.5}):
        for absent in (False, True):
            group = DurationGroup(observed, timing, absent=absent)
            expected = {
                (mask, latest): plain_measure(group, mask, False, latest)
                for mask in (0, (1 << len(observed)) - 1)
                for latest in (False, True)
            }
            total = group.weighted_seconds

            def unused(*_args):
                raise AssertionError("constant session masks must not replay candidate intervals")

            group._intervals = unused
            for (mask, latest), full in expected.items():
                assert group.weighted(mask, latest) == (total, full["weighted_active_seconds"])


def plain_measure(group, bits, recent, latest):
    """Frozen pre-cache global measurement, adding each window directly into the total."""
    result = dict.fromkeys(
        (
            "observed_seconds",
            "active_seconds",
            "weighted_seconds",
            "weighted_active_seconds",
            "longest_inactive_seconds",
        ),
        0.0,
    )
    result.update(dict.fromkeys(("episodes", "missed_episodes", "events", "unscored_samples"), 0))
    for offset, window, confidence in group.windows:
        if recent and window.times[-1] < group.recent_start:
            continue
        start = window.times[0] + max(window.hold + window.off, window.on)
        if recent:
            start = max(start, group.recent_start)
        end = window.times[-1]
        if end <= start:
            result["unscored_samples"] += len(window.times)
            continue
        intervals = _clip(
            group._intervals(window, (bits >> offset) & window.mask, latest), start, end
        )
        _add_window(result, start, end, intervals, confidence)
    return result


def test_session_contributions_match_full_replay_with_fractional_times_weights_and_warmup():
    rng = random.Random(8190)
    observed = [
        (
            1_800_000_000 + block * 10000 + index * 6 + rng.random(),
            {"g0_move": 20},
            "present",
            rng.random(),
        )
        for block in range(3)
        for index in range(100)
    ]
    modes = (
        {},
        {"timeout": 5},
        {"timeout": 7, "on_delay": 1.5, "off_delay": 2.5},
        {"timeout": 6000, "on_delay": 6000, "off_delay": 0},
    )
    for timing in modes:
        for absent in (False, True):
            group = DurationGroup(observed, timing, absent=absent)
            for _ in range(12):
                mask = rng.getrandbits(len(observed))
                for recent in (False, True):
                    for latest in (False, True):
                        assert group.measure(mask, recent, latest) == plain_measure(
                            group, mask, recent, latest
                        )


def test_remembered_ranks_are_the_uncached_ranks_for_every_timing_mode():
    rng = random.Random(8150)
    for timing in ({}, {"timeout": 5}, {"timeout": 7, "on_delay": 1.5, "off_delay": 2.5}):
        replay = DurationReplay(
            rows(60, 0, "present", 0.83), rows(60, 600, "not_present", 0.91), timing
        )
        for _ in range(80):
            positive, negative = rng.getrandbits(60), rng.getrandbits(60)
            assert replay.rank(positive, negative) == DurationReplay.rank(
                replay, positive, negative
            )
            assert replay.automatic_rank(positive, negative) == DurationReplay.automatic_rank(
                replay, positive, negative
            )


def test_across_is_byte_identical_to_summing_between_per_span():
    from tuner_under_test.calibration.intervals import across

    rng = random.Random(8161)
    times = sorted(rng.uniform(0, 3000) for _ in range(400))
    masses = [WeightedTime(times, [rng.random() for _ in times]) for _ in range(2)]
    spans = sorted(sorted((rng.uniform(-50, 3050), rng.uniform(-50, 3050))) for _ in range(300))
    starts, ends = [a for a, _b in spans], [b for _a, b in spans]
    found = across(masses, starts, ends)
    for mass, total in zip(masses, found, strict=True):
        assert total == sum([mass.between(a, b) for a, b in spans])
    assert across(masses, [], []) == [0, 0]
