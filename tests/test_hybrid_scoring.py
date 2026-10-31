"""Synthetic streaming cost parity against the independent physical referee."""

import random
from dataclasses import replace

import pytest
import test_tuner  # noqa: F401
from hybrid_referee import Replay, intersect, referee_score, union
from test_hybrid_evidence import truth
from tuner_under_test.engine.hybrid.scoring import SliceScorer, cost_from_statistics
from tuner_under_test.engine.hybrid.types import EvidencePolicy, ZoneSpec


def evaluate(
    tmp_path, spans, active, cuts=(0, 20), policy=None, coverage=None, supported=None, raw=None
):
    policy = policy or EvidencePolicy(0, False, {})
    coverage = coverage or {k: [(cuts[0], cuts[-1])] for k in active}
    with SliceScorer(
        ZoneSpec("zone", tuple(active), tuple(spans)), policy, (cuts[0], cuts[-1]), tmp_path
    ) as scorer:
        for lo, hi in zip(cuts, cuts[1:], strict=False):

            def clip(values, lo=lo, hi=hi):
                return {k: intersect(v, [(lo, hi)]) for k, v in values.items()}

            scorer.consume(
                lo,
                hi,
                clip(coverage),
                clip(raw or active),
                clip(active),
                clip(active if supported is None else supported),
            )
        result = scorer.result()
        assert scorer.result() == result
    assert list(tmp_path.iterdir()) == []
    return result


def oracle(spans, active, coverage, window, weight=0):
    return referee_score(
        {},
        {},
        [(s.start, s.end, s.state, s.source, s.confidence) for s in spans],
        window,
        weights={"automatic": weight, "adaptive": False},
        include_margins=False,
        replayed={k: Replay(tuple(coverage[k]), tuple(union(v))) for k, v in active.items()},
    )


@pytest.mark.parametrize("end,cost,failed", [(20, 200, 1), (15, 50, 0)])
def test_mirror_rule_and_exact_half_boundary(tmp_path, end, cost, failed):
    spans = [truth(0, 10), truth(10, 20, "not_present", episode="empty")]
    result = evaluate(tmp_path, spans, {"a": [(0, end)]})
    assert result["cost"] == cost
    assert result["empty_periods_failed"] == failed
    assert result["empty_periods_with_false_trigger"] == 0
    half = evaluate(tmp_path, spans, {"a": [(0, 5)]})
    assert half["mostly_missed_episodes"] == 0
    assert half["completely_missed_episodes"] == 0
    assert evaluate(tmp_path, spans, {"a": []})["cost"] == 1000


def test_trigger_not_recounted_at_day_chunk_label_or_drain_edge(tmp_path):
    spans = [
        truth(0, 10),
        truth(10, 15, "not_present", episode="empty"),
        truth(15, 20, "not_present", confidence=0.8, episode="empty"),
    ]
    result = evaluate(tmp_path, spans, {"a": [(0, 20)]}, cuts=(0, 5, 10, 15, 20))
    assert result["false_triggers"] == 0
    assert result["empty_periods_with_false_trigger"] == 0
    assert result["empty_periods_mostly_false_active"] == 1
    assert result["empty_periods"] == 1


def test_weighted_time_and_episode_caps_match_referee(tmp_path):
    spans = [
        truth(0, 5),
        truth(5, 10, source="automatic", confidence=0.6, episode="auto"),
        truth(10, 15, "not_present", episode="empty"),
        truth(15, 20, "not_present", "automatic", 0.9, "auto-empty"),
    ]
    active = {"a": [(0, 5), (15, 20)]}
    coverage = {"a": [(0, 20)]}
    result = evaluate(tmp_path, spans, active, policy=EvidencePolicy(1, False, {}))
    expected = oracle(spans, active, coverage, (0, 20), 1)
    for key in ("cost", "time_term", "episode_term"):
        assert result[key] == pytest.approx(expected[key], abs=1e-6)
    assert result["sufficient_statistics"] == expected["sufficient_statistics"]
    statistics = result["sufficient_statistics"]
    policy = EvidencePolicy(
        1, False, {"human_episodes": {"present": 10}, "automatic_episode_mass": {"present": 0.2}}
    )
    capped = cost_from_statistics(statistics, policy)
    assert capped["class_components"]["present"]["episode_fraction"] == pytest.approx(
        0.25 * 0.2 / 10.2
    )


def test_reference_and_automatic_only_class_fallback(tmp_path):
    spans = [
        truth(0, 5),
        truth(5, 10, source="automatic", episode="auto"),
        truth(10, 20, "not_present", "automatic", 0.7, "empty"),
    ]
    result = evaluate(tmp_path, spans, {"a": [(0, 5)]}, policy=EvidencePolicy(1, True, {}))
    assert result["cost"] == 0
    auto = [replace(s, source="automatic") for s in spans]
    result = evaluate(tmp_path, auto, {"a": []}, policy=EvidencePolicy(0, True, {}))
    assert result["cost"] == 1000


def test_confidence_weighted_episode_mass_uses_original_parent(tmp_path):
    spans = [
        truth(0, 4, source="automatic", confidence=0.6),
        truth(4, 10, source="automatic", confidence=0.9),
        truth(10, 20, "not_present", episode="empty"),
    ]
    result = evaluate(
        tmp_path, spans, {"a": []}, cuts=(0, 3, 7, 20), coverage={"a": [(0, 2), (6, 20)]}
    )
    assert result["occupied_episodes"] == 1
    assert result["sufficient_statistics"]["present/automatic"] == pytest.approx(
        [4.8, 4.8, 0.8, 0.8]
    )
    assert result["completely_missed_episodes"] == 1
    assert result["uncovered_label_seconds"] == 4


def test_sliced_cost_equals_whole_referee(tmp_path):
    rng = random.Random(208)
    for _ in range(15):
        spans = [
            truth(
                i * 10,
                (i + 1) * 10,
                "present" if i % 2 == 0 else "not_present",
                "human" if i % 3 else "automatic",
                rng.uniform(0.55, 1),
                str(i),
            )
            for i in range(6)
        ]
        active = {
            k: union((i, i + rng.uniform(0.01, 4)) for i in range(0, 60, 4) if rng.random() < 0.6)
            for k in ("a", "b")
        }
        coverage = {"a": [(0, 60)], "b": [(0, 17), (19, 60)]}
        expected = oracle(spans, active, coverage, (0, 60), 0.7)
        result = evaluate(
            tmp_path,
            spans,
            active,
            cuts=(0, 9, 17, 23, 40, 60),
            policy=EvidencePolicy(0.7, False, {}),
            coverage=coverage,
        )
        for key, value in expected.items():
            if isinstance(value, (float, int)):
                assert result[key] == pytest.approx(value, abs=1e-6), key
        for key, values in expected["sufficient_statistics"].items():
            assert result["sufficient_statistics"][key] == pytest.approx(values, abs=1e-6)


def test_longest_miss_crosses_chunk_edge(tmp_path):
    spans = [truth(0, 15), truth(15, 20, "not_present", episode="empty")]
    result = evaluate(tmp_path, spans, {"a": [(0, 2), (13, 15)]}, cuts=(0, 5, 10, 20))
    assert result["longest_miss_seconds"] == 11
    assert result["mostly_missed_episodes"] == 1
    assert result["completely_missed_episodes"] == 0


def test_supported_positive_is_separate_from_physical_false_activity_and_raw(tmp_path):
    spans = [
        truth(0, 10, source="automatic"),
        truth(10, 20, "not_present", "automatic", episode="empty"),
    ]
    result = evaluate(tmp_path, spans, {"a": [(0, 20)]}, supported={"a": []}, raw={"a": [(0, 2)]})
    assert result["cost"] == 1200
    assert result["false_active_seconds"] == 10
    assert result["raw"]["occupied_recall"] == 0.2
    assert result["exclusive_seconds"] == {"a": 10}


def test_missing_class_and_tiny_class(tmp_path):
    result = evaluate(tmp_path, [truth(0, 20)], {"a": []})
    assert result["cost"] is None
    assert not result["scorable_both_classes"]
    result = evaluate(
        tmp_path, [truth(0, 0.1), truth(0.1, 20, "not_present", episode="e")], {"a": []}
    )
    assert result["cost"] == 1000


def test_warmup_and_gap_do_not_fabricate_triggers(tmp_path):
    with SliceScorer(
        ZoneSpec("z", ("a",), (truth(0, 20, "not_present"),)),
        EvidencePolicy(0, False, {}),
        (0, 20),
        tmp_path,
    ) as scorer:
        scorer.consume(-5, 0, {"a": [(-5, 0)]}, {}, {"a": [(-5, 0)]}, {})
        scorer.consume(0, 10, {"a": [(0, 10)]}, {}, {"a": [(0, 10)]}, {})
        scorer.consume(12, 20, {"a": [(12, 20)]}, {}, {"a": [(12, 20)]}, {})
        result = scorer.result()
        assert result["false_triggers"] == 1
        assert result["uncovered_label_seconds"] == 2


def test_temporary_episode_store_closes_on_exception(tmp_path):
    with (
        pytest.raises(RuntimeError),
        SliceScorer(ZoneSpec("z", ("a",), ()), EvidencePolicy(0, False, {}), (0, 20), tmp_path),
    ):
        raise RuntimeError("cancelled")
    assert list(tmp_path.iterdir()) == []


def test_quality_independent_support_caps_both_time_and_episode_terms():
    statistics = {
        "present/human": [10, 0, 1, 0],
        "present/automatic": [1000, 1000, 1, 1],
        "not_present/human": [10, 0, 1, 0],
    }
    policy = EvidencePolicy(
        1, False, {"human_episodes": {"present": 1}, "automatic_episode_mass": {"present": 0.1}}
    )
    result = cost_from_statistics(statistics, policy)
    expected = 0.25 * 0.1 / 1.1
    assert result["class_components"]["present"] == pytest.approx(
        {"time_fraction": expected, "episode_fraction": expected}
    )


def test_exclusive_attribution_walks_each_member_activity_only_once_per_slice(tmp_path):
    class Activity(list):
        walks = 0

        def __iter__(self):
            self.walks += 1
            return super().__iter__()

    members = {str(i): Activity([(i, i + 2)]) for i in range(30)}
    coverage = {k: [(0, 40)] for k in members}
    with SliceScorer(
        ZoneSpec("z", tuple(members), (truth(0, 40),)),
        EvidencePolicy(0, False, {}),
        (0, 40),
        tmp_path,
    ) as scorer:
        scorer.consume(0, 40, coverage, {}, members, {})
        result = scorer.result()
    assert result["exclusive_seconds"]["0"] == 1
    assert result["exclusive_seconds"]["29"] == 1
    assert sum(result["exclusive_seconds"].values()) == 2
    assert max(v.walks for v in members.values()) <= 3


def test_slice_order_and_closed_scorer_are_rejected(tmp_path):
    with SliceScorer(
        ZoneSpec("z", ("a",), (truth(0, 20),)), EvidencePolicy(0, False, {}), (0, 20), tmp_path
    ) as scorer:
        scorer.consume(0, 0, {}, {}, {}, {})
        scorer.consume(0, 10, {}, {}, {}, {})
        for lo, hi in ((9, 20), (12, 11)):
            with pytest.raises(ValueError, match="chronological"):
                scorer.consume(lo, hi, {}, {}, {}, {})
        scorer.result()
        with pytest.raises(ValueError, match="closed"):
            scorer.consume(10, 20, {}, {}, {}, {})
    assert list(tmp_path.iterdir()) == []


def test_unconsumed_labels_are_uncovered_and_overlaps_keep_unknown_exclusion(tmp_path):
    spans = [truth(0, 20, source="automatic"), truth(2, 4), truth(6, 8, "unknown")]
    with SliceScorer(
        ZoneSpec("z", ("a",), tuple(spans)), EvidencePolicy(0, False, {}), (0, 20), tmp_path
    ) as scorer:
        scorer.consume(0, 5, {"a": [(0, 5)]}, {}, {}, {})
        result = scorer.result()
    assert result["occupied_seconds"] == 5
    assert result["uncovered_label_seconds"] == 13
    assert result["sufficient_statistics"]["present/human"] == [2, 2, 1, 1]
    assert result["sufficient_statistics"]["present/automatic"] == [3, 3, 1, 1]
