"""Independent labels and elapsed physical support survive sparse event delivery."""

import pytest
import test_tuner
from test_rooms import member
from tuner_under_test.calibration import quality, quality_evidence
from tuner_under_test.history import guesses


def view(stretches, labels, runs=()):
    """A radar labelled ``labels`` whose recorded history is ``stretches`` and guess ``runs``."""
    from tuner_under_test.history import dayfile

    result = member("any-name", [], labels)["view"]
    for events, end in stretches:
        test_tuner.recorded(result, "any-name", events, end)
    for start, end, state, level, basis in runs:
        result._days.append("any-name", dayfile.Guess(start, state, level, basis))
        result._days.append("any-name", dayfile.Guess(end, None, 0, 0))
    return result


def block(start, end, value):
    return [(start, (value,) + (None,) * 17)], end


def test_stable_empty_support_is_elapsed_time_not_one_event():
    source = view([block(0, 600, 20)], [{"start": 0, "end": 600, "state": "not_present"}])
    context = quality_evidence.collect(source, "any-name", ["g0_move"])
    assert context.gate_empty["g0_move"][20] == 100
    assert quality.learn(context).regimes["g0_move"].basis == "empirical_empty_tail"


def test_one_annotation_split_by_recording_gap_is_one_independent_vote():
    source = view(
        [block(0, 10, 20), block(20, 30, 20)],
        [{"start": 0, "end": 30, "state": "present"}],
        [(0, 30, "present", 20, guesses.EXTERNAL)],
    )
    context = quality_evidence.collect(source, "any-name", ["g0_move"])
    model = quality.learn(context)
    assert model.human_episodes("present") == 1
    assert len(context.gate_presence["g0_move"]) == 1
    assert sum(item.observed for item in context.overlaps) == 20
    assert model.reliability(guesses.EXTERNAL, "present") == pytest.approx(0.8)


def test_native_quality_cutoff_excludes_later_energy_and_source_votes():
    source = view(
        [block(0, 10, 20), block(10, 20, 99)],
        [{"start": 0, "end": 20, "state": "not_present"}],
        [(10, 20, "present", 20, guesses.EXTERNAL)],
    )
    source._attribution_until = 10
    context = quality_evidence.collect(source, "any-name", ["g0_move"])
    assert context.gate_empty["g0_move"][99] == 0
    assert context.raw_runs == ()
    assert sum(context.gate_empty["g0_move"]) == pytest.approx(10 / 6)


def test_automatic_only_stable_evidence_keeps_one_episode_across_outage():
    source = view(
        [block(0, 10, 20), block(20, 30, 20)], [], [(0, 30, "present", 20, guesses.EXTERNAL)]
    )
    context = quality_evidence.collect(source, "any-name", ["g0_move"])
    assert len(context.automatic_episodes) == 1
    assert context.automatic_episodes[0].confidence == 1
    assert quality.learn(context).episode_mass("present") == 0.75


def test_zero_duration_guess_is_not_an_observed_automatic_episode():
    source = view([block(0, 10, 20)], [], [(0, 0, "present", 20, guesses.EXTERNAL)])
    context = quality_evidence.collect(source, "any-name", ["g0_move"])
    assert context.automatic_episodes == ()


def test_fractional_delivery_cannot_drop_duration_below_support_boundary():
    events = [(i / 2, (20.01 + (i % 2) * 0.01,) + (None,) * 17) for i in range(1200)]
    source = view([(events, 600)], [{"start": 0, "end": 600, "state": "not_present"}])
    context = quality_evidence.collect(source, "any-name", ["g0_move"])
    assert context.gate_empty["g0_move"][21] == 100
    assert quality.learn(context).regimes["g0_move"].basis == "empirical_empty_tail"


def test_fingerprint_is_deterministic_and_sensitive_to_evidence():
    source = view([block(0, 600, 20)], [{"start": 0, "end": 600, "state": "present"}])
    first = quality_evidence.collect(source, "any-name", ["g0_move"])
    assert (
        first.fingerprint == quality_evidence.collect(source, "any-name", ["g0_move"]).fingerprint
    )
    changed = view([block(0, 600, 21)], [{"start": 0, "end": 600, "state": "present"}])
    assert (
        first.fingerprint != quality_evidence.collect(changed, "any-name", ["g0_move"]).fingerprint
    )
    assert (
        first.fingerprint
        != quality_evidence.collect(
            source,
            "any-name",
            ["g0_move"],
            end=300,
        ).fingerprint
    )


def test_fingerprinting_peak_is_bounded_below_whole_payload_repr(monkeypatch):
    import tracemalloc

    from tuner_under_test.calibration import event_quality

    cells = [(i, i + 1, (20 + i % 2,) * 18, None, None) for i in range(30000)]
    size = len(repr((cells, (), ["g0_move"], ("any-name",), None)).encode())
    tracemalloc.start()
    try:
        digest = event_quality._fingerprint(cells, (), ["g0_move"], ("any-name",), None)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(digest) == 64
    assert size > 2_000_000
    assert peak < 256_000
    assert peak < size / 10


def test_fingerprint_format_invalidates_saved_quality_policy():
    from tuner_under_test.calibration import comparison_cache, settings

    assert quality.QUALITY_VERSION == 5
    assert settings.signature({})["quality_version"] == 5

    pattern = {"thresholds": {"g0_move": 20}, "applicable": True}
    current = {
        "entities": {"g0_move": "number.synthetic"},
        "keys": ["g0_move"],
        "patterns": {"user": pattern},
        **settings.signature({}),
    }
    old = {**current, "quality_version": 4}
    device = {}
    report = {"patterns": {"user": {"score": 90}}}
    comparison_cache.remember(device, old, report)
    assert comparison_cache.stored(device, current, pattern) is None
    assert comparison_cache.missing(device, current) == {"user": pattern}
    comparison_cache.remember(device, current, report)
    assert comparison_cache.stored(device, current, pattern)["score"] == 90
    assert comparison_cache.missing(device, current) == {}


def test_quality_collection_streams_physical_cells_with_identical_context(monkeypatch):
    import tracemalloc

    from tuner_under_test.calibration import event_quality

    events = [(i / 2, (20.25 + i % 2,) + (None,) * 17) for i in range(30000)]
    source = view(
        [(events, 15000)],
        [{"start": 10, "end": 14990, "state": "not_present"}],
        [(0, 15000, "present", 20, guesses.EXTERNAL)],
    )
    # This is the materialized implementation's complete context, including its hash.
    expected = quality_evidence.collect(source, "any-name", ["g0_move"])
    assert (
        expected.fingerprint == "5b4588034b90e5440105e11c26b1117c07f1f12c8e4c7bb67bf58bbfb744ccbd"
    )
    original = event_quality._cells

    def materialized(*args, **kwargs):
        return list(original(*args, **kwargs))

    with monkeypatch.context() as patch:
        patch.setattr(event_quality, "_cells", materialized)
        assert quality_evidence.collect(source, "any-name", ["g0_move"]) == expected
    # The worker serves immutable decoded days. Exclude the live-store decoder here.
    store = source._days
    decoded = {date: store.day("any-name", date) for date in store.days("any-name")}
    monkeypatch.setattr(store, "day", lambda device, date, until=None: decoded[date])
    tracemalloc.start()
    try:
        actual = quality_evidence.collect(source, "any-name", ["g0_move"])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert actual == expected
    assert peak < 2_000_000
