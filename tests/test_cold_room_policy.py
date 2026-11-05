"""Room eligibility needs whole-zone costs, not discarded report variants."""

import pytest
import test_tuner  # noqa: F401
from test_hybrid_facade import alternating, member, room
from tuner_under_test.engine.hybrid import engine, facade, room_bundles


@pytest.mark.parametrize("include_skipped", [False, True])
def test_class_check_does_not_build_unused_report_variants(tmp_path, monkeypatch, include_skipped):
    values, labels = alternating(6)
    members = {"r": member(tmp_path, "r", values)}
    groups, rooms = {"good": members}, {"good": room("good", labels)}
    if include_skipped:
        groups["presence"] = members
        rooms["presence"] = room("presence", [s for s in labels if s["state"] == "present"])
    started, discarded = [], []
    fit, bundle = engine.fit, room_bundles.bundles

    def fitted(*args, **kwargs):
        started.append(1)
        return fit(*args, **kwargs)

    def bundled(*args, **kwargs):
        if not started:
            discarded.append(1)
        return bundle(*args, **kwargs)

    monkeypatch.setattr(engine, "fit", fitted)
    monkeypatch.setattr(room_bundles, "bundles", bundled)
    actual = facade.fit_set(groups, 0, 720, rooms, 0)
    assert actual["groups"]["good"]["cost_after"] is not None
    if include_skipped:
        assert actual["groups"]["presence"] == {"skipped": facade.NO_CLASS}
    assert discarded == []
