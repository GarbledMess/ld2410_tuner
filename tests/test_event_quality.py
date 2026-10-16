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
