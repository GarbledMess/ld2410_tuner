"""Synthetic evidence provenance, exact overlap, and bounded shard persistence."""

from dataclasses import replace

import pytest
import test_tuner  # noqa: F401
from test_hybrid_digest import block
from tuner_under_test.engine.hybrid.digest import fold_day
from tuner_under_test.engine.hybrid.evidence import overlay, zone_segments
from tuner_under_test.engine.hybrid.training import TrainingStore
from tuner_under_test.engine.hybrid.types import TruthSpan


def truth(lo, hi, state="present", source="human", confidence=1, episode="parent", dependencies=()):
    return TruthSpan(lo, hi, state, source, confidence, episode, dependencies)


def digest(rows=None):
    if rows is None:
        rows = [(0, 12, [40] * 18)]
    return fold_day([block(rows)], 0, 12, (0,))


def test_unknown_blocks_automatic_fallback():
    spans = [truth(0, 12, source="entity"), truth(2, 4, state="unknown")]
    assert [(lo, hi) for _, lo, hi, _, _ in overlay(digest(), spans)] == [(0, 2), (4, 6), (6, 12)]


def test_tiny_mixed_cell_labels_still_make_class_scorable(tmp_path):
    spans = [truth(0, 0.1), truth(0.1, 6, "not_present", "entity", 0.6)]
    pieces = list(overlay(digest(), spans))
    assert not any(pure for *_, pure in pieces)
    with TrainingStore(tmp_path) as store:
        for _cell, lo, hi, span, _pure in pieces:
            store.observe("zone", "radar", lo, hi, span)
        totals = store.statistics("zone", "radar")
    assert totals["present"]["seconds"] == pytest.approx(0.1)
    assert totals["not_present"]["seconds"] == pytest.approx(5.9)
    assert totals["not_present"]["weighted_seconds"] == pytest.approx(3.54)


def test_confidence_changes_do_not_split_parent_episode(tmp_path):
    spans = [
        truth(0, 6, source="entity", confidence=0.6),
        truth(6, 12, source="entity", confidence=0.9),
    ]
    with TrainingStore(tmp_path, budget=0) as store:
        for _, lo, hi, span, _ in overlay(digest(), spans):
            store.observe("zone", "radar", lo, hi, span)
        stats = store.statistics("zone", "radar")["present"]
        assert stats["episodes"] == 1
        assert stats["episode_mass"] == pytest.approx(0.75)


def test_gap_splits_coverage_but_not_parent_annotation(tmp_path):
    d = digest([(0, 2, [40] * 18), (4, 12, [40] * 18)])
    pieces = list(overlay(d, [truth(0, 12)]))
    assert [(a, b) for _, a, b, _, _ in pieces] == [(0, 2), (4, 6), (6, 12)]
    assert {s.episode_id for _, _, _, s, _ in pieces} == {"parent"}
    with TrainingStore(tmp_path) as store:
        for _, lo, hi, span, _ in pieces:
            store.observe("zone", "radar", lo, hi, span)
        assert store.statistics("zone", "radar")["present"]["episodes"] == 1


def test_shared_zone_coverage_is_local():
    digests = {"a": digest(), "b": digest([(3, 9, [40] * 18)])}
    assert list(zone_segments(digests, ("a",))) == [(0, 12)]
    assert list(zone_segments(digests, ("a", "b"))) == [(3, 9)]


def test_spilled_training_equals_memory_training(tmp_path):
    shards = []
    for budget in (0, 1000000):
        with TrainingStore(tmp_path, budget=budget) as store:
            for i in range(3):
                data = b"".join(bytes([c + i]) * 4096 for c in range(18))
                store.add("zone", "radar", "present", data, b"lineage")
            shards.append(list(store.shards("zone", "radar")))
            assert [meta for _, _, meta in store.records("zone", "radar")] == [b"lineage"] * 3
            assert all(len(ch) <= 4096 for shard in shards[-1] for ch in shard)
    assert shards[0] == shards[1]
    assert list(tmp_path.iterdir()) == []


def test_positive_support_does_not_hide_false_activity():
    span = truth(0, 12, "not_present", "entity", dependencies=("other", "other"))
    pieces = list(overlay(digest(), [span]))
    assert sum(b - a for _, a, b, _, _ in pieces) == 12
    assert all(s.dependencies == ("other",) for _, _, _, s, _ in pieces)
    assert [(a, b) for _, a, b, _, _ in pieces] == [
        (a, b) for _, a, b, _, _ in overlay(digest(), [replace(span, dependencies=())])
    ]


def test_human_overrides_automatic_only_on_its_overlap():
    spans = [truth(0, 12, "not_present", "entity"), truth(2, 4)]
    pieces = list(overlay(digest(), spans))
    assert sum(b - a for _, a, b, s, _ in pieces if s.source == "human") == 2
    assert sum(b - a for _, a, b, s, _ in pieces if s.source == "entity") == 10


def test_training_files_close_on_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with TrainingStore(tmp_path, budget=0) as store:
            store.add("zone", "radar", "present", bytes(18), b"meta")
            raise RuntimeError("cancelled")
    assert list(tmp_path.iterdir()) == []


def test_margin_histograms_use_human_duration_without_confidence(tmp_path):
    with TrainingStore(tmp_path, budget=0) as store:
        store.observe("zone", "radar", 0, 0.1, truth(0, 0.1, confidence=0.2), bytes([40] * 18))
        store.observe("zone", "radar", 1, 3, truth(1, 3, source="entity"), bytes([90] * 18))
        hist = store.histograms("zone", "radar", 0)
        assert hist["present"][40] == pytest.approx(0.1)
        assert hist["present"][90] == 0
        assert sum(hist["not_present"]) == 0


def test_source_class_statistics_remain_separate(tmp_path):
    with TrainingStore(tmp_path) as store:
        store.observe("zone", "radar", 0, 2, truth(0, 2))
        store.observe("zone", "radar", 2, 6, truth(2, 6, source="entity", confidence=0.6))
        stats = store.statistics("zone", "radar")["present"]["sources"]
        assert stats["human"]["seconds"] == 2
        assert stats["human"]["episode_mass"] == 1
        assert stats["entity"]["seconds"] == 4
        assert stats["entity"]["episode_mass"] == pytest.approx(0.6)


def test_shard_iteration_checks_cancellation(tmp_path, monkeypatch):
    from tuner_under_test.history import pace

    def cancelled():
        raise pace.Stopped()

    with TrainingStore(tmp_path) as store:
        store.add("zone", "radar", "present", bytes(18), b"lineage")
        monkeypatch.setattr(pace, "rest", cancelled)
        with pytest.raises(pace.Stopped):
            list(store.shards("zone", "radar"))


@pytest.mark.parametrize("data", [b"x", bytes(18 * 4097)], ids=["misaligned", "oversized"])
def test_invalid_shard_shapes_rejected(tmp_path, data):
    with TrainingStore(tmp_path) as store, pytest.raises(ValueError):
        store.add("zone", "radar", "present", data, b"")
