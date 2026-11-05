"""Untimed room bundles reuse the already evaluated human variant exactly."""

from copy import deepcopy
from dataclasses import replace

import pytest
import test_tuner  # noqa: F401
from test_hybrid_reports import problem as exact_problem
from test_hybrid_search import problem as cell_problem
from tuner_under_test.engine.hybrid import fixed, room_bundles


@pytest.mark.parametrize("mode", ["exact", "cells"])
@pytest.mark.parametrize("timing", [{}, {"timeout": 0}, {"timeout": 7}])
def test_untimed_room_raw_scores_reuse_human_metrics(tmp_path, mode, timing):
    problem = (
        exact_problem() if mode == "exact" else cell_problem({"r": [(70,), (10,), (20,), (60,)]})
    )
    problem = replace(problem, radars={"r": replace(problem.radars["r"], timing=timing)})
    patterns = [{"r": {0: 15}}, {"r": {0: 50}}, {"r": {0: 80}}]
    zone = problem.zones[0]
    with fixed.FixedScores(problem, len(patterns), directory=tmp_path) as scores:
        assert scores.mode == mode
        original, calls = scores.scores, []

        def counted(*args, **kwargs):
            calls.append(kwargs.get("raw", False))
            return original(*args, **kwargs)

        scores.scores = counted
        actual = room_bundles.bundles(scores, zone, patterns, None)
        expected = original(patterns, [fixed.source(zone, "human")], raw=True)
    for (bundle, _whole), raw in zip(actual, expected, strict=True):
        assert bundle["raw"] == raw[fixed.source(zone, "human").group_id]
        before = deepcopy(bundle["human"])
        bundle["raw"]["exclusive_seconds"]["r"] = -1
        assert bundle["human"] == before
    assert calls.count(True) == int(bool(timing))


@pytest.mark.parametrize("mode", ["exact", "cells"])
@pytest.mark.parametrize("timing", [{}, {"timeout": 7}])
def test_room_variants_with_identical_inputs_are_scored_once(tmp_path, mode, timing):
    problem = (
        exact_problem() if mode == "exact" else cell_problem({"r": [(70,), (10,), (20,), (60,)]})
    )
    problem = replace(problem, radars={"r": replace(problem.radars["r"], timing=timing)})
    patterns = [{"r": {0: 15}}, {"r": {0: 50}}, {"r": {0: 80}}]
    zone = problem.zones[0]
    base, members = room_bundles.variants(zone, None)
    variants = [*base.values(), *(z for found in members.values() for z in found.values())]
    with fixed.FixedScores(problem, len(patterns), directory=tmp_path) as scores:
        expected = scores.scores(patterns, variants)
        original, batches = scores.scores, []

        def counted(patterns, zones, **kwargs):
            batches.append(list(zones))
            return original(patterns, zones, **kwargs)

        scores.scores = counted
        actual = room_bundles.bundles(scores, zone, patterns, None)
    for (bundle, whole), wanted in zip(actual, expected, strict=True):
        assert whole == wanted[zone.group_id]
        for name in ("human", "automatic", "recent"):
            assert bundle[name] == wanted[base[name].group_id]
        assert bundle["members"]["r"]["metrics"] == wanted[members["r"]["metrics"].group_id]
        before = deepcopy(whole)
        bundle["human"]["exclusive_seconds"]["r"] = -1
        assert whole == before
    keys = [(z.members, z.truth) for z in batches[0]]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("mode", ["exact", "cells"])
@pytest.mark.parametrize("timing", [{}, {"timeout": 7}])
def test_identical_room_variants_are_scored_once(tmp_path, mode, timing):
    problem = (
        exact_problem() if mode == "exact" else cell_problem({"r": [(70,), (10,), (20,), (60,)]})
    )
    problem = replace(problem, radars={"r": replace(problem.radars["r"], timing=timing)})
    zone = problem.zones[0]
    patterns = [{"r": {0: 15}}, {"r": {0: 50}}, {"r": {0: 80}}]
    base, members = room_bundles.variants(zone, None)
    every = [*base.values(), *(z for found in members.values() for z in found.values())]
    with fixed.FixedScores(problem, len(patterns), directory=tmp_path) as scores:
        original, requested = scores.scores, []
        expected = original(patterns, every)

        def counted(patterns, zones, **kwargs):
            requested.append(list(zones))
            return original(patterns, zones, **kwargs)

        scores.scores = counted
        actual = room_bundles.bundles(scores, zone, patterns, None)
    keys = [(z.members, z.truth) for z in requested[0]]
    assert len(keys) == len(set(keys))
    assert len(keys) < len(every)
    for (bundle, whole), found in zip(actual, expected, strict=True):
        assert whole == found[zone.group_id]
        for name, variant in base.items():
            if name != "all":
                assert bundle[name] == found[variant.group_id]
        member = bundle["members"]["r"]
        assert member["metrics"] == found[members["r"]["metrics"].group_id]
        for name in room_bundles.SOURCES:
            assert member["sources"][name] == found[members["r"][name].group_id]
        before = deepcopy(member["sources"]["human"])
        bundle["human"]["exclusive_seconds"]["r"] = -1
        assert member["sources"]["human"] == before


@pytest.mark.parametrize("mode", ["exact", "cells"])
def test_named_zone_quality_override_is_not_shared(tmp_path, mode):
    problem = (
        exact_problem() if mode == "exact" else cell_problem({"r": [(70,), (10,), (20,), (60,)]})
    )
    zone = problem.zones[0]
    # Even an equal current override must keep its named scoring boundary.
    quality = {"zones": {zone.group_id: {}}}
    problem = replace(problem, policy=replace(problem.policy, quality=quality))
    patterns = [{"r": {0: 50}}]
    base, members = room_bundles.variants(zone, None)
    variants = [*base.values(), *(z for found in members.values() for z in found.values())]
    with fixed.FixedScores(problem, 1, directory=tmp_path) as scores:
        expected = scores.scores(patterns, variants)
        actual = room_bundles._scores(scores, patterns, variants)
    assert actual == expected
