import math

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.calibration.episodes import Episode


def context(**changes):
    from tuner_under_test.calibration.quality_evidence import QualityContext

    values = {
        "episodes": (),
        "automatic_episodes": (),
        "overlaps": (),
        "gate_presence": {},
        "gate_empty": {},
        "fingerprint": "test",
    }
    return QualityContext(**(values | changes))


def test_quality_prior_support_and_invalid_confidence():
    from tuner_under_test.calibration.quality import learn

    model = learn(context())
    assert model.reliability(1, "present") == 0.75
    assert model.supports("g0_move", 5)
    assert model.correct(0.55, 0, "present") == pytest.approx(0.4125)
    for value in (math.nan, math.inf, -1, 1.1):
        with pytest.raises(ValueError):
            model.correct(value, 1, "present")


def test_reliability_one_fractional_vote_per_episode_source_state():
    from tuner_under_test.calibration.quality import learn
    from tuner_under_test.calibration.quality_evidence import Overlap

    ep = Episode(0, 10, "present", ("a",))
    row = Overlap(ep, 1, "present", 5, 10)
    model = learn(context(episodes=(ep,), overlaps=(row, row)))
    assert model.reliability(1, "present") == 0.7
    assert model.reliability(2, "present") == 0.75
    assert model.human_episodes("present") == 1


def test_quiet_support_and_automatic_mass_deduplicate_basis_fragments():
    from tuner_under_test.calibration.quality import learn
    from tuner_under_test.calibration.quality_evidence import AutomaticEpisode

    ep = Episode(0, 10, "present", ("a",))
    auto = AutomaticEpisode(ep, 1, 0.8, ("a",))
    empty = tuple(int(i == 30) for i in range(101))
    model = learn(
        context(
            automatic_episodes=(auto, auto, AutomaticEpisode(ep, 2, 0.8, ("a",))),
            gate_presence={"g": (1 << 10,)},
            gate_empty={"g": empty},
        )
    )
    assert model.episode_mass("present") == pytest.approx(0.6)
    assert not model.supports("g", 10)
    assert model.supports("g", 31)
    assert learn(
        context(gate_presence={"g": (1 << 10, 1 << 10)}, gate_empty={"g": empty})
    ).supports("g", 10)


def view_fixture(labels=(), runs=()):
    from codex_history import view_with_runs

    return view_with_runs(labels, runs)


def test_collection_raw_basis_human_precedence_and_training_window():
    from tuner_under_test.calibration.quality_evidence import collect

    view = view_fixture([{"start": 0, "end": 10, "state": "present"}], [(0, 9, "present", 16, 0)])
    ctx = collect(view, "a", ["g0_move"], start=0, end=5)
    assert ctx.basis_at(2) == 0
    assert ctx.raw_reader().sample_guess(2)[2] == 0.8
    assert len(ctx.episodes) == 1
    assert ctx.episodes[0].end <= 5
    assert len(ctx.overlaps) == 1
    assert ctx.overlaps[0].agreement == ctx.overlaps[0].observed
    assert not ctx.automatic_episodes
    changed = view_fixture(
        [{"start": 0, "end": 10, "state": "present"}], [(0, 9, "present", 15, 0)]
    )
    assert collect(changed, "a", ["g0_move"], start=0, end=5).fingerprint != ctx.fingerprint


def test_unknown_blocks_automatic_and_confidence_changes_share_episode():
    from tuner_under_test.calibration.quality_evidence import collect

    view = view_fixture(
        [{"start": 3, "end": 6, "state": "unknown"}],
        [(0, 4, "present", 16, 1), (4, 9, "present", 18, 2)],
    )
    ctx = collect(view, "a", ["g0_move"])
    assert not ctx.episodes
    assert len({row.episode for row in ctx.automatic_episodes}) == 2
    assert all(not (row.episode.start < 4 < row.episode.end) for row in ctx.automatic_episodes)


def test_stable_sampler_counts_only_admitted_mass_and_keeps_bucket_anchor():
    from codex_history import objective

    view = view_fixture(
        [{"start": 0, "end": 2, "state": "unknown"}, {"start": 5, "end": 20, "state": "unknown"}],
        [(0, 20, "present", 16, 1)],
    )
    regions = objective(view).regions
    assert [(a, b) for a, b, *_ in regions] == [(2, 5)]
    assert sum(b - a for a, b, *_ in regions) == 3
    view.data["devices"]["a"]["history_labels"] = [{"start": 0, "end": 20, "state": "unknown"}]
    assert objective(view).regions == []


def test_sampler_gap_and_partial_buckets_do_not_transfer_mass():
    from codex_history import objective, view_with_runs

    rows = [(t, bytes([10] * 18)) for t in (0, 1, 2, 50, 51)]
    view = view_with_runs(runs=[(0, 100, "present", 16, 1)], rows=rows)
    regions = objective(view).regions
    assert sum(b - a for a, b, *_ in regions) == 9
    assert all(b <= 5 or a >= 50 for a, b, *_ in regions)


def test_optional_room_raw_reader_preserves_unknown_precedence():
    from tuner_under_test.history.guesses import Guesses
    from tuner_under_test.rooms.evidence import device_label, read

    view = view_fixture([{"start": 3, "end": 6, "state": "unknown"}], [(0, 9, "present", 16, 1)])
    raw = Guesses([(0, 9, "present", 16, 1)])
    result = read(view, "a", ["g0_move"], 0, 9, automatic_reader=raw)
    assert device_label(result, 4) is None
    assert device_label(result, 2) == ("present", "automatic", 0.8)


def test_internal_unknown_interval_is_a_barrier_even_between_samples():
    from tuner_under_test.calibration.quality_evidence import collect

    view = view_fixture(
        [{"start": 2.2, "end": 2.3, "state": "unknown"}], [(0, 9, "present", 16, 1)]
    )
    ctx = collect(view, "a", ["g0_move"])
    assert len({row.episode for row in ctx.automatic_episodes}) == 2
    assert all(row.episode.end <= 2.2 or row.episode.start >= 2.3 for row in ctx.automatic_episodes)


def test_sampler_explicit_barriers_reset_without_assigning_rejected_mass():
    from codex_history import objective

    view = view_fixture([{"start": 1, "end": 2, "state": "unknown"}], [(0, 3, "present", 16, 1)])
    regions = objective(view, end=3).regions
    assert [(a, b) for a, b, *_ in regions] == [(0, 1), (2, 3)]


def test_raw_run_gap_is_a_sampler_barrier_even_when_samples_are_near():
    from codex_history import objective, view_with_runs

    rows = [(i / 10, bytes([10] * 18)) for i in range(12)]
    view = view_with_runs(
        runs=[(0, 0.5, "present", 16, 1), (0.6, 1.2, "present", 16, 1)], rows=rows
    )
    regions = objective(view, end=1.2).regions
    assert sum(b - a for a, b, *_ in regions) == pytest.approx(1.1)
    assert all(b <= 0.5 or a >= 0.6 for a, b, *_ in regions)


def test_confidence_fragments_do_not_shift_raw_bucket_anchors():
    from codex_history import objective, view_with_runs

    rows = [(i / 2, bytes([10] * 18)) for i in range(25)]

    def physical(runs):
        return objective(view_with_runs(runs=runs, rows=rows), end=13).regions

    whole = physical([(0, 13, "present", 16, 1)])
    split = physical([(0, 3, "present", 16, 1), (3, 13, "present", 18, 1)])
    from tuner_under_test.rooms.intervals import union

    assert (
        union([(a, b) for a, b, *_ in whole]) == union([(a, b) for a, b, *_ in split]) == [(0, 13)]
    )
    assert all(
        row[2]["g0_move"] == 10 and row[3:5] == ("present", "automatic") for row in whole + split
    )
    assert sum(b - a for a, b, *_ in whole) == 13


def test_training_reader_excludes_cutoff_sample_and_basis():
    from tuner_under_test.calibration.quality_evidence import collect

    ctx = collect(view_fixture(runs=[(0, 9, "present", 16, 1)]), "a", ["g0_move"], end=5)
    assert ctx.raw_reader().sample_guess(5) is None
    assert ctx.raw_reader().of_sample(5) is None
    assert ctx.basis_at(5) == 0


def test_audit_is_serializable_and_separates_raw_corrected_support():
    import json

    from tuner_under_test.calibration.quality import learn
    from tuner_under_test.calibration.quality_evidence import AutomaticEpisode

    ep = Episode(0, 10, "present", ("a",))
    audit = learn(context(automatic_episodes=(AutomaticEpisode(ep, 1, 0.8, ("a",)),))).audit()
    assert audit["raw_episode_mass"]["present"] == 0.8
    assert audit["automatic_episode_mass"]["present"] == pytest.approx(0.6)
    assert "recorded_basis_only" in json.dumps(audit)


def test_held_label_edits_cannot_change_training_summary():
    from tuner_under_test.calibration.quality_evidence import collect

    view = view_fixture([{"start": 6, "end": 9, "state": "present"}], [(0, 9, "present", 16, 1)])
    first = collect(view, "a", ["g0_move"], end=5)
    view.data["devices"]["a"]["history_labels"][0]["state"] = "not_present"
    second = collect(view, "a", ["g0_move"], end=5)
    assert first.fingerprint == second.fingerprint
    assert first.automatic_episodes == second.automatic_episodes


def test_independent_room_labels_supply_support_but_own_exclusions_win():
    from tuner_under_test.calibration.quality_evidence import collect
    from tuner_under_test.history.label_lookup import index_reader

    view = view_fixture([{"start": 0, "end": 3, "state": "unknown"}])
    independent = index_reader(([0, 10], ["not_present", None]))
    found = collect(view, "a", ["g0_move"], human_reader=independent, owner=("a", "room"))
    assert found.episodes
    assert all(ep.start >= 3 and ep.state == "not_present" for ep in found.episodes)
    assert all(ep.owner == ("a", "room") for ep in found.episodes)
    assert sum(found.gate_empty["g0_move"]) > 0
    self_only = collect(view, "a", ["g0_move"])
    assert not self_only.episodes


@pytest.mark.parametrize("runs", [[(0, 20, "present", 16, 1)], []])
def test_raw_bucket_preserves_each_attributed_class_mass(runs):
    from codex_history import objective, view_with_runs

    labels = [
        {"start": 0, "end": 5, "state": "present"},
        {"start": 5, "end": 6, "state": "not_present"},
    ]
    rows = [(i / 2, bytes([10] * 18)) for i in range(12)]
    regions = objective(view_with_runs(labels, runs, rows), end=6).regions
    assert sum(b - a for a, b, _, state, *_ in regions if state == "present") == 5
    assert sum(b - a for a, b, _, state, *_ in regions if state == "not_present") == 1


def histogram(**counts):
    return tuple(counts.get(str(energy), 0) for energy in range(101))


def test_credible_quiet_tail_ignores_rare_empty_spike_and_retains_quiet_veto():
    from tuner_under_test.calibration.quality import learn

    model = learn(
        context(
            gate_presence={"g": (1 << 20,)},
            gate_empty={"g": histogram(**{"10": 999, "90": 1})},
            gate_occupied_histograms={"g": (histogram(**{"14": 50, "20": 50}),)},
            gate_empty_histograms={"g": (histogram(**{"10": 999, "90": 1}),)},
        )
    )
    assert model.supports("g", 20)
    assert not model.supports("g", 10)
    assert not model.supports("g", 13)
    assert model.supports("g", 14)


def test_recurrent_empty_spikes_and_sparse_baselines_remain_quiet():
    from tuner_under_test.calibration.quality import learn

    recurrent = learn(context(gate_empty={"g": histogram(**{"10": 980, "90": 20})}))
    sparse = learn(context(gate_empty={"g": histogram(**{"10": 98, "90": 1})}))
    assert not recurrent.supports("g", 20)
    assert not sparse.supports("g", 20)
    assert recurrent.supports("g", 91)
    assert sparse.supports("g", 91)


def test_tail_boundary_minimum_count_and_observed_maximum_cap():
    from tuner_under_test.calibration.quality import learn

    at_minimum = learn(
        context(
            gate_empty={"g": histogram(**{"10": 99, "90": 1})},
            gate_occupied_histograms={"g": (histogram(**{"14": 100}),)},
            gate_empty_histograms={"g": (histogram(**{"10": 99, "90": 1}),)},
        )
    )
    assert at_minimum.supports("g", 14)
    narrow = learn(context(gate_empty={"g": histogram(**{"10": 100})}))
    assert narrow.supports("g", 11)
    assert not narrow.supports("g", 10)


def test_two_independent_occupied_episodes_override_credible_quiet_only_at_observed_bin():
    from tuner_under_test.calibration.quality import learn

    empty = histogram(**{"10": 999, "90": 1})
    one = learn(context(gate_presence={"g": (1 << 10,)}, gate_empty={"g": empty}))
    two = learn(context(gate_presence={"g": (1 << 10, 1 << 10)}, gate_empty={"g": empty}))
    assert not one.supports("g", 10)
    assert two.supports("g", 10)
    assert not two.supports("g", 11)


def test_tail_audit_is_versioned_and_reports_prior_sparse_and_credible_paths():
    from tuner_under_test.calibration.quality import QUALITY_VERSION, learn

    model = learn(
        context(
            gate_empty={"g": histogram(**{"10": 999, "90": 1}), "sparse": histogram(**{"30": 1})},
            gate_presence={"unseen": ()},
        )
    )
    audit = model.audit()
    assert QUALITY_VERSION == audit["quality_version"] == 5
    gate = audit["gate_support"]["g"]
    assert gate["empty_maximum"] == 90
    assert gate["empty_quantile"] == 10
    assert gate["credible_ceiling"] == 13
    assert gate["regime_basis"] == "empirical_empty_tail"
    assert audit["gate_support"]["sparse"]["regime_basis"] == "sparse_empty_maximum"
    assert audit["gate_support"]["unseen"]["regime_basis"] == "no_empty_prior"
    assert audit["gate_support"]["unseen"]["credible_ceiling"] is None


def test_learned_tail_is_frozen_and_does_not_mutate_human_histograms():
    from tuner_under_test.calibration.quality import learn

    counts = list(histogram(**{"10": 999, "90": 1}))
    ctx = context(
        gate_empty={"g": counts},
        gate_occupied_histograms={"g": (histogram(**{"20": 100}),)},
        gate_empty_histograms={"g": (counts,)},
    )
    before = ctx.gate_empty["g"]
    model = learn(ctx)
    counts[10], counts[90] = 0, 1000
    assert model.supports("g", 20)
    assert ctx.gate_empty["g"] == before
    with pytest.raises(TypeError):
        model.regimes["g"] = None


def test_v3_tail_relaxation_requires_explicit_occupied_regime_support():
    from tuner_under_test.calibration.quality import learn

    empty = histogram(**{"10": 999, "90": 1})
    quiet_episode = histogram(**{"10": 1000})
    legitimate = learn(
        context(
            gate_empty={"g": empty},
            gate_occupied_histograms={"g": (histogram(**{"20": 100}),)},
            gate_empty_histograms={"g": (quiet_episode,)},
        )
    )
    ghost = learn(
        context(
            gate_empty={"g": empty},
            gate_occupied_histograms={"g": (histogram(**{"80": 100}),)},
            gate_empty_histograms={"g": (quiet_episode,)},
        )
    )
    assert legitimate.supports("g", 20)
    assert not ghost.supports("g", 20)
    assert not legitimate.supports("g", 10)
    assert ghost.supports("g", 91)


def test_v3_episode_normalization_does_not_let_one_long_empty_episode_hide_noisy_episode():
    from tuner_under_test.calibration.quality import learn

    empty = histogram(**{"10": 100000, "20": 10, "90": 1})
    quiet = histogram(**{"10": 100000})
    noisy = histogram(**{"20": 10})
    occupied = histogram(**{"20": 1000})
    model = learn(
        context(
            gate_empty={"g": empty},
            gate_occupied_histograms={"g": (occupied,)},
            gate_empty_histograms={"g": (quiet, noisy)},
        )
    )
    # Equal episode weights: P(20 +/-3 | occupied)=1, P(...|empty)=.5;
    # (1+.01)/(.5+.01) <2, despite the pooled empty density near zero.
    assert not model.supports("g", 20)


def test_v3_occupied_duration_cannot_manufacture_density_support():
    from tuner_under_test.calibration.quality import learn

    kwargs = {
        "gate_empty": {"g": histogram(**{"10": 999, "90": 1})},
        "gate_empty_histograms": {"g": (histogram(**{"10": 100}),)},
    }
    one = learn(
        context(**kwargs, gate_occupied_histograms={"g": (histogram(**{"20": 5, "80": 95}),)})
    )
    repeated = learn(
        context(**kwargs, gate_occupied_histograms={"g": (histogram(**{"20": 5000, "80": 95000}),)})
    )
    assert one.supports("g", 20) == repeated.supports("g", 20)
    assert (
        one.audit()["gate_support"]["g"]["likelihood_supported_bins"]
        == repeated.audit()["gate_support"]["g"]["likelihood_supported_bins"]
    )


def test_rare_occupied_low_tail_cannot_authorize_quiet_overlapping_automatic_energy():
    from tuner_under_test.calibration.quality import learn

    model = learn(
        context(
            gate_empty={"g": histogram(**{"10": 999, "90": 1})},
            gate_occupied_histograms={"g": (histogram(**{"20": 5, "80": 95}),)},
            gate_empty_histograms={"g": (histogram(**{"10": 100}),)},
        )
    )
    assert not model.supports("g", 20)
    assert model.supports("g", 80)


def test_v3_collection_histograms_are_immutable_and_training_only():
    from tuner_under_test.calibration.quality_evidence import collect

    view = view_fixture(
        [
            {"start": 0, "end": 4, "state": "present"},
            {"start": 4, "end": 10, "state": "not_present"},
        ]
    )
    ctx = collect(view, "a", ["g0_move"], end=6)
    assert len(ctx.gate_occupied_histograms["g0_move"]) == 1
    assert len(ctx.gate_empty_histograms["g0_move"]) == 1
    assert sum(ctx.gate_occupied_histograms["g0_move"][0]) == 4
    assert sum(ctx.gate_empty_histograms["g0_move"][0]) == 2
    view.data["devices"]["a"]["history_labels"].append({"start": 6, "end": 9, "state": "present"})
    changed = collect(view, "a", ["g0_move"], end=6)
    assert changed.gate_occupied_histograms == ctx.gate_occupied_histograms
    with pytest.raises(TypeError):
        ctx.gate_occupied_histograms["g0_move"][0][10] = 0


def test_v3_tail_audit_exposes_normalized_regime_support_and_prior():
    from tuner_under_test.calibration.quality import QUALITY_VERSION, learn

    model = learn(
        context(
            gate_empty={"g": histogram(**{"10": 999, "90": 1})},
            gate_occupied_histograms={"g": (histogram(**{"20": 100}),)},
            gate_empty_histograms={"g": (histogram(**{"10": 100}),)},
        )
    )
    audit = model.audit()
    assert QUALITY_VERSION == audit["quality_version"] == 5
    assert audit["gate_support"]["g"]["likelihood_supported_bins"] > 0
    assert audit["gate_support"]["g"]["occupied_density_episodes"] == 1
    assert audit["gate_support"]["g"]["empty_density_episodes"] == 1
    assert audit["regime_policy"]["density_prior"] == 0.01
    assert audit["regime_policy"]["minimum_likelihood_ratio"] == 2
    assert audit["provenance"] == "recorded_basis_only"
