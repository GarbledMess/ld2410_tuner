"""Chronological search dilation stays independent of annotation boundaries."""

from dataclasses import replace

import pytest
import test_tuner  # noqa: F401
from test_hybrid_evidence import truth
from test_hybrid_search import problem
from tuner_under_test.engine.hybrid.search_data import collect_training
from tuner_under_test.engine.hybrid.training import TrainingStore


def test_packed_dilation_matches_scalar_with_gaps():
    import random

    from tuner_under_test.engine.hybrid.search_timing import dilate, pack_mask

    rng = random.Random(893)
    for size in (0, 1, 17, 97):
        for hold in (0, 1, 3, 16, 100):
            column = bytes(rng.choice((0, 40, 41, 255)) for _ in range(size))
            coverage = [rng.randrange(2) for _ in range(size)]
            covered = sum(value << i for i, value in enumerate(coverage))
            hits = pack_mask(column, 40) & covered
            expected = 0
            remaining = 0
            for i in range(size):
                if not coverage[i]:
                    remaining = 0
                    continue
                if hits & (1 << i):
                    remaining = hold + 1
                if remaining:
                    expected |= 1 << i
                    remaining -= 1
            assert dilate(hits, hold, covered) == expected


def costs(tmp_path, p, values=(20, 90), *, attenuate=False):
    from tuner_under_test.engine.hybrid.search_timing import timed_coordinate_costs

    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        return timed_coordinate_costs(p, store, {"a": {0: 90}}, "a", 0, values, attenuate=attenuate)


def test_search_dilation_carries_across_label_edges(tmp_path):
    p = problem({"a": [(70,), (0,), (0,), (0,)]}, timing={"timeout": 6})
    assert costs(tmp_path, p) == {20: 0, 90: 1000}


def test_candidates_do_not_replay_sql_intervals(tmp_path):
    from tuner_under_test.engine.hybrid.search_timing import timed_coordinate_costs

    p = problem({"a": [(70,), (0,), (0,), (0,)]}, timing={"timeout": 6})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        statements = []
        store._database.set_trace_callback(statements.append)
        assert timed_coordinate_costs(p, store, {"a": {0: 90}}, "a", 0, (20, 90)) == {
            20: 0,
            90: 1000,
        }
        assert not any("search_timing_intervals" in sql for sql in statements)


def test_search_dilation_rounds_hold_and_off_up_to_cells(tmp_path):
    p = problem(
        {"a": [(70,), (0,), (0,), (0,)]}, timing={"timeout": 0.1, "off_delay": 0.1, "on_delay": 100}
    )
    assert costs(tmp_path, p)[20] == 0


def test_search_dilation_stops_at_physical_gap(tmp_path):
    p = problem({"a": [(70,), (0,), (0,), (0,)]}, timing={"timeout": 18})
    p.days.rows["a"] = [(0, 6, [70] + [255] * 17), (12, 24, [0] + [255] * 17)]
    p = replace(
        p,
        zones=(
            replace(p.zones[0], truth=(truth(0, 6, "not_present"), truth(12, 24, episode="p"))),
        ),
    )
    assert costs(tmp_path, p)[20] == 1200


def test_search_dilation_stops_at_gap_inside_partial_cell(tmp_path):
    p = problem(
        {"a": [(70,), (0,), (0,), (0,)]},
        spans=(truth(0, 6, "not_present", episode="e"), truth(12, 24, episode="p")),
        timing={"timeout": 18},
    )
    p.days.rows["a"] = [
        (0, 6, [70] + [255] * 17),
        (6, 7, [0] + [255] * 17),
        (11, 24, [0] + [255] * 17),
    ]
    assert costs(tmp_path, p) == {20: 1200, 90: 1000}


def test_search_attenuates_only_present_runs(tmp_path):
    p = problem({"a": [(70,), (0,), (30,), (0,)]}, timing={"timeout": 0.1})
    assert costs(tmp_path, p, (45,), attenuate=False)[45] == 0
    assert costs(tmp_path, p, (45,), attenuate=True)[45] == 1000


class Unsupported:
    def supports(self, key, value):
        return False


def test_coordinate_reuses_compiled_geometry_without_sql(tmp_path):
    from tuner_under_test.engine.hybrid.search_timing import timed_coordinate_costs

    p = problem({"a": [(70,), (0,), (0,), (0,)]}, timing={"timeout": 6})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        pattern = {"a": {0: 90}}
        expected = timed_coordinate_costs(p, store, pattern, "a", 0, (20, 90))
        statements = []
        store._database.set_trace_callback(statements.append)
        assert timed_coordinate_costs(p, store, pattern, "a", 0, (20, 90)) == expected
        assert statements == []


def test_timed_coordinate_cannot_activate_a_zone_without_that_radar(tmp_path):
    from tuner_under_test.engine.hybrid.search_timing import timed_coordinate_costs

    p = problem(
        {"a": [(70,), (70,), (0,), (0,)], "b": [(0,), (0,), (0,), (0,)]}, timing={"timeout": 6}
    )
    p = replace(p, zones=(replace(p.zones[0], members=("b",)),))
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        values = timed_coordinate_costs(p, store, {"a": {0: 90}, "b": {0: 90}}, "a", 0, (20, 90))
        assert values[20] == values[90] == 1000


def test_spilled_timed_shards_equal_compacted_halos(tmp_path, monkeypatch):
    import random

    from tuner_under_test.engine.hybrid import search_timing

    rng = random.Random(921)
    p = problem(
        {rid: [(rng.randrange(101), rng.randrange(101)) for _ in range(51)] for rid in ("a", "b")},
        spans=(truth(30, 90), truth(120, 168, "not_present"), truth(210, 300, episode="p2")),
        timing={"timeout": 19, "off_delay": 7},
    )
    pattern = {rid: {0: 60, 1: 85} for rid in p.radars}
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        expected = search_timing.timed_coordinate_costs(p, store, pattern, "a", 0, (20, 60, 90))
        monkeypatch.setattr(search_timing, "SHARD_CELLS", 7)
        monkeypatch.setattr(search_timing, "PROJECTED_CELLS", 0)
        store._timing_geometry = search_timing.Geometry(p, store, budget=0)
        assert len(store._timing_geometry.blocks) > 1
        assert store._timing_geometry.used == 0
        assert store._timing_geometry.locations
        assert (
            search_timing.timed_coordinate_costs(p, store, pattern, "a", 0, (20, 60, 90))
            == expected
        )


def test_coordinate_memo_keeps_hits_before_shared_coverage(tmp_path):
    from tuner_under_test.engine.hybrid.search import _Search

    p = problem(
        {"a": [(50,), (0,), (0,), (0,)], "b": [(0,), (0,), (0,), (0,)]},
        spans=(truth(6, 12), truth(12, 24, "not_present")),
        timing={"timeout": 6},
    )
    p.days.rows["b"] = p.days.rows["b"][1:]
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        engine = _Search(p, store)
        assert engine.costs({"a": {0: 40}, "b": {0: 90}}, "b", 0, (90,)) == {90: 0}
        assert engine.costs({"a": {0: 60}, "b": {0: 90}}, "b", 0, (90,)) == {90: 1000}


def test_episode_masks_keep_local_offsets_even_late_in_history():
    from tuner_under_test.engine.hybrid.search_timing import _add_bits

    groups = {}
    _add_bits(groups, "episode", 6_000_000, 3)
    _add_bits(groups, "episode", 6_000_004, 1)
    assert groups == {"episode": (6_000_000, 19)}


def test_search_supported_positive_does_not_hide_false_activation(tmp_path):
    p = problem({"a": [(70,), (0,), (70,), (0,)]}, timing={"timeout": 0.1})
    p = replace(
        p,
        zones=(
            replace(
                p.zones[0], truth=(truth(0, 12, source="entity"), truth(12, 24, "not_present"))
            ),
        ),
        policy=replace(p.policy, quality={"models": {"a": Unsupported()}}),
    )
    assert costs(tmp_path, p)[20] == 1200


@pytest.mark.parametrize("resident", (True, False))
@pytest.mark.parametrize("supported", (False, True))
def test_streamed_parent_counters_spill_without_sql_and_preserve_confidence(
    tmp_path, monkeypatch, supported, resident
):
    from tuner_under_test.engine.hybrid import search_timing

    spans = tuple(
        truth(
            i * 6,
            (i + 1) * 6,
            "present" if i % 4 < 2 else "not_present",
            "human" if i % 8 < 4 else "entity",
            0.4 + (i % 3) * 0.2,
            episode=f"p{i // 2}",
        )
        for i in range(120)
    )
    p = problem({"a": [(i * 17 % 101,) for i in range(120)]}, spans=spans, timing={"timeout": 7})
    p = replace(
        p,
        zones=(*p.zones, replace(p.zones[0], group_id="other", truth=spans[20:80])),
        policy=replace(
            p.policy,
            automatic_weight=0.5,
            quality={"models": {"a": Unsupported()}} if supported else {},
        ),
    )
    pattern = {"a": {0: 60}}
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        expected = search_timing.timed_coordinate_costs(p, store, pattern, "a", 0, (20, 60, 90))
        monkeypatch.setattr(search_timing, "SHARD_CELLS", 7)
        monkeypatch.setattr(search_timing, "PROJECTED_CELLS", 0)
        if not resident:
            monkeypatch.setattr(search_timing, "RESIDENT_PARENTS", 0)
        geometry = search_timing.Geometry(p, store, budget=0)
        store._timing_geometry = geometry
        assert geometry.parent_counts == {"z": 60, "other": 30}
        assert (geometry.parent_rows["z"] is not None) == resident
        statements = []
        store._database.set_trace_callback(statements.append)
        assert (
            search_timing.timed_coordinate_costs(p, store, pattern, "a", 0, (20, 60, 90))
            == expected
        )
        assert statements == []


def test_single_block_streaming_fallback_keeps_parent_confidence_and_projection(
    tmp_path, monkeypatch
):
    from tuner_under_test.engine.hybrid import search_timing

    p = problem(
        {"a": [(0,), (70,), (0,), (70,), (0,), (0,), (70,), (0,)]},
        spans=(
            truth(12, 18, confidence=0.3),
            truth(18, 24, confidence=0.9),
            truth(36, 48, "not_present", source="entity", confidence=0.7),
        ),
        timing={"timeout": 7},
    )
    pattern = {"a": {0: 90}}
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        expected = search_timing.timed_coordinate_costs(p, store, pattern, "a", 0, (20, 90))
        monkeypatch.setattr(search_timing, "_formula_fits", lambda geometry, zone: False)
        store._timing_geometry = search_timing.Geometry(p, store)
        assert len(store._timing_geometry.blocks) == 1
        assert store._timing_geometry.parent_counts == {"z": 2}
        assert search_timing.timed_coordinate_costs(p, store, pattern, "a", 0, (20, 90)) == expected


def test_oversize_timed_caches_return_results_without_retaining_them(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search_timing

    p = problem({"a": [(70,), (0,), (0,), (0,)]}, timing={"timeout": 6})
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        expected = search_timing.timed_coordinate_costs(p, store, {"a": {0: 90}}, "a", 0, (20, 90))
        geometry = search_timing.Geometry(p, store)
        store._timing_geometry = geometry
        monkeypatch.setattr(search_timing, "_mask_bytes", lambda masks: 9 * 1024**2)
        assert (
            search_timing.timed_coordinate_costs(p, store, {"a": {0: 90}}, "a", 0, (20, 90))
            == expected
        )
        assert not geometry.current
        assert not geometry.scores
        assert not geometry.coordinate_scores
        assert not geometry.masks


class SplitDays:
    """The synthetic rows cut into several day digests, some days missing."""

    def __init__(self, rows, cells, missing=()):
        self.rows, self.cells, self.missing = rows, cells, set(missing)

    def dates(self, rid):
        return iter(range(-(-len(self.rows[rid]) // self.cells)))

    def digest(self, rid, date, channels):
        from test_hybrid_digest import block
        from tuner_under_test.engine.hybrid.digest import fold_day

        rows = self.rows[rid][date * self.cells : (date + 1) * self.cells]
        if date in self.missing or not rows:
            return None
        return fold_day([block(rows)], rows[0][0], rows[-1][1], channels)


def _long_problem(seed):
    import random

    rng = random.Random(seed)
    vectors = {rid: [(rng.randrange(101), rng.randrange(101)) for _ in range(400)] for rid in "ab"}
    spans = (
        truth(30, 210),
        truth(600, 750, "not_present", source="entity", confidence=0.6, episode="e1"),
        truth(1200, 1290, episode="p2"),
        truth(1800, 1950, "not_present", episode="e2"),
        truth(2280, 2394, source="entity", confidence=0.8, episode="p3"),
    )
    p = problem(vectors, spans=spans, timing={"timeout": 19, "off_delay": 7})
    p.days.rows["b"] = [row for i, row in enumerate(p.days.rows["b"]) if i % 37 != 5]
    return replace(p, days=SplitDays(p.days.rows, 60, missing=(3,)))


@pytest.mark.parametrize("seed", (5, 6, 7))
def test_projected_geometry_spans_many_physical_blocks_with_identical_costs(
    tmp_path, monkeypatch, seed
):
    """One projected block replaces fixed physical blocks without changing a cost."""
    from tuner_under_test.engine.hybrid import search_timing

    p = _long_problem(seed)
    patterns = ({rid: {0: 60, 1: 85} for rid in "ab"}, {"a": {0: 30, 1: 95}, "b": {0: 75, 1: 10}})
    monkeypatch.setattr(search_timing, "SHARD_CELLS", 60)
    results = []
    for limit in (search_timing.PROJECTED_CELLS, 0):
        monkeypatch.setattr(search_timing, "PROJECTED_CELLS", limit)
        with TrainingStore(tmp_path) as store:
            collect_training(p, store)
            geometry = search_timing.Geometry(p, store, budget=0)
            store._timing_geometry = geometry
            assert (len(geometry.blocks) == 1) == bool(limit)
            assert (geometry.projection is not None) == bool(limit)
            results.append(
                [
                    search_timing.timed_coordinate_costs(
                        p, store, pattern, rid, c, (0, 20, 45, 60, 90, 100), attenuate=attenuate
                    )
                    for pattern in patterns
                    for rid in "ab"
                    for c in (0, 1)
                    for attenuate in (False, True)
                ]
            )
    assert results[0] == results[1]


def test_spilled_geometry_reads_only_the_masked_channel(tmp_path, monkeypatch):
    """A cold channel mask reads one raw column, never every channel of the radar."""
    from pathlib import Path

    from tuner_under_test.engine.hybrid import search_timing

    p = _long_problem(5)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        geometry = search_timing.Geometry(p, store, budget=0)
        reads = []
        original = Path.read_bytes

        def read_bytes(path):
            data = original(path)
            reads.append(len(data))
            return data

        monkeypatch.setattr(Path, "read_bytes", read_bytes)
        geometry.channel_mask(p.zones[0], "a", 1, 40, 0, False)
        column = geometry.get(("a", geometry.blocks[0][0], 1))
        assert isinstance(column, bytes) and len(column) == geometry.projected_size
        assert reads and sum(reads) < 2 * len(column) + 4096
