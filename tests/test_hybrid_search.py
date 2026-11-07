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


def test_search_memo_families_do_not_rescan_all_coordinates(tmp_path):
    from tuner_under_test.engine.hybrid.search import _Search

    p = problem({"a": [(80, 70), (0, 0)], "b": [(70, 60), (0, 0)]})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = _Search(p, store)

        class Indexed(tuple):
            def __iter__(self):
                raise AssertionError("memo family rescanned every coordinate")

        engine.coordinates = Indexed(engine.coordinates)
        got = engine.costs(engine.start, "a", 0, (0, 50, 100))
        expected = coordinate_costs(p, store, engine.start, "a", 0, (0, 50, 100))
        assert got == expected


def test_search_observed_energies_stream_without_distinct_sort(tmp_path):
    from tuner_under_test.engine.hybrid.search import _Search

    p = problem(
        {"a": [(80,), (99,), (0,), (80,)]},
        spans=[truth(0, 6), truth(12, 24, "not_present", episode="e")],
    )
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        statements = []
        store._database.set_trace_callback(statements.append)
        engine = _Search(p, store)
        assert engine.energies["a", 0] == (0, 80, 99)
        assert not any("SELECT DISTINCT vector FROM search_vectors" in sql for sql in statements)


def test_timed_score_cache_hashes_a_large_mask_once_on_hit():
    from types import SimpleNamespace

    from tuner_under_test.engine.hybrid.search_timing import _cached_score

    class Mask(int):
        hashes = 0

        def __hash__(self):
            self.hashes += 1
            return super().__hash__()

    mask = Mask(2**10000 - 1)
    key = "z", ((mask,),)
    geometry = SimpleNamespace(scores={key: 0.0})
    mask.hashes = 0
    assert _cached_score(None, geometry, None, None, key, ((mask,),)) == 0.0
    assert mask.hashes <= 1  # Python 3.14 caches a tuple's hash, so a hit may need none.


def test_search_pool_keys_use_compiled_coordinate_columns(tmp_path):
    from tuner_under_test.engine.hybrid.search import _Search

    p = problem({"a": [(80, 70), (0, 0)], "b": [(70, 60), (0, 0)]})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = _Search(p, store)
        expected = bytes(engine.start[r][c] for r, c in engine.coordinates)

        class Indexed(tuple):
            def __iter__(self):
                raise AssertionError("pool key rebuilt coordinate columns")

        engine.coordinates = Indexed(engine.coordinates)
        engine.remember(engine.start, 12.5)
        assert engine.pool == {expected: (12.5, engine.start)}


def test_timed_episode_flags_use_one_signed_count_comparison(monkeypatch):
    from tuner_under_test.engine.hybrid import search_timing

    episodes = [
        (signal, offset, bits, bits.bit_count() / 2, present, 1.0)
        for signal in (0, 1)
        for offset in (0, 7, 13)
        for bits in (1, 0b1111, 0b10101)
        for present in (False, True)
    ]
    masks = ((0b10100110110110101,), (0b10010100001111000,))
    expected = []
    for signal, offset, bits, half, present, _factor in episodes:
        count = ((masks[signal][0] >> offset) & bits).bit_count()
        expected.append(count < half if present else count > 0)
    compiled = search_timing._compiled_episodes(episodes)

    def forbidden(*args):
        raise AssertionError("episode flags rebuilt both sides of each comparison")

    monkeypatch.setattr(search_timing, "add", forbidden, raising=False)
    assert list(search_timing._episode_flags(compiled, masks)) == expected


def test_single_block_prefix_unions_do_not_zip_for_each_member(monkeypatch):
    from tuner_under_test.engine.hybrid import search_timing

    masks = [(1,), (2,), (4,), (8,)]
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return zip(*args, strict=kwargs.pop("strict", False), **kwargs)

    monkeypatch.setattr(search_timing, "zip", counted, raising=False)
    assert search_timing._prefix_suffix(masks, 1) == (
        [(0,), (1,), (3,), (7,), (15,)],
        [(15,), (14,), (12,), (8,), (0,)],
    )
    assert len(calls) <= 2


def test_search_memo_reuses_equivalent_candidate_thresholds(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search as module

    p = problem({"a": [(80,), (0,)]})
    calls = []
    original = module.coordinate_costs

    def counted(*args, **kwargs):
        calls.append(tuple(args[5]))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "coordinate_costs", counted)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = module._Search(p, store)
        first = engine.costs(engine.start, "a", 0, (40, 50))
        second = engine.costs(engine.start, "a", 0, (41, 51))
    assert first == {40: 0.0, 50: 0.0}
    assert second == {41: 0.0, 51: 0.0}
    assert calls == [(40,)]


def test_timed_coordinate_reads_its_spilled_column_once(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search_timing

    p = problem({"a": [(20,), (40,), (60,), (80,), (0,), (0,), (0,), (0,)]}, timing={"timeout": 6})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        geometry = search_timing.Geometry(p, store, budget=1)
        original = geometry.get
        reads = []

        def counted(key):
            if key[0] == "a" and key[2] == 0:
                reads.append(key)
            return original(key)

        monkeypatch.setattr(geometry, "get", counted)
        got = search_timing._zone_costs(
            p, geometry, store, p.zones[0], {"a": {0: 90}}, "a", 0, (0, 20, 40, 60), False
        )
        assert len(set(got)) > 1
        assert len(reads) == 1
        geometry.close()


@pytest.mark.parametrize("attenuate", [False, True])
@pytest.mark.parametrize("timing", [{}, {"timeout": 6}])
def test_candidate_family_memo_matches_every_integer_threshold(tmp_path, attenuate, timing):
    from tuner_under_test.engine.hybrid.search import _Search

    p = problem(
        {"a": [(80, 31), (41, 60), (25, 19), (0, 0)]},
        spans=[
            truth(0, 6, source="human", episode="p"),
            truth(6, 12, source="entity", confidence=0.7, episode="a"),
            truth(12, 24, "not_present", episode="e"),
        ],
        timing=timing,
    )
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = _Search(p, store, attenuate)
        candidates = tuple(range(101))
        for channel in (0, 1):
            got = engine.costs(engine.start, "a", channel, candidates)
            expected = coordinate_costs(
                p, store, engine.start, "a", channel, candidates, attenuate=attenuate
            )
            assert got == expected


def test_search_cost_cache_keeps_compact_exact_values_within_byte_budget(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search as module

    p = problem({"a": [(v,) for v in range(101)]})
    original = module.coordinate_costs
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "coordinate_costs", counted)
    monkeypatch.setattr(module, "MASK_CACHE_BYTES", 4096)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = module._Search(p, store)
        first = engine.costs(engine.start, "a", 0, tuple(range(101)))
        second = engine.costs(engine.start, "a", 0, tuple(range(101)))
        assert engine.memo_bytes <= 4096
    assert first == second
    assert len(calls) == 1


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


def test_finalists_preserve_crossings_supported_only_by_automatic_evidence(tmp_path):
    spans = [
        truth(0, 600, episode="human-present"),
        truth(600, 1200, source="automatic", episode="automatic-present"),
        truth(1200, 1800, "not_present", episode="human-empty"),
    ]
    p = problem({"r": [(30, 5)] * 100 + [(5, 12)] * 100 + [(5, 5)] * 100}, spans)
    radar = replace(p.radars["r"], current={0: 20, 1: 13})
    p = replace(p, radars={"r": radar}, policy=EvidencePolicy(0.3, False, {}))
    candidates = finalists(tmp_path, p)
    # Channel 1 is constant on human truth, but it detects an independently
    # supported automatic location. Repair must retain that physical crossing.
    assert any(5 <= candidate["r"][1] < 12 for candidate in candidates)


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


def test_search_crossings_retain_original_start_for_every_seed(tmp_path):
    """The prototype includes the original start even after a seed moves away."""
    from tuner_under_test.engine.hybrid.search import _Search

    p = problem({"a": [(70,), (80,), (10,), (20,)]}, current=53)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = _Search(p, store)
        assert 53 in engine.candidate_base["a", 0]


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
        got = db.execute("SELECT radar,t,stop,vector FROM search_raw ORDER BY t").fetchall()
    assert got == expected and len(expected) > 20


@pytest.mark.parametrize("confidence", [0.6, 0.7, 1.0])
def test_packed_time_lanes_fold_multiplicities_into_binary_weights(tmp_path, confidence):
    from tuner_under_test.engine.hybrid.search_masks import GroupBlock
    from tuner_under_test.engine.hybrid.search_scores import linear_cost

    p = problem({"a": [(70,), (0,)]})
    rows = [
        (state, "human", str(i), confidence, bytes([i % 101] + [255] * 17), i + 1)
        for state in ("not_present", "present")
        for i in range(1024)
    ]
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        block = GroupBlock(p, store, p.zones[0], rows, False)
        for threshold in (0, 20, 50, 99, 100):
            mask = block.mask(0, threshold)
            expected = linear_cost(store, "room", block.statistics(mask, False), p.policy)
            got = block.time_cost(mask, store, "room", p.policy)
            assert round(got, 9) == round(expected, 9)
        factors, _bits = block.time_formula[1]
        assert len(factors) <= 22


def test_running_search_reuses_unchanged_member_signatures(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search as module
    from tuner_under_test.engine.hybrid import search_timing

    p = problem({"a": [(80, 60), (0, 0)], "b": [(70, 50), (0, 0)]}, timing={"timeout": 6})
    scans = []
    original = search_timing._pattern_signature

    def counted(values):
        signature = original(values)
        scans.append(signature)
        return signature

    monkeypatch.setattr(search_timing, "_pattern_signature", counted)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = module._Search(p, store)
        pattern = {rid: module._Thresholds(values) for rid, values in engine.start.items()}
        one = engine._family(pattern, "a", 0)
        first = pattern["b"].signature()
        engine.coordinate(pattern, "a", 0)
        two = engine._family(pattern, "a", 0)
        assert one == two
        assert pattern["b"].signature() is first
        assert scans


def test_packed_blocks_admit_large_compact_groups_by_bytes(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search_masks

    p = problem({"a": [(70,), (0,)]})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        store._database.execute("DELETE FROM search_groups")
        count = search_masks.PACKED_ROWS + 1
        store._database.executemany(
            "INSERT INTO search_groups VALUES (?,?,?,?,?,?,?)",
            (
                (
                    p.zones[0].group_id,
                    "present" if i % 2 else "not_present",
                    "human",
                    "p" if i % 2 else "e",
                    1.0,
                    bytes([i % 101, i // 101 % 101, i // 10201] + [255] * 15),
                    1,
                )
                for i in range(count)
            ),
        )
        packed = search_masks.packed_block(p, store, p.zones[0], False)
        assert packed is not None
        assert packed.used <= search_masks.PACKED_BYTES
        assert len(packed.columns[0]) == count
        monkeypatch.setattr(search_masks, "PACKED_BYTES", 100)
        store._packed_key = None
        assert search_masks.packed_block(p, store, p.zones[0], False) is None


def test_packed_group_construction_accepts_one_pass_rows(tmp_path):
    from tuner_under_test.engine.hybrid.search_masks import GroupBlock

    p = problem({"a": [(70,), (0,)]})
    rows = [("present", "human", "p", 1.0, bytes([70] + [255] * 17), 2)]
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        packed = GroupBlock(p, store, p.zones[0], iter(rows), False)
        assert packed.statistics(1, True) == {"present/human": [12.0, 0.0, 1.0, 0.0]}


def test_packed_score_masks_share_the_byte_cache_budget(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search_masks

    p = problem({"a": [(70,), (0,)]})
    rows = [("present", "human", "p", 1.0, bytes([70] + [255] * 17), 2)]
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        size = search_masks.GroupBlock(p, store, p.zones[0], rows, False).used
        monkeypatch.setattr(search_masks, "PACKED_CACHE_BYTES", size + 1024)
        packed = search_masks.GroupBlock(p, store, p.zones[0], rows, False)
        for mask in range(64):
            search_masks._score(packed, mask, False, store, "z", p.policy)
        retained = sum(256 + (key[1].bit_length() + 7) // 8 for key in packed.scores)
        assert retained <= packed.limit
        assert packed.cache_bytes <= packed.limit


def test_search_family_keys_intern_each_members_exact_crossing_vector(tmp_path):
    from tuner_under_test.engine.hybrid.search import _Search

    p = problem({rid: [tuple(range(60, 78)), (0,) * 18] for rid in ("a", "b", "c")})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = _Search(p, store)
        original = engine._family(engine.start, "a", 0)
        assert len(original) <= 8 * len(p.radars)
        pattern = {rid: dict(values) for rid, values in engine.start.items()}
        pattern["a"][0] = 0
        assert engine._family(pattern, "a", 0) == original
        pattern["b"][0] = 0
        assert engine._family(pattern, "a", 0) != original
        assert engine.costs(pattern, "a", 0, (0, 40, 90)) == coordinate_costs(
            p, store, pattern, "a", 0, (0, 40, 90)
        )


def test_packed_binary_lanes_do_not_expand_unique_confidence_weights(tmp_path):
    from tuner_under_test.engine.hybrid.search_masks import GroupBlock

    p = problem({"a": [(70,), (0,)]})
    rng = random.Random(997)
    rows = [
        (
            "present",
            "human",
            str(i),
            0.55 + rng.random() * 0.45,
            bytes([i % 101] + [255] * 17),
            1023,
        )
        for i in range(64)
    ]
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        packed = GroupBlock(p, store, p.zones[0], rows, False)
        packed.time_cost(1, store, "z", p.policy)
        assert len(packed.time_formula[1][0]) <= len(rows)


def test_packed_formulas_and_member_unions_are_charged_to_the_byte_budget(tmp_path):
    from tuner_under_test.engine.hybrid import search_masks

    p = problem({"a": [(70,), (0,)]})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        packed = search_masks.packed_block(p, store, p.zones[0], False)
        before = packed.used
        packed.time_cost(1, store, "z", p.policy)
        assert packed.used > before
        assert packed.limit == search_masks.PACKED_CACHE_BYTES - packed.used
        packed.base({"a": {0: 50}}, "a", 0)
        assert any(identity == id(packed.unions) for identity, _key in packed.cache_order)
        assert packed.used + packed.cache_bytes <= search_masks.PACKED_CACHE_BYTES


def test_packed_replaced_member_union_releases_its_previous_cache_charge(tmp_path):
    from tuner_under_test.engine.hybrid import search_masks

    p = problem({"a": [(70,), (0,)]})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        packed = search_masks.packed_block(p, store, p.zones[0], False)
        for threshold in range(100):
            packed.base({"a": {0: threshold}}, "a", 0)
        assert packed.cache_bytes == sum(size for _cache, size in packed.cache_order.values())


def test_packed_episode_statistics_reads_only_each_episodes_local_bytes(tmp_path):
    from tuner_under_test.engine.hybrid.search_masks import GroupBlock

    class WholeHistoryMask(int):
        def __and__(self, other):
            raise AssertionError("episode scanned the whole-history mask")

    p = problem({"a": [(70,), (0,)]})
    rows = [("present", "human", str(i), 1.0, bytes([70] + [255] * 17), 2) for i in range(100)]
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        packed = GroupBlock(p, store, p.zones[0], rows, False)
        assert packed.statistics(WholeHistoryMask((1 << 100) - 1), True) == {
            "present/human": [1200.0, 0.0, 100.0, 0.0]
        }


@pytest.mark.parametrize("support", [False, True])
def test_packed_construction_translates_columns_without_per_vector_calls(
    tmp_path, monkeypatch, support
):
    from tuner_under_test.engine.hybrid.decision import Decision
    from tuner_under_test.engine.hybrid.search_masks import GroupBlock

    p = problem({"a": [(70,), (0,)]})

    class Support:
        def supports(self, key, value):
            return value % 3 != 1

    if support:
        p = replace(p, policy=replace(p.policy, quality={"models": {"a": Support()}}))
    rows = [
        (state, source, str(i), 1.0, bytes([i, 255] + [i] * 16), i + 1)
        for state in ("not_present", "present")
        for source in ("entity", "human")
        for i in range(101)
    ]
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        decision = Decision(p, store, p.zones[0], {"a": {0: 90}}, "a", 0, True)
        expected = tuple(
            bytes(c)
            for c in zip(*(decision.energies(row[4], row[0], row[1]) for row in rows), strict=True)
        )

        def forbidden(*args):
            raise AssertionError("packed construction translated an individual vector")

        monkeypatch.setattr(Decision, "energies", forbidden)
        packed = GroupBlock(p, store, p.zones[0], iter(rows), True)
        assert packed.columns == expected


@pytest.mark.parametrize("episodes", [False, True])
def test_packed_dominated_gate_scores_only_its_lowest_candidate(tmp_path, monkeypatch, episodes):
    from tuner_under_test.engine.hybrid import search_masks

    p = problem({"a": [(40, 40), (70, 70), (0, 0), (0, 0)]})
    pattern = {"a": {0: 90, 1: 0}}
    candidates = tuple(range(101))
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        packed = search_masks.packed_block(p, store, p.zones[0], False)
        base = packed.base(pattern, "a", 0)
        calls = []
        original = packed.mask

        def counted(position, threshold):
            calls.append(threshold)
            return original(position, threshold)

        monkeypatch.setattr(packed, "mask", counted)
        got = search_masks._coordinate_scores(
            packed, base, 0, candidates, episodes, store, "z", p.policy
        )
        assert got == dict.fromkeys(candidates, 0.0)
        assert calls == [0]


def test_packed_large_column_crossings_translate_at_most_seven_times(tmp_path):
    from tuner_under_test.engine.hybrid import search_masks

    calls = []

    class Column(bytes):
        def __getitem__(self, index):
            value = super().__getitem__(index)
            return Column(value) if isinstance(index, slice) else value

        def translate(self, table):
            calls.append(1)
            return super().translate(table)

    p = problem({"a": [(40,), (70,), (0,), (0,)]})
    rows = [("present", "human", "p", 1.0, bytes([i % 101]) * 18, 1) for i in range(5000)]
    with TrainingStore(tmp_path) as store:
        packed = search_masks.GroupBlock(p, store, p.zones[0], iter(rows), False)
        column = Column(packed.columns[0])
        packed.columns = (column, *packed.columns[1:])
        for threshold in range(101):
            expected = sum(1 << i for i, v in enumerate(column) if v > threshold)
            assert packed.mask(0, threshold) == expected
        assert len(calls) <= 7
        assert packed.used + packed.cache_bytes <= search_masks.PACKED_CACHE_BYTES


def test_packed_member_signature_reuses_unchanged_threshold_settings(tmp_path):
    from tuner_under_test.engine.hybrid import search_masks

    calls = []

    class Settings(dict):
        def signature(self):
            return tuple(self.items())

        def get(self, *args):
            calls.append(1)
            return super().get(*args)

    p = problem({"a": [(40,), (70,), (0,), (0,)]})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        packed = search_masks.packed_block(p, store, p.zones[0], False)
        values = Settings({0: 40})
        first = packed.base({"a": values}, "a", 0)
        assert packed.base({"a": values}, "a", 0) == first
        assert len(calls) == 18
        values[0] = 70
        assert packed.base({"a": values}, "a", 0) == first
        assert len(calls) == 36


def test_large_conditional_histogram_does_not_build_every_threshold_mask(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search_masks
    from tuner_under_test.engine.hybrid.search_scores import zone_policy

    p = problem({"a": [(40,), (70,), (0,), (0,)]})
    rows = [
        (
            "present" if i % 2 else "not_present",
            "human",
            str(i % 2),
            1.0,
            bytes([i % 101]) * 18,
            i % 3 + 1,
        )
        for i in range(65536)
    ]
    rows.sort(key=lambda row: row[:3])
    with TrainingStore(tmp_path) as store:
        packed = search_masks.GroupBlock(p, store, p.zones[0], iter(rows), False)
        candidates = tuple(range(101))
        policy = zone_policy(p, p.zones[0])
        base = packed.mask(0, 80)
        expected = {
            t: packed.time_cost(base | packed.mask(0, t), store, "z", policy) for t in candidates
        }
        calls = []
        original = packed.mask

        def counted(*args):
            calls.append(1)
            return original(*args)

        monkeypatch.setattr(packed, "mask", counted)
        actual = search_masks._coordinate_scores(
            packed, base, 0, candidates, False, store, "z", policy
        )
        assert actual == expected
        assert len(calls) <= 1


def test_large_conditional_histogram_reuses_energy_planes(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search_masks as module
    from tuner_under_test.engine.hybrid.search_scores import zone_policy

    p = problem({"a": [(40,), (70,), (0,), (0,)]})
    rows = [("present", "human", "p", 1.0, bytes([i % 101]) * 18, 1) for i in range(65536)]
    rows += [("not_present", "human", "e", 1.0, bytes(18), 1)]
    with TrainingStore(tmp_path) as store:
        packed = module.GroupBlock(p, store, p.zones[0], iter(rows), False)
        policy = zone_policy(p, p.zones[0])
        candidates = tuple(range(101))
        expected = {t: packed.time_cost(packed.mask(0, t), store, "z", policy) for t in candidates}

        class Column(bytes):
            def __iter__(self):
                raise AssertionError("conditional histogram scanned individual cells")

        monkeypatch.setattr(packed, "columns", (Column(packed.columns[0]), *packed.columns[1:]))
        assert packed.conditional_times(0, 0, candidates, store, "z", policy) == expected


def test_packed_energy_buffer_copies_bounded_batches():
    from tuner_under_test.engine.hybrid.search_masks import _buffer_rows

    class Buffer(bytearray):
        copies = 0

        def extend(self, values):
            self.copies += 1
            super().extend(values)

    rows = [("present", "human", "p", 1.0, bytes([i % 101]) * 18, 1) for i in range(10000)]
    joined = Buffer()
    assert list(_buffer_rows(iter(rows), joined)) == rows
    assert joined == b"".join(row[4] for row in rows)
    assert joined.copies <= 3


def test_search_reuses_candidate_crossings_and_domain_layout(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search as module

    p = problem({"a": [(80,), (0,)]})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = module._Search(p, store)
        pattern = module._Pattern(engine.start, engine)
        candidates = p.radars["a"].domains[0]
        expected = engine.costs(pattern, "a", 0, candidates)
        bisects, sorts = [], []
        original = module.bisect_right

        def counted_bisect(*args):
            bisects.append(1)
            return original(*args)

        def counted_sort(*args, **kwargs):
            sorts.append(1)
            return sorted(*args, **kwargs)

        monkeypatch.setattr(module, "bisect_right", counted_bisect)
        monkeypatch.setattr(module, "sorted", counted_sort, raising=False)
        for _ in range(2):
            assert engine.costs(pattern, "a", 0, candidates) == expected
        assert not bisects
        assert not sorts


def test_search_ranks_only_the_four_best_tail_thresholds(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search as module

    p = problem({"a": [(80,), (0,)]})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = module._Search(p, store)
        pattern = module._Pattern(engine.start, engine)
        original = sorted
        sizes = []

        def counted(values, **kwargs):
            values = list(values)
            sizes.append(len(values))
            return original(values, **kwargs)

        monkeypatch.setattr(module, "sorted", counted, raising=False)
        engine.coordinate(pattern, "a", 0)
    assert max(sizes) < 101


@pytest.mark.parametrize("budget", [1, 4096])
def test_candidate_layout_cache_stays_bounded_and_exact(tmp_path, monkeypatch, budget):
    from tuner_under_test.engine.hybrid import search as module

    p = problem({"a": [(80, 70, 60, 50), (0, 0, 0, 0)]})
    monkeypatch.setattr(module, "MASK_CACHE_BYTES", budget)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = module._Search(p, store)
        for channel in (*p.radars["a"].channels, 0):
            candidates = tuple(range(101))
            assert engine.costs(engine.start, "a", channel, candidates) == coordinate_costs(
                p, store, engine.start, "a", channel, candidates
            )
            assert engine.layout_bytes <= budget


def test_plane_histograms_match_scalar_counts():
    from itertools import accumulate

    from tuner_under_test.engine.hybrid.search_masks import _PLANES, _plane_counts

    rng = random.Random(11732)
    for size in (0, 1, 7, 65, 2048):
        for _ in range(20):
            column = bytes(rng.randrange(101) for _ in range(size))
            planes = tuple(
                int(column[::-1].translate(table), 2) if column else 0 for table in _PLANES
            )
            lane, base = rng.getrandbits(size), rng.getrandbits(size)
            bins = [0] * 102
            for i, value in enumerate(column):
                if lane & (1 << i) and not base & (1 << i):
                    bins[value] += 1
            assert _plane_counts(planes, lane, base) == (
                (lane & base).bit_count(),
                tuple(accumulate(reversed(bins))),
            )


def test_packed_base_reuses_unchanged_other_member_unions(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search_masks

    p = problem({f"r{i}": [(80,), (0,)] for i in range(10)})
    pattern = {rid: {0: 50} for rid in p.radars}
    calls = []

    class Mask(int):
        def __or__(self, other):
            calls.append(1)
            return Mask(int(self) | int(other))

        def __ror__(self, other):
            calls.append(1)
            return Mask(int(self) | int(other))

    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        packed = search_masks.packed_block(p, store, p.zones[0], False)
        original = packed._unions

        def counted(*args):
            signature, prefix, suffix = original(*args)
            return signature, list(map(Mask, prefix)), list(map(Mask, suffix))

        monkeypatch.setattr(packed, "_unions", counted)
        expected = packed.base(pattern, "r0", 0)
        calls.clear()
        assert packed.base(pattern, "r0", 0) == expected
        assert len(calls) <= 4


def test_search_memo_keeps_repeated_wide_families_in_its_byte_budget(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search as module

    p = problem({f"r{i}": [(value,) for value in range(32)] for i in range(30)})
    monkeypatch.setattr(module, "MASK_CACHE_BYTES", 16384)
    original = module.coordinate_costs
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "coordinate_costs", counted)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = module._Search(p, store)
        pattern = {rid: dict(values) for rid, values in engine.start.items()}
        first = None
        for threshold in range(22):
            pattern["r1"][0] = threshold
            result = engine.costs(pattern, "r0", 0, (0, 100))
            if threshold == 0:
                first = result
        pattern["r1"][0] = 0
        assert engine.costs(pattern, "r0", 0, (0, 100)) == first
        assert engine.memo_bytes <= module.MASK_CACHE_BYTES
        assert len(calls) == 22


def test_compact_memo_families_preserve_ids_and_growth_boundaries():
    import random
    from array import array

    from tuner_under_test.engine.hybrid.search import _memo_family

    rng = random.Random(715)
    small = [tuple(rng.randrange(2**16) for _ in range(5)) for _ in range(300)]
    compact = {_memo_family(array("Q", values).tobytes(), 2**16 - 1) for values in small}
    assert len(compact) == len(set(small))
    assert all(len(key) == 11 for key in compact)
    for values in ((0,), (65535,), (65536,), (65537,), (2**64 - 1,)):
        family = array("Q", values).tobytes()
        assert _memo_family(family, 2**16) == b"\1" + family
    low = array("Q", (1, 1)).tobytes()
    high = array("Q", (1, 65537)).tobytes()
    assert _memo_family(low, 65537) != _memo_family(high, 65537)
    assert _memo_family(low, 65535) != _memo_family(low, 65536)


def test_running_pattern_key_updates_only_the_changed_coordinate(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search as module

    p = problem({"a": [(80, 20), (0, 40)], "b": [(10, 90), (0, 70)]})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = module._Search(p, store)
        plain = {rid: dict(values) for rid, values in engine.start.items()}
        pattern = module._Pattern(plain, engine)
        expected = module._key(plain, engine.coordinates)

        def forbidden(self, channel):
            raise AssertionError("pattern key reread an unchanged threshold")

        monkeypatch.setattr(module._Thresholds, "__getitem__", forbidden, raising=False)
        assert engine.pattern_key(pattern) == expected
        pattern["a"][0] = 33
        plain["a"][0] = 33
        assert engine.pattern_key(pattern) == module._key(plain, engine.coordinates)
        assert engine.pattern_key(pattern) is engine.pattern_key(pattern)
