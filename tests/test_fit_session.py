"""Fits of one problem under the two evidence policies share rows, and nothing else."""

from dataclasses import replace
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from real_rows import recorded_view
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.engine.hybrid import adapters, engine, held, replay
from tuner_under_test.engine.hybrid.types import ZoneSpec
from tuner_under_test.history import spans

ROWS = 100_000
WORK_ONLY = ("operations", "spilled_bytes")


@pytest.fixture(scope="module")
def problem(tmp_path_factory):
    view = recorded_view(tmp_path_factory.mktemp("session"), ROWS)
    keys = list(HISTORY_KEYS)
    start, end = spans.bounds(view, "r")
    current = dict.fromkeys(keys, 90)
    return adapters.device_problem(view, "r", keys, current, {}, 0.5, None, start, end)


def _view(result):
    diagnostics = {k: v for k, v in result.diagnostics.items() if k not in WORK_ONLY}
    return repr((result.thresholds, result.before, result.after, diagnostics))


def _both(problem):
    return held.with_reference(problem, True), held.with_reference(problem, False)


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_a_shared_fit_is_the_fit_alone(problem, order):
    problems = _both(problem)
    alone = [_view(engine.fit(problems[at])) for at in order]
    with engine.FitSession() as session:
        shared = [_view(engine.fit(problems[at], session=session)) for at in order]
    assert shared == alone


def test_a_shared_timed_fit_is_the_fit_alone(problem):
    timed = replace(
        problem,
        radars={"r": replace(problem.radars["r"], timing={"hold": 5, "on": 1, "off": 2})},
    )
    problems = _both(timed)
    alone = [_view(engine.fit(item)) for item in problems]
    with engine.FitSession() as session:
        shared = [_view(engine.fit(item, session=session)) for item in problems]
    assert shared == alone


def _counted(owner, name, calls):
    original = getattr(owner, name)

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    return patch.object(owner, name, counted)


def test_the_second_policy_compiles_no_training_rows(problem):
    compiled = []
    with _counted(engine, "collect_training", compiled), engine.FitSession() as session:
        for item in _both(problem):
            engine.fit(item, session=session)
    assert len(compiled) == 1


def test_the_second_policy_replays_only_new_patterns(problem):
    alone, shared = [], []
    plain = replay.replay

    def replayed(calls):
        def counted(problem_, providers, regions, wanted, *rest):
            calls.extend((rid, key) for rid, keys in wanted.items() for key in keys)
            return plain(problem_, providers, regions, wanted, *rest)

        return counted

    with patch("tuner_under_test.engine.hybrid.exact.replay", replayed(alone)):
        for item in _both(problem):
            engine.fit(item)
    with (
        patch("tuner_under_test.engine.hybrid.exact.replay", replayed(shared)),
        engine.FitSession() as session,
    ):
        for item in _both(problem):
            engine.fit(item, session=session)
    assert len(set(shared)) == len(shared)
    assert len(shared) < len(alone)


def test_other_truth_compiles_its_own_training_rows(problem):
    zone = problem.zones[0]
    human = tuple(span for span in zone.truth if span.source == "human")
    other = replace(problem, zones=(ZoneSpec("z", zone.members, human),))
    compiled = []
    alone = _view(engine.fit(other))
    with _counted(engine, "collect_training", compiled), engine.FitSession() as session:
        engine.fit(problem, session=session)
        assert _view(engine.fit(other, session=session)) == alone
    assert len(compiled) == 2


def test_training_fits_share_a_session(problem):
    compiled = []
    with _counted(engine, "collect_training", compiled):
        found = held.training_fits(problem, 0.5)
    assert len(compiled) == 1
    assert set(found) == set(held.POLICIES)
    assert found["reference"] == engine.fit(held.with_reference(problem, True)).thresholds


def test_a_failing_fit_leaves_no_file(problem, tmp_path, monkeypatch):
    monkeypatch.setattr(engine.tempfile, "tempdir", str(tmp_path))
    with (
        pytest.raises(RuntimeError),
        patch.object(engine, "search_candidates", side_effect=RuntimeError("boom")),
        engine.FitSession() as session,
    ):
        engine.fit(problem, session=session)
    assert list(tmp_path.iterdir()) == []
