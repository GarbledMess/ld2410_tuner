"""Independent observed episodes and one shared chronological partition."""

import pytest
import test_tuner  # noqa: F401


def test_adjacent_rows_are_one_vote():
    from tuner_under_test.calibration.episodes import episodes

    for times in (range(100), [0, 99]):
        result = episodes([(float(t), {}, "present") for t in times], owner=("renamed",), gap=100)
        assert len(result) == 1
        assert (result[0].start, result[0].end, result[0].owner) == (0, 99, ("renamed",))


def test_midpoint_cells_opposite_unknown_and_gaps():
    from tuner_under_test.calibration.episodes import episodes

    rows = [
        (t, {}, s)
        for t, s in (
            (0.25, "present"),
            (1.25, "present"),
            (2.25, "not_present"),
            (3.25, None),
            (4.25, "present"),
            (20.25, "present"),
            (21.25, "present"),
        )
    ]
    result = episodes(rows, owner=("any",), gap=2)
    assert [(e.start, e.end, e.state) for e in result] == [
        (0.25, 1.75, "present"),
        (1.75, 2.75, "not_present"),
        (3.75, 4.25, "present"),
        (20.25, 21.25, "present"),
    ]


def test_exclusion_between_samples_splits_without_inventing_votes():
    from tuner_under_test.calibration.episodes import episodes

    rows = [(t, {}, "present") for t in (0, 1, 2, 3)]
    assert [
        (e.start, e.end)
        for e in episodes(rows, owner=("a", "b"), gap=2, exclusions=((1.25, 1.75),))
    ] == [(0, 1.25), (1.75, 3)]


def test_unordered_duplicates_are_deterministic_and_conflicts_are_barriers():
    from tuner_under_test.calibration.episodes import episodes

    rows = [(0, {}, "present"), (1, {}, "present"), (1, {}, "not_present"), (2, {}, "present")]
    expected = [(0, 0.5), (1.5, 2)]
    for source in (rows, rows[::-1]):
        assert [(e.start, e.end) for e in episodes(source, owner=("x",), gap=2)] == expected
    result = episodes([(0, {}, "present"), (0, {}, "present")], owner=("x",), gap=2)
    assert len(result) == 1 and result[0].start == result[0].end == 0


@pytest.mark.parametrize("time", [float("nan"), float("inf"), "bad"])
def test_invalid_times_are_rejected(time):
    from tuner_under_test.calibration.episodes import episodes

    with pytest.raises(ValueError):
        episodes([(time, {}, "present")], owner=("x",), gap=2)


def test_chronological_split_shared_cutoff_and_missing_class():
    from tuner_under_test.calibration.episodes import Episode, chronological_split

    items = tuple(
        Episode(t, t + 5, state, ("x",))
        for t, state in (
            (0, "present"),
            (10, "not_present"),
            (20, "present"),
            (30, "not_present"),
            (40, "present"),
            (50, "not_present"),
        )
    )
    assert chronological_split(items[::-1]) == 40
    assert chronological_split(items[:2]) is None
    assert chronological_split(tuple(e for e in items if e.state == "present")) is None
    assert chronological_split(()) is None
    overlapping = items + (Episode(32, 45, "present", ("another",)),)
    assert chronological_split(overlapping) == 30
    assert chronological_split(items + (Episode(0, 60, "present", ("blocking",)),)) is None


@pytest.mark.parametrize("gap", [-1, float("inf")])
def test_invalid_gap_is_rejected(gap):
    from tuner_under_test.calibration.episodes import episodes

    with pytest.raises(ValueError):
        episodes([], owner=("x",), gap=gap)


def test_exclusion_clipping_empty_and_invalid_intervals():
    from tuner_under_test.calibration.episodes import Episode, chronological_split, episodes

    assert episodes([], owner=("x",), gap=2) == ()
    rows = [(0, {}, "present"), (2, {}, "present")]
    assert episodes(rows, owner=("x",), gap=2, exclusions=((-1, 3),)) == ()
    assert episodes(rows, owner=("x",), gap=2, exclusions=((-3, -2), (3, 4))) == episodes(
        rows, owner=("x",), gap=2
    )
    with pytest.raises(ValueError):
        episodes(rows, owner=("x",), gap=2, exclusions=((2, 1),))
    with pytest.raises(ValueError):
        chronological_split((Episode(2, 1, "present", ("x",)),))


def test_split_earliest_tie_and_frozen_owner():
    from dataclasses import FrozenInstanceError

    from tuner_under_test.calibration.episodes import Episode, chronological_split

    items = tuple(
        Episode(
            t, t + (1.5 if t == 11 else 0.5), "present" if t % 2 == 0 else "not_present", ("x",)
        )
        for t in range(15)
    )
    # The 80% target is 12; its crossing episode leaves 11 and 13 equally close.
    assert chronological_split(items) == 11
    with pytest.raises(FrozenInstanceError):
        items[0].state = "not_present"


def test_half_open_exclusions_ignore_empty_intervals_and_keep_endpoints():
    from tuner_under_test.calibration.episodes import episodes

    rows = [(t, {}, "present") for t in (0, 1, 2)]
    original = episodes(rows, owner=("x",), gap=2)
    assert episodes(rows, owner=("x",), gap=2, exclusions=((1, 1),)) == original
    singleton = [(2, {}, "present")]
    assert episodes(singleton, owner=("x",), gap=2, exclusions=((2, 2),)) == episodes(
        singleton, owner=("x",), gap=2
    )
    assert episodes(singleton, owner=("x",), gap=2, exclusions=((1, 2),)) == episodes(
        singleton, owner=("x",), gap=2
    )
    assert episodes(singleton, owner=("x",), gap=2, exclusions=((2, 3),)) == ()
