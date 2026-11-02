"""Every production fit and score runs the hybrid engine, and identities mark the change.

The legacy row objectives and searches stay importable until they are deleted, but no
production entry point may reach them: each is patched to raise while worker, service,
room, adaptive and Apply paths run on synthetic recorded days.
"""

from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from test_hybrid_facade import KEY, QUIET, alternating, member, room, worker_view
from tuner_under_test.calibration import (
    automatic,
    comparison,
    comparison_cache,
    comparison_jobs,
    constants,
    event_fitting,
    results,
    service,
    settings,
)
from tuner_under_test.calibration import quality as quality_model
from tuner_under_test.engine.hybrid import constants as hybrid_constants
from tuner_under_test.engine.hybrid import facade
from tuner_under_test.history import result_cache, summary_cache
from tuner_under_test.rooms import (
    adaptive,
    application,
    assessment,
    fitting,
    groups,
    learning,
    objective,
)
from tuner_under_test.worker import calculations

ENTITIES = {KEY: "number.r_g0_move"}
OLD_ADAPTIVE = ("choose_policy", "_validated_policy", "_held_score", "_candidate_sources")


def _forbidden(name):
    def raise_(*_args, **_kwargs):
        raise AssertionError(f"production reached the legacy engine: {name}")

    return raise_


@pytest.fixture
def legacy(monkeypatch):
    """Make every legacy objective, mask table and search raise if constructed or run."""
    for cls in (objective.Objective, objective.LinkedObjective, objective.Radar):
        monkeypatch.setattr(cls, "__init__", _forbidden(cls.__name__))
    monkeypatch.setattr(event_fitting, "_objective", _forbidden("event_fitting._objective"))
    monkeypatch.setattr(event_fitting, "_search", _forbidden("event_fitting._search"))
    monkeypatch.setattr(fitting, "search", _forbidden("rooms.fitting.search"))


@pytest.fixture
def routed(monkeypatch):
    """Names of the facade entry points that production called."""
    calls = []
    for name in ("fit", "assess", "compare", "fit_set", "room_fit", "measure", "compare_room"):
        original = getattr(facade, name)

        def spy(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(facade, name, spy)
    return calls


def _context(thresholds):
    return {
        "keys": [KEY],
        "entities": ENTITIES,
        "timing": {},
        "patterns": {
            "user": {"id": "u", "thresholds": thresholds, "applicable": True},
            "live": {"id": None, "thresholds": {KEY: 90}, "applicable": False},
        },
        **settings.signature({}),
        "threshold_constraints": None,
    }


def _worker_member(threshold=90):
    return {"view_key": "r", "thresholds": {KEY: threshold}, "timing": {}}


def test_every_production_fit_and_score_routes_to_hybrid(tmp_path, legacy, routed):
    for name in OLD_ADAPTIVE:
        assert not hasattr(adaptive, name), f"legacy adaptive policy helper {name} remains"
    values, labels = alternating(10, last_present=QUIET)
    view = worker_view(tmp_path, "r", values, labels)
    view.data["learning_schedule"] = {settings.KEY: 0.5}
    parameters = {"device_id": "r", "entities": ENTITIES, "current": {KEY: 90}, "timing": None}

    learned = calculations._learn(view, parameters)
    assert learned["method"] == constants.METHOD
    assert learned["adaptive_policy"]["cutoff"] is not None  # Both candidate policies ran.
    assert routed == ["fit"]

    view._fit_constraints = None
    fallback = service._fit_history(view, "r", ENTITIES, {KEY: 90})
    assert fallback["proposals"] == learned["proposals"]
    compared = comparison.calculate(view, "r", _context({KEY: 40}))
    assert compared["model"] == comparison.SCORE_MODEL
    assert routed == ["fit", "fit", "compare"]

    rooms = {"zone": room("zone", labels)}
    (tmp_path / "plain").mkdir()
    plain = member(tmp_path / "plain", "r", values)
    views = {"r": plain["view"]}
    window = {"start": 0, "end": 1200}
    fitted = calculations._room(
        "room_fit",
        views,
        {"members": {"zone": {"r": _worker_member()}}, "rooms": rooms, "weight": 0.5, **window},
    )
    assert fitted["method"] == fitting.METHOD
    assert fitted["automatic_evidence"]["selection"]["cutoff"] is not None
    assessed = calculations._room(
        "room_assess", views, {"members": {"r": _worker_member()}, "room": rooms["zone"], **window}
    )
    assert assessed["scorer_version"] == assessment.SCORER_VERSION
    compared = calculations._room(
        "room_compare",
        views,
        {
            "members": {"zone": {"r": _worker_member()}},
            "thresholds": {"r": {KEY: 40}},
            "rooms": rooms,
            **window,
        },
    )
    assert compared["zone"]["candidate"]["error_cost"] is not None
    assert routed[3:] == ["fit_set", "measure", "compare_room"]

    members = {"zone": {"r": plain}}
    direct = learning._fit_set(members, 0, 1200, rooms, 0.5)
    assert direct["thresholds"] == fitted["thresholds"]
    zones = application._compare_all(members, {"r": {KEY: 40}}, 0, 1200, rooms)
    assert zones == compared
    assert fitting.fit({"r": plain}, 0, 1200, rooms["zone"])["method"] == fitting.METHOD
    assert fitting.measure({"r": plain}, {"r": {KEY: 40}}, 0, 1200, rooms["zone"])["room"]
    assert event_fitting.assess(view, "r", {KEY: 40})["scoring_mode"] in ("exact", "cells")
    assert routed[6:] == ["fit_set", "compare_room", "room_fit", "fit_set", "measure", "assess"]


def _device(learned):
    return {"learning_results": {"user": learned}, "label_revision": 0}


def test_old_results_are_viewable_but_not_automatically_applied(monkeypatch):
    report = {"training": {"duration": {"error_cost": 1.5, "basis": "sampled_timing_estimate"}}}
    old = {
        "id": "old",
        "method": "human_priority_v11",
        "entities": ENTITIES,
        "proposals": {KEY: {"threshold": 40}},
        **report,
    }
    device = _device(old)
    assert results.saved_results(device)["user"] == old  # Shown exactly as it was saved.
    assert comparison_jobs._pattern(old, ENTITIES)["applicable"] is False
    assert comparison_jobs._pattern({**old, "method": constants.METHOD}, ENTITIES)["applicable"]
    with pytest.raises(ValueError, match="current model"):
        service._validate_learning(device, old)
    patterns = {
        "live": {"score": 50.0, "error_cost": 50.0},
        "user": {"score": 90.0, "error_cost": 10.0, "applicable": False},
    }
    assert automatic.decision(patterns, "user") == (
        "The learned settings no longer match this device."
    )

    joint = {"id": "j", "method": "joint_room_v1", "thresholds": {}}
    runtime = SimpleNamespace(data={"room_learning": {"set": {"slots": {"user": joint}}}})
    monkeypatch.setattr(groups, "set_id_for", lambda _runtime, key: key)
    with pytest.raises(ValueError, match="learn the zone again"):
        application.selected(runtime, "set", "j", "user")
    joint["method"] = fitting.METHOD
    assert application.selected(runtime, "set", "j", "user") is joint


def test_old_scorer_cache_is_invalidated(tmp_path, monkeypatch):
    context = _context({KEY: 40})
    pattern = context["patterns"]["user"]
    device = {}
    comparison_cache.remember(
        device, context, {"patterns": {"user": {"score": 70.0}, "live": {"score": 60.0}}}
    )
    assert comparison_cache.stored(device, context, pattern)["score"] == 70.0
    record = device[comparison_cache.FIELD][comparison_cache.pattern_key(context, pattern)]
    record["scorer_version"] = 2  # As stored by the weighted-time scorer.
    assert comparison_cache.stored(device, context, pattern) is None

    sources = set(result_cache.PRODUCER_SOURCES)
    assert {
        "engine/hybrid/adapters.py",
        "engine/hybrid/evidence.py",
        "engine/hybrid/quality.py",
        "engine/hybrid/scoring.py",
        "history/fit_rows.py",
        "history/fit_days.py",
    } <= sources
    path = tmp_path / "radar" / "synthetic.day"
    path.parent.mkdir()
    path.write_bytes(b"synthetic physical identity")
    identity = (*summary_cache.stamp(path), ())
    result_cache.store(path, path.parent, identity, "rows", (), 7)
    assert result_cache.load(path, identity, "rows", ()) == (True, 7)
    monkeypatch.setattr(result_cache, "SCHEMA", result_cache.SCHEMA + "-next")
    result_cache._producer.cache_clear()
    try:
        assert result_cache.load(path, identity, "rows", ()) == (False, None)
    finally:
        monkeypatch.undo()
        result_cache._producer.cache_clear()


def test_new_hybrid_result_is_not_marked_older_model(tmp_path):
    assert constants.METHOD == "human_priority_v12"
    assert fitting.METHOD == constants.ROOM_METHOD == "joint_room_v2"
    assert comparison.SCORER_VERSION == assessment.SCORER_VERSION == 3
    assert comparison.SCORE_MODEL == facade.SCORE_MODEL == "hybrid_time_episode_v1"
    assert quality_model.QUALITY_VERSION == 6
    values, labels = alternating(6)
    view = worker_view(tmp_path, "r", values, labels)
    parameters = {"device_id": "r", "entities": ENTITIES, "current": {KEY: 90}, "timing": None}
    learned = calculations._learn(view, parameters)
    assert learned["method"] == settings.signature(view.data)["learner_method"]
    assert learned["quality_version"] == quality_model.QUALITY_VERSION
    (tmp_path / "zone").mkdir()
    item = member(tmp_path / "zone", "r", values, labels)
    fitted = facade.room_fit({"r": item}, 0, 720)
    assert fitted["method"] == fitting.METHOD
    assert fitted["after"]["scorer_version"] == assessment.SCORER_VERSION
    stored = {
        **fitted,
        "id": "j",
        "group_id": "zone",
        "group_name": "Zone",
        "created_at": 0,
        "signature": {"r": {"thresholds": {KEY: 90}, "entities": ENTITIES, "revision": 0}},
        "groups": {"zone": {"after": fitted["after"]}},
    }
    projected = learning.projection(stored, "r")
    assert projected["method"] == constants.METHOD
    assert projected["joint"]["report"]["scorer_version"] == assessment.SCORER_VERSION


def test_apply_uses_unrounded_common_cost_and_no_zone_regression(tmp_path):
    values, labels = alternating(6, last_present=QUIET)
    item = member(tmp_path, "r", values)
    rooms = {"zone": room("zone", labels)}
    candidate = {"r": {KEY: 40}}
    zones = application._compare_all({"zone": {"r": item}}, candidate, 0, 720, rooms)
    measured = facade.measure({"r": item}, candidate, 0, 720, rooms["zone"])["room"]
    found = zones["zone"]["candidate"]
    assert found["error_cost"] == measured["error_cost"]
    assert found["error_cost"] < zones["zone"]["live"]["error_cost"]
    application._improvement(zones)  # The fresh candidate lowers the common cost.

    def side(cost):
        return {"score": round(100 - cost, 2), "error_cost": cost}

    rounded = {"zone": {"live": side(5.0), "candidate": side(5.0 - 1e-6)}}
    assert rounded["zone"]["live"]["score"] == rounded["zone"]["candidate"]["score"]
    application._improvement(rounded)  # Equal display scores; the unrounded cost decides.

    linked = {
        "a": {"live": side(10.0), "candidate": side(1.0)},
        "b": {"live": side(1.0), "candidate": side(1.0 + 1e-6)},
    }
    with pytest.raises(ValueError, match=application.WORSE):
        application._improvement(linked)  # Lower total, but one zone gets worse.
    tie = {"zone": {"live": side(5.0), "candidate": side(5.0)}}
    with pytest.raises(ValueError, match="does not reduce"):
        application._improvement(tie)


def test_fidelity_change_invalidates_comparison_cache(monkeypatch):
    context = _context({KEY: 40})
    pattern = context["patterns"]["user"]
    device = {}
    comparison_cache.remember(
        device, context, {"patterns": {"user": {"score": 70.0}, "live": {"score": 60.0}}}
    )
    key = comparison_cache.pattern_key(context, pattern)
    assert comparison_cache.stored(device, context, pattern)["score"] == 70.0
    for name in ("EXACT_BUDGET", "MIN_EXACT", "ROW_BLOCK_ROWS", "CHUNK_SECONDS", "CELL_SECONDS"):
        with monkeypatch.context() as patched:
            patched.setattr(hybrid_constants, name, getattr(hybrid_constants, name) + 1)
            assert comparison_cache.pattern_key(context, pattern) != key, name
            assert comparison_cache.stored(device, context, pattern, key) is None, name
            assert set(comparison_cache.missing(device, context)) == {"user", "live"}, name
    assert comparison_cache.stored(device, context, pattern)["score"] == 70.0


def test_apply_explains_the_common_episode_aware_penalty():
    patterns = {
        "live": {"score": 50.0, "error_cost": 50.0},
        "user": {"score": 50.0, "error_cost": 50.0, "applicable": True},
    }
    assert "episode" in automatic.decision(patterns, "user")
    with pytest.raises(ValueError, match="episode"):
        application._improvement(
            {"zone": {"live": patterns["live"], "candidate": patterns["user"]}}
        )
