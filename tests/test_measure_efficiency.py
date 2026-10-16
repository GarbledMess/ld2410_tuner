"""Measuring a pattern intersects the labelled regions with coverage once, not per region."""

from types import SimpleNamespace

import test_tuner  # noqa: F401
from tuner_under_test.calibration import event_fitting
from tuner_under_test.rooms import intervals


def objective(regions, coverage):
    radar = SimpleNamespace(
        bits=lambda thresholds: thresholds,
        project=lambda bits: {"lower": ([0], [10_000]), "upper": ([5], [7])},
        projector=SimpleNamespace(coverage=coverage),
    )
    return SimpleNamespace(
        device_id="radar",
        radars={"radar": radar},
        regions=regions,
        reference=False,
        totals={("human", "present"): 1, ("human", "not_present"): 1},
    )


def plain(regions, coverage, start, end):
    """The definition: each region's covered part, joined per state."""
    found = {"present": [], "not_present": []}
    for low, high, _values, state, origin, _confidence in regions:
        if origin == "human":
            found[state].extend(intervals.intersect(coverage, [(max(start, low), min(end, high))]))
    return {state: intervals.union(items) for state, items in found.items()}


def test_source_totals_equal_the_per_region_definition_without_a_pass_per_region(monkeypatch):
    regions = [
        (i * 10, i * 10 + 7, None, "present" if i % 3 else "not_present", "human", 1)
        for i in range(2_000)
    ]
    regions.append((5, 30, None, "present", "automatic", 0.8))
    coverage = [(i * 25, i * 25 + 20) for i in range(800)]
    expected = plain(regions, coverage, 3, 19_000)
    calls = []
    real = event_fitting.intersect
    monkeypatch.setattr(event_fitting, "intersect", lambda *args: calls.append(1) or real(*args))
    target, hits = event_fitting._source_totals(
        objective(regions, coverage), {"g0_move": 1}, "human", 3, 19_000, True
    )
    assert target == expected
    assert hits["present"] == intervals.intersect(expected["present"], [(0, 10_000)])
    assert len(calls) <= 4  # Per state: coverage once, hits once.


def test_source_rates_integrate_each_span_list_once_with_identical_results(monkeypatch):
    import random

    from tuner_under_test.calibration.intervals import WeightedTime
    from tuner_under_test.rooms import objective as room_objective

    rng = random.Random(4)
    times = sorted(rng.uniform(0, 1000) for _ in range(300))
    mass = {
        (source, state): WeightedTime(times, [rng.random() for _ in times])
        for source in room_objective.SOURCES
        for state in ("present", "not_present")
    }
    found = SimpleNamespace(mass=mass, totals=dict.fromkeys(mass, 100.0))

    def spans():
        edges = sorted(rng.uniform(0, 1000) for _ in range(80))
        return edges[0::2], edges[1::2]

    detected, false, automatic = spans(), spans(), spans()
    plain = {
        source: (
            1
            - room_objective.across(
                [mass[source, "present"]], *(automatic if source == "automatic" else detected)
            )[0]
            / 100.0,
            room_objective.across([mass[source, "not_present"]], *false)[0] / 100.0,
        )
        for source in room_objective.SOURCES
    }
    calls = []
    real = room_objective.across
    monkeypatch.setattr(room_objective, "across", lambda *a: calls.append(1) or real(*a))
    rates = room_objective.Objective._source_rates(found, detected, false, automatic)
    assert rates == plain
    assert len(calls) == 3  # Detected, automatic and the shared false-active spans.
    found.totals["automatic", "present"] = 0  # No such mass: its rate is 0, not a full miss.
    assert room_objective.Objective._source_rates(found, detected, false, automatic)[
        "automatic"
    ] == (0, plain["automatic"][1])


def test_longest_inactive_and_episode_pieces_equal_the_per_interval_definition(monkeypatch):
    import random

    rng = random.Random(7)

    def disjoint(count):
        edges = sorted(rng.uniform(0, 5000) for _ in range(2 * count))
        return intervals.union(list(zip(edges[0::2], edges[1::2], strict=True)))

    target, hits = disjoint(300), disjoint(700)
    longest = 0.0
    for start, end in target:  # The definition: each target's longest stretch without a hit.
        active = intervals.intersect(hits, [(start, end)])
        bounds = [start, *(edge for item in active for edge in item), end]
        longest = max(longest, *(b - a for a, b in zip(bounds[::2], bounds[1::2], strict=True)))
    episodes = [SimpleNamespace(state="present", start=a, end=b) for a, b in disjoint(40)]
    pieces = [p for ep in episodes if (p := intervals.intersect(target, [(ep.start, ep.end)]))]
    monkeypatch.setattr(
        event_fitting, "intersect", lambda *a: (_ for _ in ()).throw(AssertionError)
    )
    assert event_fitting._longest_inactive(target, hits) == longest
    context = SimpleNamespace(episodes=tuple(episodes), automatic_episodes=())
    found = SimpleNamespace(device_id="radar", quality={"radar": SimpleNamespace(context=context)})
    assert event_fitting._parent_coverage(found, target, "human") == pieces
