"""Exact histogram construction and bounded per-column bookkeeping."""

import random
from dataclasses import replace

import pytest
import test_tuner  # noqa: F401
from test_hybrid_evidence import truth
from test_hybrid_search import problem
from tuner_under_test.engine.hybrid.search import _count_groups
from tuner_under_test.engine.hybrid.training import TrainingStore


class CountedSources(dict):
    def __init__(self):
        super().__init__()
        self.lookups = 0

    def setdefault(self, key, default=None):
        self.lookups += 1
        return super().setdefault(key, default)


def test_histogram_initializes_source_once_per_observed_column():
    sources = CountedSources()
    bins = {("r", 0): {"present": sources}}
    groups = {("present", "human", 3, 2): bytearray([0, 255, 40, 255, 40, 255, 100, 255])}

    _count_groups(groups, [(("r", 0), 0)], bins)

    expected = [0.0] * 101
    expected[0], expected[40], expected[100] = 18, 36, 18
    assert sources == {"human": expected}
    assert sources.lookups == 1


def test_missing_energy_column_does_not_add_source_histogram():
    sources = CountedSources()
    bins = {("r", 0): {"present": sources}}
    _count_groups({("present", "human", 3, 1): bytearray([101, 255])}, [(("r", 0), 0)], bins)
    assert sources == {}
    assert sources.lookups == 0


@pytest.mark.parametrize("intervals", [(), ((0, 6),), ((1, 13),), ((0, 24),), ((0, 6), (18, 24))])
def test_masked_histograms_equal_retained_row_scan(tmp_path, intervals):
    from tuner_under_test.engine.hybrid import search as module

    p = problem(
        {"a": [(70,), (60,), (30,), (0,)]},
        spans=[
            truth(0, 6, source="human", episode="h"),
            truth(6, 12, source="entity", episode="a"),
            truth(12, 24, "not_present", episode="e"),
        ],
    )
    with TrainingStore(tmp_path) as store:
        module.collect_training(p, store)
        expected = {("a", 0): {"present": {}, "not_present": {}}}
        zone = p.zones[0]
        module._zone_histograms(
            module._zone_rows(store, zone, intervals), [(("a", 0), 0)], expected
        )
        baseline = module.class_bins(p, store)
        actual = module.class_bins(p, store, {zone.group_id: intervals})
        assert actual == expected
        assert module.class_bins(p, store) == baseline


def test_masked_histogram_only_counts_removed_rows_after_cache_warmup(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search as module

    p = problem({"a": [(i,) for i in range(40)]})
    original = module._zone_histograms
    work = []

    def counted(rows, positions, bins):
        rows = list(rows)
        work.append(len(rows))
        return original(rows, positions, bins)

    with TrainingStore(tmp_path) as store:
        module.collect_training(p, store)
        module.class_bins(p, store)
        monkeypatch.setattr(module, "_zone_histograms", counted)
        module.class_bins(p, store, {p.zones[0].group_id: ((0, 6),)})
        assert sum(work) == 1
        assert store._histogram_bytes <= module.HISTOGRAM_BYTES


@pytest.mark.parametrize("budget", [1, 8 * 1024**2])
def test_cached_histograms_match_streaming_across_zones_and_masks(tmp_path, monkeypatch, budget):
    from tuner_under_test.engine.hybrid import search as module

    rng = random.Random(703)
    vectors = {
        rid: [tuple(rng.choice((0, 20, 70, 255)) for _ in range(2)) for _ in range(40)]
        for rid in ("a", "b")
    }
    p = problem(vectors)
    p = replace(p, zones=(*p.zones, replace(p.zones[0], group_id="second", members=("a",))))
    monkeypatch.setattr(module, "HISTOGRAM_BYTES", budget)
    with TrainingStore(tmp_path) as store:
        module.collect_training(p, store)
        for _ in range(20):
            masks = {
                zone.group_id: ((rng.randrange(120), rng.randrange(120, 240)),) for zone in p.zones
            }
            expected = {
                item: {"present": {}, "not_present": {}}
                for rid, radar in p.radars.items()
                for item in ((rid, c) for c in radar.channels)
            }
            for zone in p.zones:
                positions = [
                    (item, sorted(zone.members).index(item[0]) * 18 + item[1])
                    for item in expected
                    if item[0] in zone.members
                ]
                module._zone_histograms(
                    module._zone_rows(store, zone, masks[zone.group_id]), positions, expected
                )
            assert module.class_bins(p, store, masks) == expected
            assert store._histogram_bytes <= budget


def test_collecting_training_invalidates_cached_zone_histograms(tmp_path):
    from tuner_under_test.engine.hybrid import search as module

    first = problem({"a": [(70,), (0,)]})
    second = problem({"a": [(30,), (0,)]})
    with TrainingStore(tmp_path) as store:
        module.collect_training(first, store)
        before = module.class_bins(first, store)
        module.collect_training(second, store)
        after = module.class_bins(second, store)
    assert before[("a", 0)]["present"]["human"][70] == 6
    assert after[("a", 0)]["present"]["human"][70] == 0
    assert after[("a", 0)]["present"]["human"][30] == 6


def test_running_family_only_reads_signatures_of_changed_members(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search as module

    p = problem({rid: [(70, 60), (0, 0)] for rid in ("a", "b", "c")})
    original = module._Thresholds.signature
    original_set = module._Thresholds.__setitem__
    original_family = module._Search._family
    calls = []
    changes = []
    active = False

    def counted(values):
        if active:
            calls.append(1)
        return original(values)

    def family(engine, pattern, rid, channel):
        nonlocal active
        active = True
        try:
            return original_family(engine, pattern, rid, channel)
        finally:
            active = False

    def changed(values, channel, value):
        if values.get(channel) != value:
            changes.append(1)
        original_set(values, channel, value)

    monkeypatch.setattr(module._Thresholds, "signature", counted)
    monkeypatch.setattr(module._Thresholds, "__setitem__", changed)
    monkeypatch.setattr(module._Search, "_family", family)
    with TrainingStore(tmp_path) as store:
        module.collect_training(p, store)
        engine = module._Search(p, store)
        engine.run(engine.start, 1)
        assert len(calls) <= len(p.radars) + len(changes)


@pytest.mark.parametrize("attenuate", [False, True])
@pytest.mark.parametrize("timing", [{}, {"timeout": 6}])
def test_running_family_matches_plain_patterns_across_member_updates(tmp_path, attenuate, timing):
    from tuner_under_test.engine.hybrid import search as module

    p = problem({rid: [(70, 60), (40, 30), (0, 0)] for rid in ("a", "b", "c")}, timing=timing)
    rng = random.Random(839)
    with TrainingStore(tmp_path) as store:
        module.collect_training(p, store)
        engine = module._Search(p, store, attenuate)
        pattern = module._Pattern(engine.start, engine)
        for _ in range(40):
            rid, channel = rng.choice(engine.coordinates)
            pattern[rid][channel] = rng.randrange(101)
            plain = {member: dict(settings) for member, settings in pattern.items()}
            for member, selected in engine.coordinates:
                assert engine._family(pattern, member, selected) == engine._family(
                    plain, member, selected
                )
            assert engine.costs(pattern, rid, channel, (0, 40, 90)) == module.coordinate_costs(
                p, store, plain, rid, channel, (0, 40, 90), attenuate=attenuate
            )


def test_untimed_observed_energies_reuse_raw_collection_cache(tmp_path):
    from tuner_under_test.engine.hybrid import search as module

    p = problem(
        {"a": [(70,), (20,), (99,), (98,)]},
        spans=[truth(0, 6), truth(6, 12, "not_present", episode="e")],
    )
    with TrainingStore(tmp_path) as store:
        module.collect_training(p, store)
        queries = []
        store._database.set_trace_callback(queries.append)
        try:
            engine = module._Search(p, store)
        finally:
            store._database.set_trace_callback(None)
        assert not any("FROM search_vectors" in query for query in queries)
        assert engine.energies["a", 0] == (20, 70, 98, 99)


@pytest.mark.parametrize("budget", [1, 8 * 1024**2])
def test_observed_energy_cache_preserves_clipping_partial_and_missing_cells(
    tmp_path, monkeypatch, budget
):
    from tuner_under_test.engine.hybrid import search as module
    from tuner_under_test.engine.hybrid import search_data

    p = problem({"a": [(1,), (10,), (20,), (30,), (40,), (99,)]})
    # A fully missing cell and a partial cell contribute no joined vector.
    p.days.rows["a"][2] = (12, 18, [255] * 18)
    p.days.rows["a"][3] = (19, 23, [30] + [255] * 17)
    p = replace(p, start=6, end=30)
    monkeypatch.setattr(search_data, "RAW_ENERGY_BYTES", budget, raising=False)
    with TrainingStore(tmp_path) as store:
        module.collect_training(p, store)
        cached = module._Search(p, store).energies
        assert cached["a", 0] == (10, 40)
        assert store._raw_energy_bytes <= budget
        del store._raw_energies
        assert module._Search(p, store).energies == cached


def test_multi_member_observed_energies_keep_joint_coverage_semantics(tmp_path):
    from tuner_under_test.engine.hybrid import search as module

    p = problem({"a": [(70,), (20,)], "b": [(255,), (0,)]})
    with TrainingStore(tmp_path) as store:
        module.collect_training(p, store)
        engine = module._Search(p, store)
        assert engine.energies == {("a", 0): (20,), ("b", 0): (0,)}


def test_multi_member_only_collection_does_not_compute_raw_energy_cache(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search_data

    p = problem({"a": [(70,), (20,)], "b": [(60,), (0,)]})

    def forbidden(*args):
        raise AssertionError("multi-member-only collection computed unused raw energies")

    monkeypatch.setattr(search_data, "_observe_day", forbidden)
    with TrainingStore(tmp_path) as store:
        search_data.collect_training(p, store)
        assert store._raw_energies == {}


def test_full_coverage_raw_energy_collection_does_not_filter_columns(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search_data

    p = problem({"a": [(70,), (20,)]})

    def forbidden(*args):
        raise AssertionError("full coverage filtered every column's individual bytes")

    monkeypatch.setattr(search_data, "compress", forbidden)
    with TrainingStore(tmp_path) as store:
        search_data.collect_training(p, store)
        assert store._raw_energies["a"][0][0] == bytes([20, 70])


def test_timed_collection_does_not_compute_unused_raw_energy_cache(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid import search_data

    p = problem({"a": [(70,), (20,)]}, timing={"timeout": 6})

    def forbidden(*args):
        raise AssertionError("timed search computed unused untimed raw energies")

    monkeypatch.setattr(search_data, "_observe_day", forbidden)
    with TrainingStore(tmp_path) as store:
        search_data.collect_training(p, store)
        assert store._raw_energies == {}


def test_overlapping_raw_days_fall_back_to_latest_saved_vectors(tmp_path):
    from test_hybrid_search import Days
    from tuner_under_test.engine.hybrid import search as module

    p = problem({"a": [(70,), (20,)]})

    class OverlappingDays(Days):
        def dates(self, rid):
            return iter((0, 1))

        def digest(self, rid, date, channels):
            digest = super().digest(rid, date, channels)
            if date:
                digest = replace(digest, channels=(bytes([30, 40]), *digest.channels[1:]))
            return digest

    p = replace(p, days=OverlappingDays(p.days.rows))
    with TrainingStore(tmp_path) as store:
        module.collect_training(p, store)
        assert "a" not in store._raw_energies
        assert module._Search(p, store).energies["a", 0] == (30, 40)


@pytest.mark.parametrize("attenuate", [False, True])
@pytest.mark.parametrize("size", [31, 5001])
def test_direct_conditional_histograms_match_scalar_masks_exactly(tmp_path, attenuate, size):
    from tuner_under_test.engine.hybrid.search_masks import GroupBlock

    class Model:
        def supports(self, key, value):
            return value >= 25 and value % 4 != 0

    p = problem({"a": [(70, 60), (0, 0)], "b": [(60, 50), (0, 0)]})
    p = replace(
        p,
        policy=replace(
            p.policy, automatic_weight=0.35, quality={"models": {"a": Model(), "b": Model()}}
        ),
    )
    rng = random.Random(983)
    rows = []
    for i in range(size):
        vector = bytes(
            value
            for _member in range(2)
            for value in (
                *[rng.choice((0, 20, 40, 60, 80, 100, 255)) for _ in range(2)],
                *([255] * 16),
            )
        )
        rows.append(
            (
                "present" if i % 2 else "not_present",
                "human" if i % 3 else "entity",
                str(i % 11),
                rng.choice((0.6, 0.7, 0.9, 1.0)),
                vector,
                rng.randrange(1, 65),
            )
        )
    rows.sort(key=lambda row: row[:3])
    with TrainingStore(tmp_path) as store:
        block = GroupBlock(p, store, p.zones[0], rows, attenuate)
        pattern = {"a": {0: 80, 1: 90}, "b": {0: 90, 1: 80}}
        for member, channel in (("a", 0), ("b", 1)):
            position = block.members.index(member) * 18 + channel
            base = block.base(pattern, member, channel)
            expected = {}
            for threshold in range(101):
                scalar = int(
                    "".join(
                        "1" if value > threshold else "0"
                        for value in reversed(block.columns[position])
                    ),
                    2,
                )
                assert block.mask(position, threshold) == scalar
                expected[threshold] = block.time_cost(base | scalar, store, "z", p.policy)
            actual = block.conditional_times(
                base, position, tuple(range(101)), store, "z", p.policy
            )
            assert actual == expected


@pytest.mark.parametrize("attenuate", [False, True])
def test_untimed_search_and_scores_ignore_energies_outside_pure_labels(tmp_path, attenuate):
    from tuner_under_test.engine.hybrid import search as module

    results = []
    for noise in ((0, 0), (99, 100)):
        p = problem(
            {"a": [(70, 60), (20, 30), noise, noise]},
            spans=[truth(0, 6), truth(6, 12, "not_present", episode="e")],
        )
        with TrainingStore(tmp_path) as store:
            module.collect_training(p, store)
            engine, finalists = module.run_search(p, store, attenuate=attenuate)
            thresholds = tuple(range(101))
            for channel in (0, 1):
                expected = module.coordinate_costs(
                    p,
                    store,
                    engine.start,
                    "a",
                    channel,
                    thresholds,
                    folded=False,
                    attenuate=attenuate,
                )
                assert engine.costs(engine.start, "a", channel, thresholds) == expected
            results.append(finalists)
    assert results[0] == results[1]


def test_masked_histogram_queries_seek_to_the_suspect_run(tmp_path):
    from tuner_under_test.engine.hybrid import search as module

    p = problem({"a": [(i % 101,) for i in range(2400)]})
    with TrainingStore(tmp_path) as store:
        module.collect_training(p, store)
        module.class_bins(p, store)
        expected = {("a", 0): {"present": {}, "not_present": {}}}
        module._zone_histograms(
            module._zone_rows(store, p.zones[0], ((14000, 14006),)),
            [(("a", 0), 0)],
            expected,
        )
        steps = []
        store._database.set_progress_handler(lambda: steps.append(1) and 0, 1)
        try:
            actual = module.class_bins(p, store, {"z": ((14000, 14006),)})
        finally:
            store._database.set_progress_handler(None, 0)
        assert actual == expected
        assert len(steps) < 1000


@pytest.mark.parametrize("confidence", (1.0, 0.73))
def test_single_stratum_episode_statistics_avoid_general_reductions(
    tmp_path, monkeypatch, confidence
):
    from builtins import sum as total

    from tuner_under_test.engine.hybrid import search_data, search_masks

    p = problem({"a": [(70,), (0,)]})
    rows = [("present", "human", "p", confidence, bytes([70] + [255] * 17), 2)]
    with TrainingStore(tmp_path) as store:
        search_data.collect_training(p, store)
        packed = search_masks.GroupBlock(p, store, p.zones[0], rows, False)
        reductions = []

        def counted(values):
            reductions.append(1)
            return total(values)

        monkeypatch.setattr(search_masks, "sum", counted, raising=False)
        for mask in (0, 1):
            value = packed.statistics(mask, True)["present/human"]
            assert value == [
                12 * confidence,
                0.0 if mask else 12 * confidence,
                confidence,
                0.0 if mask else confidence,
            ]
        assert not reductions


def test_time_lane_weights_preserve_head_multiplication_and_insertion_order():
    from tuner_under_test.engine.hybrid.search_masks import GroupBlock

    block = GroupBlock.__new__(GroupBlock)
    block.episodes = (
        ("present", "human", 42, ((7, 0.7, 1),)),
        ("not_present", "human", 18, ((3, 0.2, 2),)),
        ("present", "human", 12, ((2, 0.7, 4),)),
    )
    coefficients = {("present/human", 1): 100 / 13, ("not_present/human", 1): 100 / 17}
    expected = (
        (-(100 / 13) * 7 * 0.7 * 6, (100 / 17) * 3 * 0.2 * 6, -(100 / 13) * 2 * 0.7 * 6),
        (1, 2, 4),
    )
    assert block._time_lanes(coefficients) == expected
