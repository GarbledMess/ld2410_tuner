"""Synthetic global search and lossless decision-folding regressions."""

import random
from array import array
from dataclasses import replace

import pytest
import test_tuner  # noqa: F401
from test_hybrid_digest import block
from test_hybrid_evidence import truth
from tuner_under_test.engine.hybrid.candidates import candidate_values
from tuner_under_test.engine.hybrid.digest import fold_day
from tuner_under_test.engine.hybrid.search import (
    collect_training,
    conditional_histograms,
    coordinate_costs,
    search,
)
from tuner_under_test.engine.hybrid.training import TrainingStore
from tuner_under_test.engine.hybrid.types import EvidencePolicy, FitProblem, RadarSpec, ZoneSpec


class Days:
    def __init__(self, rows):
        self.rows = rows

    def dates(self, rid):
        return iter((0,))

    def digest(self, rid, date, channels):
        rows = self.rows[rid]
        return fold_day([block(rows)], 0, rows[-1][1], channels)


def problem(vectors, spans=None, current=90, domains=None, timing=None):
    rows = {
        rid: [(i * 6, (i + 1) * 6, list(v) + [255] * (18 - len(v))) for i, v in enumerate(vs)]
        for rid, vs in vectors.items()
    }
    n = len(next(iter(rows.values())))
    radars = {}
    for rid, vs in vectors.items():
        channels = tuple(range(len(vs[0])))
        radars[rid] = RadarSpec(
            rid,
            channels,
            domains or dict.fromkeys(channels, tuple(range(101))),
            dict.fromkeys(channels, current),
            timing or {},
        )
    spans = spans or [truth(0, n * 3), truth(n * 3, n * 6, "not_present", episode="e")]
    zone = ZoneSpec("z", tuple(vectors), tuple(spans))
    return FitProblem(radars, (zone,), 0, n * 6, Days(rows), {}, EvidencePolicy(0, False, {}))


def finalists(tmp_path, p, shard_cells=4096):
    with TrainingStore(tmp_path) as store:
        collect_training(p, store, shard_cells=shard_cells)
        return search(p, store)


def test_conditional_histograms_equal_scalar_or():
    rng = random.Random(81)
    cols = tuple(bytes(rng.randrange(101) for _ in range(71)) for _ in range(3))
    covered = bytes(any(cols[c][i] > 50 for c in (1, 2)) for i in range(71))
    weights = (array("d", [0.5] * 31 + [0] * 40), array("d", [0] * 31 + [0.7] * 40))
    got = conditional_histograms((cols[0],), covered, weights)
    expected = [[0.0] * 101 for _ in range(2)]
    for i in range(71):
        if not covered[i]:
            for side in range(2):
                expected[side][cols[0][i]] += weights[side][i]
    assert got == tuple(expected)


def test_candidate_domains_include_gap_centres_and_current():
    stats = {"gap": (20, 61), "hp": [0] * 101, "he": [0] * 101}
    values = candidate_values(tuple(range(101)), 80, stats, dict.fromkeys(range(101), 0))
    assert {20, 40, 41, 60, 80, 100} <= set(values)
    assert candidate_values((0, 7, 14, 21), 14, stats, {})[-1] == 21


def test_only_partial_label_cells_keep_current_search_start(tmp_path):
    p = problem({"a": [(70,), (0,)]}, spans=(truth(1, 5), truth(7, 11, "not_present", episode="e")))
    assert finalists(tmp_path, p) == [{"a": {0: 90}}]


def test_anticorrelated_gates_and_complementary_radars(tmp_path):
    p = problem({"a": [(70, 0), (0, 70), (20, 20), (20, 20)]})
    assert any(all(t < 70 for t in pat["a"].values()) for pat in finalists(tmp_path, p))
    p = problem({"a": [(70,), (0,), (20,), (20,)], "b": [(0,), (70,), (20,), (20,)]})
    assert any(pat["a"][0] < 70 and pat["b"][0] < 70 for pat in finalists(tmp_path, p))


def test_one_shared_radar_vector_minimizes_sum_of_zone_costs(tmp_path):
    p = problem({"a": [(70,), (40,), (20,), (20,)]})
    other = ZoneSpec("other", ("a",), (truth(0, 6), truth(6, 24, "not_present", episode="e2")))
    p = replace(p, zones=(*p.zones, other))
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        costs = coordinate_costs(p, store, {"a": {0: 90}}, "a", 0, (20, 40, 90))
    assert costs[40] < costs[90]
    assert costs[20] < costs[90]
    assert all(set(pat) == {"a"} for pat in finalists(tmp_path, p))


def test_shard_partition_does_not_change_candidates(tmp_path):
    p = problem({"a": [(70,), (61,), (20,), (10,)]})
    assert finalists(tmp_path, p, 1) == finalists(tmp_path, p, 4096)


def test_unsupported_channel_keeps_start(tmp_path):
    p = problem({"a": [(70, 0), (61, 0), (20, 0), (10, 0)]}, current=82)
    assert all(pat["a"][1] == 82 for pat in finalists(tmp_path, p))


def test_lowered_tie_variant_reaches_finalists(tmp_path):
    p = problem({"a": [(70,), (61,), (20,), (20,)]}, current=90)
    values = {pat["a"][0] for pat in finalists(tmp_path, p)}
    assert 40 in values
    assert 20 in values
    assert 90 in values


def test_lowered_ties_extend_only_best_untimed_pattern(tmp_path):
    from tuner_under_test.engine.hybrid.search import _Search

    p = problem({"a": [(70, 70), (61, 61), (20, 20), (20, 20)]}, current=90)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = _Search(p, store)
        engine.remember({"a": {0: 40, 1: 40}}, 0)
        engine.remember({"a": {0: 60, 1: 60}}, 0)
        engine.ties["a", 0] = 20
        engine.ties["a", 1] = 20
        got = engine.finalists(({"a": {0: 60, 1: 60}},), 16)
    assert {"a": {0: 20, 1: 40}} in got
    assert {"a": {0: 20, 1: 60}} not in got


def test_search_is_invariant_to_zone_order_and_radar_renames(tmp_path):
    p = problem({"a": [(70,), (0,), (20,), (20,)], "b": [(0,), (70,), (20,), (20,)]})
    first = finalists(tmp_path, p)
    q = problem({"b": [(70,), (0,), (20,), (20,)], "a": [(0,), (70,), (20,), (20,)]})
    second = finalists(tmp_path, q)
    assert {(pat["a"][0], pat["b"][0]) for pat in first} == {
        (pat["b"][0], pat["a"][0]) for pat in second
    }
    assert finalists(tmp_path, replace(p, zones=tuple(reversed(p.zones)))) == first


@pytest.mark.parametrize("seed", range(8))
def test_contested_folding_equals_unfolded_source_episode_counts(tmp_path, seed):
    rng = random.Random(seed)
    vectors = {
        rid: [tuple(rng.choice((0, 20, 21, 40, 70)) for _ in range(2)) for _ in range(24)]
        for rid in ("a", "b")
    }
    spans = [
        truth(
            i * 36,
            (i + 1) * 36,
            "present" if i % 2 else "not_present",
            source="human" if i < 2 else "entity",
            confidence=0.6 + 0.1 * i,
            episode=str(i),
        )
        for i in range(4)
    ]
    p = problem(vectors, spans)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        args = (
            p,
            store,
            {rid: {0: 40, 1: 40} for rid in vectors},
            "a",
            0,
            (0, 20, 21, 40, 70, 100),
        )
        assert coordinate_costs(*args) == coordinate_costs(*args, folded=False)


def test_search_does_not_bridge_recording_gaps_with_hold(tmp_path):
    p = problem({"a": [(70,), (0,), (20,), (20,)]}, timing={"timeout": 12})
    p.days.rows["a"] = [(0, 6, [70] + [255] * 17), (12, 24, [20] + [255] * 17)]
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        got = coordinate_costs(p, store, {"a": {0: 90}}, "a", 0, (20, 90))
    assert got[20] < got[90]


def test_confidence_fragments_weight_time_before_parent_episode_averaging(tmp_path):
    p = problem(
        {"a": [(70,), (0,), (20,), (20,)]},
        [
            truth(0, 6, source="entity", confidence=1, episode="same"),
            truth(6, 12, source="entity", confidence=0.6, episode="same"),
            truth(12, 24, "not_present", episode="empty"),
        ],
    )
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        got = coordinate_costs(p, store, {"a": {0: 90}}, "a", 0, (40,))
    assert got[40] == pytest.approx(100 * 5 * 3.6 / 9.6)


def test_search_hold_uses_chronological_geometry_across_label_edges(tmp_path):
    p = problem({"a": [(70,), (0,), (0,), (0,)]}, timing={"timeout": 6})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        got = coordinate_costs(p, store, {"a": {0: 90}}, "a", 0, (20, 90))
    assert got[20] == 0
    assert got[90] == 1000


def test_source_cost_coefficients_are_compiled_once_per_denominator(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search_scores

    p = problem({"a": [(70,), (61,), (20,), (20,)]})
    original = search_scores.cost_from_statistics
    calls = []

    def counted(stats, policy):
        calls.append(1)
        return original(stats, policy)

    monkeypatch.setattr(search_scores, "cost_from_statistics", counted)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        args = (p, store, {"a": {0: 90}}, "a", 0, tuple(range(101)))
        first = coordinate_costs(*args)
        coordinate_costs(*args)
    assert first[40] == 0
    assert len(calls) <= 5


def test_search_reuses_equivalent_crossing_coordinates(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search as module
    from tuner_under_test.engine.hybrid.search import _Search

    p = problem({"a": [(70, 0), (61, 0), (20, 0), (20, 0)]})
    calls = []
    original = module.coordinate_costs

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "coordinate_costs", counted)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = _Search(p, store)
        one = engine.costs({"a": {0: 50, 1: 50}}, "a", 0, (20, 40))
        two = engine.costs({"a": {0: 90, 1: 100}}, "a", 0, (20, 40))
    assert one == two
    assert len(calls) == 1


def test_packed_group_scoring_never_visits_vector_crossings_after_compile(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid.decision import Decision

    p = problem({"a": [(70,), (61,), (20,), (20,)]})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        args = (p, store, {"a": {0: 90}}, "a", 0, (20, 40, 90))
        expected = coordinate_costs(*args, folded=False)

        def forbidden(*args):
            raise AssertionError("per-vector candidate crossing")

        monkeypatch.setattr(Decision, "crossing", forbidden)
        assert coordinate_costs(*args) == expected


@pytest.mark.parametrize("confidence", [0.6, 0.7, 1.0])
def test_packed_parent_exactly_half_is_not_mostly_missed(tmp_path, confidence):
    p = problem(
        {"a": [(70,), (0,), (20,), (20,)]},
        [
            truth(0, 12, source="entity", confidence=confidence, episode="p"),
            truth(12, 24, "not_present", source="entity", confidence=confidence, episode="e"),
        ],
    )
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        args = p, store, {"a": {0: 90}}, "a", 0, (40,)
        assert coordinate_costs(*args) == coordinate_costs(*args, folded=False) == {40: 250}


def test_vector_runs_equal_grouped_cells():
    from itertools import groupby

    from tuner_under_test.engine.hybrid.search_data import vector_runs

    rng = random.Random(77)
    for n in (0, 1, 2, 9, 300):
        for alphabet in ((0, 1), (0, 1, 2, 255), tuple(range(256))):
            columns = [bytes(rng.choice(alphabet) for _ in range(n)) for _ in range(3)]
            expected, at = [], 0
            for _, group in groupby(zip(*columns, strict=True)):
                count = sum(1 for _ in group)
                expected.append((at, at + count))
                at += count
            assert vector_runs(columns, n) == expected
    with pytest.raises(ValueError):
        vector_runs([b"ab", b"a"], 2)


def test_saved_day_rows_equal_grouped_covered_cells(tmp_path):
    from itertools import groupby

    from tuner_under_test.engine.hybrid.search_data import _save_day

    rng = random.Random(78)
    rows = [(i * 6, i * 6 + 6, [rng.choice((3, 4, 255))] * 2 + [255] * 16) for i in range(200)]
    rows = [row for i, row in enumerate(rows) if i % 23 != 4]
    digest = fold_day([block(rows)], 0, 1200, (0, 1))
    expected, offset = [], 0
    for vector, group in groupby(zip(digest.cov, *digest.channels, strict=True)):
        count = sum(1 for _ in group)
        if vector[0] == 1:
            lo = (digest.k0 + offset) * 6
            expected.append(("r", lo, lo + count * 6, bytes(vector[1:])))
        offset += count
    with TrainingStore(tmp_path) as store:
        from tuner_under_test.engine.hybrid.search_data import _schema

        db = _schema(store)
        _save_day(db, "r", digest, 7)
        got = db.execute("SELECT radar,t,stop,vector FROM search_raw ORDER BY rowid").fetchall()
    assert got == expected and len(expected) > 20
