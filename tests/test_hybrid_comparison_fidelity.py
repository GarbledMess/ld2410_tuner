"""Incremental comparisons retain the fidelity of the complete displayed bundle."""

import asyncio
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from test_hybrid_facade import KEY, alternating, worker_view
from test_hybrid_routing import _context
from tuner_under_test.calibration import comparison_cache, comparison_jobs
from tuner_under_test.engine.hybrid import adapters, evaluate, facade, fixed


@pytest.mark.parametrize("transition", [False, True])
def test_partial_comparison_uses_the_full_bundle_plan(tmp_path, monkeypatch, transition):
    values, labels = alternating(6)
    view = worker_view(tmp_path, "r", values, labels)
    context = _context({KEY: 40})
    problem = adapters.device_problem(view, "r", [KEY], {KEY: 90}, {}, 0, None, 0, 720)
    with fixed.FixedScores(problem, 1) as scores:
        rows = scores.plan.row_count
    monkeypatch.setattr(evaluate, "EXACT_BUDGET", rows)
    complete = facade.compare(view, "r", context)
    assert complete["scoring_mode"] == "cells"
    device = {}
    comparison_cache.remember(
        device, context, {**complete, "patterns": {"user": complete["patterns"]["user"]}}
    )
    if transition:
        # A less dense captured history can move the full bundle from cells to exact.
        monkeypatch.setattr(evaluate, "EXACT_BUDGET", rows * 2)
    runtime = SimpleNamespace(
        data={"devices": {"r": device}},
        _comparison_semaphore=asyncio.Semaphore(1),
        _comparison_cache={},
        _schedule_save=lambda: None,
    )
    sent = []

    async def execute(_runtime, operation, device_id, parameters):
        assert (operation, device_id) == ("comparison", "r")
        sent.append(parameters["context"])
        return facade.compare(view, "r", parameters["context"])

    monkeypatch.setattr(comparison_jobs.bridge, "device", execute)
    monkeypatch.setattr(comparison_jobs, "_context", lambda *_args: (context, {}))
    report = asyncio.run(
        comparison_jobs._run(runtime, "r", context, {}, {"live": context["patterns"]["live"]})
    )
    assert set(sent[0]["patterns"]) == {"live"}
    if transition:
        assert set(sent[1]["patterns"]) == {"user", "live"}
    expected = "exact" if transition else "cells"
    assert report["patterns"]["live"]["scoring_mode"] == expected
    assert report["patterns"]["user"]["scoring_mode"] == expected


def test_distinct_bundle_size_changes_cache_identity_but_duplicate_slots_do_not():
    context = _context({KEY: 40})
    pattern = context["patterns"]["user"]
    original = comparison_cache.pattern_key(context, pattern)
    context["patterns"]["automatic"] = dict(pattern)
    assert comparison_cache.pattern_key(context, pattern) == original
    context["patterns"]["automatic"] = {**pattern, "thresholds": {KEY: 30}}
    assert comparison_cache.pattern_key(context, pattern) != original
