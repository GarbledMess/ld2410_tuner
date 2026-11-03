"""Deferred report attribution preserves complete public fit metrics."""

from dataclasses import replace

import pytest
import test_tuner  # noqa: F401
from test_hybrid_exact import Rows
from test_hybrid_search import Days
from test_hybrid_selection import episodes, fit_problem
from tuner_under_test.engine.hybrid import engine
from tuner_under_test.engine.hybrid.types import ZoneSpec


def room_problem():
    rows, spans = episodes(12, quiet=True)
    p = fit_problem(rows, spans)
    radar = replace(p.radars["a"], device_id="b")
    other = [(a, b, [max(0, v[0] - 5), *v[1:]]) for a, b, v in rows]
    return replace(
        p,
        radars={**p.radars, "b": radar},
        zones=(ZoneSpec("z", ("a", "b"), spans),),
        days=Days({"a": rows, "b": other}),
        rows={"a": Rows(rows), "b": Rows(other)},
    )


def test_room_fit_defers_attribution_during_candidate_selection(monkeypatch):
    requested = []
    original = engine.evaluate_patterns

    def record(*args, **kwargs):
        requested.append(kwargs.get("attribution", True))
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "evaluate_patterns", record)
    result = engine.fit(room_problem(), audit=False)
    assert result.after["exclusive_seconds"]
    assert requested and not any(requested)


@pytest.mark.parametrize("mode", ["exact", "cells", "cells_then_exact"])
@pytest.mark.parametrize("audit", [False, True])
def test_deferred_fit_equals_eager_complete_metrics(monkeypatch, mode, audit):
    original_plan = engine.plan_scores

    def plan(*args, **kwargs):
        value = original_plan(*args, **kwargs)
        return replace(
            value,
            mode=mode,
            finalist_limit=2 if mode == "cells_then_exact" else value.finalist_limit,
        )

    monkeypatch.setattr(engine, "plan_scores", plan)
    optimized = engine.fit(room_problem(), audit=audit)
    original = engine.evaluate_patterns

    def eager(*args, **kwargs):
        kwargs["attribution"] = True
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "evaluate_patterns", eager)
    baseline = engine.fit(room_problem(), audit=audit)
    assert optimized == baseline
    assert optimized.before["exclusive_seconds"] == baseline.before["exclusive_seconds"]
    assert (
        optimized.after["zones"]["z"]["exclusive_seconds"]
        == baseline.after["zones"]["z"]["exclusive_seconds"]
    )


def test_fit_attributes_only_before_and_selected_base_patterns(monkeypatch):
    from tuner_under_test.engine.hybrid import exact

    attributed = []
    original = exact._Scoring.run

    def counted(self, jobs):
        if self.attribution:
            attributed.extend(jobs)
        return original(self, jobs)

    monkeypatch.setattr(exact._Scoring, "run", counted)
    result = engine.fit(room_problem(), audit=False)
    assert result.diagnostics["finalists"] > 2
    assert 1 <= len(attributed) <= 2
    assert all(variant == "base" for _pattern, variant in attributed)


def test_single_member_fit_keeps_direct_attribution(monkeypatch):
    rows, spans = episodes(6)
    original = engine.evaluate_patterns
    requested = []

    def record(*args, **kwargs):
        requested.append(kwargs.get("attribution", True))
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "evaluate_patterns", record)
    result = engine.fit(fit_problem(rows, spans), audit=False)
    assert all(requested)
    assert result.after["exclusive_seconds"]["a"] > 0


def test_deferred_masked_fit_keeps_complete_report_metrics(monkeypatch):
    from test_hybrid_anomaly import phone_case, problem_for

    rows, truth = phone_case()
    p = problem_for(rows, truth)
    radar = replace(p.radars["a"], device_id="b")
    p = replace(
        p,
        radars={**p.radars, "b": radar},
        zones=(replace(p.zones[0], members=("a", "b")),),
        rows={**p.rows, "b": Rows(rows)},
    )
    optimized = engine.fit(p)
    assert optimized.diagnostics["anomaly"]["suspects"] > 0
    original = engine.evaluate_patterns

    def eager(*args, **kwargs):
        kwargs["attribution"] = True
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "evaluate_patterns", eager)
    assert optimized == engine.fit(p)
