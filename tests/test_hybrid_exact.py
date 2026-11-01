"""Exact shortlist evaluation: referee parity, lossless folding and common fidelity."""

import random
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from hybrid_referee import referee_score
from test_hybrid_digest import block
from test_hybrid_evidence import truth
from test_hybrid_search import Days, problem
from tuner_under_test.engine.hybrid.scoring import SliceScorer
from tuner_under_test.engine.hybrid.timing import TimingStream
from tuner_under_test.engine.hybrid.types import EvidencePolicy, FitProblem, RadarSpec, ZoneSpec

KEYS = (
    "cost",
    "partial_cost",
    "time_term",
    "episode_term",
    "occupied_seconds",
    "empty_seconds",
    "occupied_recall",
    "false_active_seconds",
    "occupied_episodes",
    "empty_periods",
    "mostly_missed_episodes",
    "completely_missed_episodes",
    "empty_periods_with_false_trigger",
    "empty_periods_mostly_false_active",
    "empty_periods_failed",
    "false_triggers",
    "longest_miss_seconds",
    "uncovered_label_seconds",
    "covered_seconds",
)


class Rows:
    """A blocks-only row source; records every read window."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def blocks(self, lo, hi):
        self.calls.append((lo, hi))
        selected = [(max(lo, a), min(hi, b), v) for a, b, v in self.rows if a < hi and b > lo]
        for at in range(0, len(selected), 4096):
            yield block(selected[at : at + 4096])

    def rows_in(self, lo, hi):
        return sum(a < hi and b > lo for a, b, _ in self.rows)


class Windowed:
    """Production-style source answering clipped usable row windows."""

    def __init__(self, rows, channels):
        from tuner_under_test.engine.hybrid.physical_index import physical_rows

        self.physical = physical_rows([block(rows)], rows[0][0], rows[-1][1], channels)
        self.calls = []

    def window(self, lo, hi):
        self.calls.append((lo, hi))
        return self.physical.window(lo, hi)

    def rows_in(self, lo, hi):
        return self.physical.rows_in(lo, hi)

    def blocks(self, lo, hi):
        raise AssertionError("windowed sources are never decoded as raw blocks")


def random_rows(rng, start, count, lo=0, hi=95, gaps=True):
    rows, t = [], start
    for _ in range(count):
        if gaps and rng.random() < 0.03:
            t += rng.choice([0.25, 3, 40])
        end = t + rng.choice([0.25, 0.5, 1, 2, 6])
        values = [rng.choice([lo, 9, 30, 41, 60, hi, 255 if rng.random() < 0.02 else 20])]
        values += [rng.randrange(lo, hi) for _ in range(17)]
        rows.append((t, end, values))
        t = end
    return rows


def random_timing(rng):
    return rng.choice(
        [
            {},
            {"timeout": 0, "on_delay": 0, "off_delay": 0},
            {"timeout": rng.choice([0.5, 2.5, 8]), "on_delay": None, "off_delay": None},
            {
                "timeout": rng.choice([0, 2.5, 8]),
                "on_delay": rng.choice([0, 1.5, 7]),
                "off_delay": rng.choice([0, 3, 10.25]),
            },
        ]
    )


def alternating(rng, start, end, sources=("human",), lo=20, hi=200):
    spans, t, state, n = [], start, "present", 0
    while t < end:
        d = rng.uniform(lo, hi)
        spans.append(
            truth(
                t,
                min(end, t + d),
                state,
                rng.choice(sources),
                rng.choice([1, 1, 0.6]),
                episode=f"e{n // 2}{state}",
            )
        )
        n += 1
        state = "not_present" if state == "present" else "present"
        t += d
    return tuple(spans)


def build(rows, channels, timing, zones, policy=None):
    radars = {
        rid: RadarSpec(
            rid,
            channels[rid],
            dict.fromkeys(channels[rid], tuple(range(101))),
            dict.fromkeys(channels[rid], 50),
            timing[rid],
        )
        for rid in rows
    }
    start = min(r[0][0] for r in rows.values())
    end = max(r[-1][1] for r in rows.values())
    return FitProblem(
        radars,
        zones,
        start,
        end,
        None,
        {rid: Rows(r) for rid, r in rows.items()},
        policy or EvidencePolicy(0, False, {}),
    )


def patterns_for(rng, p, count):
    out = []
    for _ in range(count):
        out.append(
            {
                rid: {c: rng.choice([9, 30, 41, 50, 60, 100]) for c in radar.channels}
                for rid, radar in p.radars.items()
            }
        )
    out.append(dict(out[0]))  # an identical pattern shares every replay
    return out


def assert_same(got, expected):
    assert len(got) == len(expected)
    for left, right in zip(got, expected, strict=True):
        assert set(left["zones"]) == set(right["zones"])
        for gid, zone in right["zones"].items():
            for key in KEYS:
                assert left["zones"][gid][key] == pytest.approx(zone[key], abs=1e-7), key


def referee_rows(rows, timing):
    return SimpleNamespace(
        times=[r[0] for r in rows],
        ends=[r[1] for r in rows],
        columns=block(rows).columns,
        timing=timing,
    )


def test_regions_merge_warmup_without_mutating_labels():
    from tuner_under_test.engine.hybrid.exact import regions

    p = problem({"a": [(70,), (0,), (20,), (20,)]})
    assert regions(p.zones[0].truth, warmup=10) == ((-10, 24),)
    assert regions((truth(0, 5), truth(100, 105)), warmup=10) == ((-10, 5), (90, 105))
    assert p.zones[0].truth[0].start == 0


@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize("chunk", [37.0, 21600.0])
def test_exact_scores_equal_referee(seed, chunk, monkeypatch):
    from tuner_under_test.engine.hybrid import replay
    from tuner_under_test.engine.hybrid.exact import exact_scores, regions

    monkeypatch.setattr(replay, "CHUNK_SECONDS", chunk)
    rng = random.Random(f"exact-{seed}")
    rows = {rid: random_rows(rng, 1000.0, 600, gaps=False) for rid in ("a", "b")}
    channels = {"a": tuple(range(18)), "b": (0, 1, 4, 5)}
    timing = {rid: random_timing(rng) for rid in rows}
    end = min(r[-1][1] for r in rows.values())
    spans = alternating(rng, 1000.0, end)
    p = build(rows, channels, timing, (ZoneSpec("room", ("a", "b"), spans),))
    pats = patterns_for(rng, p, 4)
    got = exact_scores(p, pats, regions(spans))
    radars = {rid: referee_rows(rows[rid], timing[rid]) for rid in rows}
    window = (p.start, p.end)
    for pat, score in zip(pats, got, strict=True):
        expected = referee_score(
            radars,
            pat,
            [(s.start, s.end, s.state, s.source, s.confidence) for s in spans],
            window,
            include_margins=False,
        )
        for key in ("cost", "false_triggers", "mostly_missed_episodes", "empty_periods_failed"):
            assert score[key] == pytest.approx(expected[key], abs=1e-6), key
        for key in ("false_active_seconds", "occupied_seconds", "empty_seconds"):
            assert score[key] == pytest.approx(expected[key], abs=1e-6), key
        assert score["scoring_mode"] == "exact"


def test_exact_scores_count_subcell_flicker_triggers():
    from tuner_under_test.engine.hybrid.exact import exact_scores

    p = problem({"a": [(0,), (0,), (0,), (0,)]})
    rows = [(0, 3, [0] + [255] * 17)]
    for t in range(3, 21, 3):
        rows.extend([(t, t + 0.5, [90] + [255] * 17), (t + 0.5, t + 3, [0] + [255] * 17)])
    p = replace(
        p,
        rows={"a": Rows(rows)},
        end=24,
        zones=(
            replace(p.zones[0], truth=(truth(0, 3), truth(3, 24, "not_present", episode="empty"))),
        ),
    )
    got = exact_scores(p, [{"a": {0: 40}}], ((0, 24),))[0]
    assert got["false_triggers"] == 6
    assert got["false_active_seconds"] == 3


def test_exact_scores_accept_a_windowed_row_source():
    from tuner_under_test.engine.hybrid.exact import exact_scores, regions

    rng = random.Random("source")
    rows = random_rows(rng, 5000.0, 800)
    spans = alternating(rng, 5000.0, rows[-1][1])
    timing = {"a": {"timeout": 5, "on_delay": 1.5, "off_delay": 10.25}}
    p = build({"a": rows}, {"a": (0, 3, 7)}, timing, (ZoneSpec("z", ("a",), spans),))
    pats = [{"a": dict.fromkeys((0, 3, 7), t)} for t in (30, 50, 92)]
    windowed = Windowed(rows, (0, 3, 7))
    expected = exact_scores(p, pats, regions(spans))
    got = exact_scores(replace(p, rows={"a": windowed}), pats, regions(spans))
    assert got == expected
    assert windowed.calls
    assert all(hi - lo <= 21600 for lo, hi in windowed.calls)


def test_dense_rows_subdivide_time_chunks(monkeypatch):
    from tuner_under_test.engine.hybrid import replay
    from tuner_under_test.engine.hybrid.exact import exact_scores

    rng = random.Random(9)
    rows = [(i / 1000, (i + 1) / 1000, [rng.choice([0, 90])] + [255] * 17) for i in range(10000)]
    p = problem({"a": [(70,), (0,), (20,), (20,)]})
    spans = (truth(0, 5), truth(5, 10, "not_present", episode="empty"))
    p = replace(p, rows={"a": Rows(rows)}, end=10, zones=(replace(p.zones[0], truth=spans),))
    original = replay.fold_masks
    sizes = []

    def checked(columns, vectors):
        sizes.append(len(next(iter(columns.values()))))
        return original(columns, vectors)

    monkeypatch.setattr(replay, "fold_masks", checked)
    pats = [{"a": {0: 40}}, {"a": {0: 95}}]
    whole = exact_scores(p, pats, ((0, 10),))
    assert max(sizes) <= 4096
    monkeypatch.setattr(replay, "ROW_BLOCK_ROWS", 37)
    monkeypatch.setattr(replay, "CHUNK_SECONDS", 0.7)
    assert_same(exact_scores(p, pats, ((0, 10),)), whole)


def test_decision_masks_keep_every_contested_row_and_fold_constants():
    from tuner_under_test.engine.hybrid.replay import fold_masks

    columns = {0: bytes([0, 10, 11, 90, 255]), 1: bytes([0, 0, 0, 0, 0])}
    masks, info = fold_masks(columns, [{0: t, 1: 100} for t in (10, 11, 80)])
    assert masks == [
        int.from_bytes(bytes([0, 0, 1, 1, 0]), "little"),
        int.from_bytes(bytes([0, 0, 0, 1, 0]), "little"),
        int.from_bytes(bytes([0, 0, 0, 1, 0]), "little"),
    ]
    assert info == {"rows": 5, "contested": 1, "always": 1}


def test_premerge_only_joins_within_hold_and_segment():
    from tuner_under_test.engine.hybrid.replay import premerge

    hits = [(0.0, 1.0), (2.0, 3.0), (9.0, 10.0)]
    assert premerge(hits, [(0.0, 10.0)], 1.5) == [(0.0, 3.0), (9.0, 10.0)]
    assert premerge(hits, [(0.0, 1.5), (1.8, 10.0)], 1.5) == hits
    assert premerge(hits, [(0.0, 10.0)], 0.0) == hits
    assert premerge(hits, [(0.0, 10.0)], 6.0) == [(0.0, 10.0)]


class Support:
    def __init__(self):
        self.calls = 0

    def supports(self, key, value):
        self.calls += 1
        return value % 3 != 0


def unfolded(p, pattern, models):
    """Independent per-pattern whole-window replay: no folding, sharing or chunks."""
    from tuner_under_test.calibration.event_replay import _timing

    signals = {name: {} for name in ("coverage", "raw", "active", "supported")}
    for rid, radar in p.radars.items():
        physical, support = TimingStream(radar.timing), TimingStream(radar.timing)
        coverage, raw = [], []
        model = models.get(rid)
        for a, b, values in p.rows[rid].rows:
            if b <= a or any(values[c] == 255 for c in radar.channels):
                continue
            hit = any(values[c] > pattern[rid][c] for c in radar.channels)
            ok = hit and (
                model is None
                or any(
                    values[c] > pattern[rid][c] and model.supports(None, values[c])
                    for c in radar.channels
                )
            )
            physical.feed([(a, b)], [(a, b)] if hit else [])
            support.feed([(a, b)], [(a, b)] if ok else [])
            coverage.append((a, b))
            if hit:
                raw.append((a, b))
        physical.finish()
        support.finish()
        assert _timing(radar.timing) is not None
        _, active = physical.drain(physical.frontier())
        _, supported = support.drain(support.frontier())
        for name, value in zip(signals, (coverage, raw, active, supported), strict=True):
            signals[name][rid] = value
    result = {}
    for zone in p.zones:
        with SliceScorer(zone, p.policy, (p.start, p.end), p.directory) as scorer:
            scorer.consume(p.start - 1e6, p.end + 1e6, *signals.values())
            result[zone.group_id] = scorer.result()
    return result


@pytest.mark.parametrize("seed", range(6))
def test_folded_sparse_replay_equals_unfolded_evaluator(seed, monkeypatch, tmp_path):
    from tuner_under_test.engine.hybrid import replay
    from tuner_under_test.engine.hybrid.exact import exact_scores, regions

    rng = random.Random(f"fold-{seed}")
    monkeypatch.setattr(replay, "CHUNK_SECONDS", rng.choice([23.0, 300.0, 21600.0]))
    monkeypatch.setattr(replay, "ROW_BLOCK_ROWS", rng.choice([7, 64, 4096]))
    rows = {rid: random_rows(rng, 2000.0, 500) for rid in ("a", "b", "c")}
    channels = {"a": (0, 1, 2), "b": (0, 5), "c": (3,)}
    timing = {rid: random_timing(rng) for rid in rows}
    end = max(r[-1][1] for r in rows.values())
    zones = (
        ZoneSpec("room", ("a", "b"), alternating(rng, 1990.0, end, ("human", "entity"))),
        ZoneSpec("solo", ("c",), alternating(rng, 1995.0, end, ("human",), 30, 90)),
    )
    models = {"a": Support()} if seed % 2 else {}
    policy = EvidencePolicy(0.5, False, {"models": models} if models else {})
    p = build(rows, channels, timing, zones, policy)
    pats = patterns_for(rng, p, 7)
    spans = [s for z in zones for s in z.truth]
    got = exact_scores(p, pats, regions(spans))
    oracle = SimpleNamespace(**{k: getattr(p, k) for k in p.__slots__}, directory=tmp_path)
    for pat, score in zip(pats, got, strict=True):
        expected = unfolded(oracle, pat, models)
        for gid, zone_score in expected.items():
            for key in KEYS:
                assert score["zones"][gid][key] == pytest.approx(zone_score[key], abs=1e-7), key
            statistics = score["zones"][gid]["sufficient_statistics"]
            assert set(statistics) == set(zone_score["sufficient_statistics"])
            for key, values in zone_score["sufficient_statistics"].items():
                assert statistics[key] == pytest.approx(values), key
        assert score["cost"] == pytest.approx(
            sum(z["cost"] for z in expected.values())
            if all(z["cost"] is not None for z in expected.values())
            else None
        )


def test_identical_member_vectors_are_replayed_once(monkeypatch):
    from tuner_under_test.engine.hybrid import replay
    from tuner_under_test.engine.hybrid.exact import exact_scores

    rng = random.Random(5)
    rows = {rid: random_rows(rng, 0.0, 300, gaps=False) for rid in ("a", "b")}
    spans = alternating(rng, 0.0, 250.0)
    p = build(rows, {"a": (0,), "b": (0,)}, {"a": {}, "b": {}}, (ZoneSpec("z", ("a", "b"), spans),))
    calls = []
    original = replay.fold_masks

    def counted(columns, vectors):
        calls.append(len(vectors))
        return original(columns, vectors)

    monkeypatch.setattr(replay, "fold_masks", counted)
    pats = [{"a": {0: 41}, "b": {0: t}} for t in (9, 30, 60)]
    exact_scores(p, pats, ((-1800.0, 250.0),))
    # Radar a shares one vector for all three patterns; radar b needs three.
    assert sorted(set(calls)) == [1, 3]


def test_replay_records_rescore_new_truth_without_reading_rows(tmp_path):
    from tuner_under_test.engine.hybrid.exact import Evaluator, exact_scores, regions

    rng = random.Random(11)
    rows = {"a": random_rows(rng, 0.0, 700)}
    spans = alternating(rng, 0.0, rows["a"][-1][1])
    timing = {"a": {"timeout": 2.5, "on_delay": 1.5, "off_delay": 3}}
    p = build(rows, {"a": (0, 1)}, timing, (ZoneSpec("z", ("a",), spans),))
    pats = [{"a": {0: 30, 1: 60}}, {"a": {0: 60, 1: 41}}]
    shifted = tuple(replace(s, state="unknown") if i == 3 else s for i, s in enumerate(spans))
    masked = replace(p, zones=(replace(p.zones[0], truth=shifted),))
    with Evaluator(p, regions(spans), directory=tmp_path) as evaluator:
        first = evaluator.scores(pats)
        reads = len(p.rows["a"].calls)
        again = evaluator.scores(pats, zones=masked.zones)
        assert len(p.rows["a"].calls) == reads
        assert evaluator.spilled_bytes > 0
    assert first == exact_scores(p, pats, regions(spans))
    assert again == exact_scores(masked, pats, regions(spans))
    assert list(tmp_path.iterdir()) == []


def test_chunks_settle_when_a_radar_has_no_rows():
    from tuner_under_test.engine.hybrid.exact import exact_scores

    rng = random.Random(3)
    a = random_rows(rng, 0.0, 400, gaps=False)
    b = [r for r in random_rows(rng, 0.0, 400, gaps=False) if not 100 < r[0] < 200]
    spans = alternating(rng, 0.0, 300.0)
    zones = (ZoneSpec("both", ("a", "b"), spans), ZoneSpec("a", ("a",), spans))
    p = build({"a": a, "b": b}, {"a": (0,), "b": (0,)}, {"a": {"timeout": 4}, "b": {}}, zones)
    pats = [{"a": {0: 41}, "b": {0: 30}}]
    small = exact_scores(p, pats, ((-1800.0, 300.0),))
    from tuner_under_test.engine.hybrid import replay

    original = replay.CHUNK_SECONDS
    try:
        replay.CHUNK_SECONDS = 13.0
        assert_same(exact_scores(p, pats, ((-1800.0, 300.0),)), small)
    finally:
        replay.CHUNK_SECONDS = original


def test_support_is_positive_only_and_compiled_once_per_radar():
    from tuner_under_test.engine.hybrid.exact import exact_scores

    class Unsupported:
        def __init__(self):
            self.calls = 0

        def supports(self, key, value):
            self.calls += 1
            return False

    model = Unsupported()
    p = problem(
        {"a": [(70,), (70,), (70,), (70,)]},
        spans=(truth(0, 12, source="automatic"), truth(12, 24, "not_present", episode="empty")),
    )
    p = replace(
        p,
        rows={"a": Rows(p.days.rows["a"])},
        policy=replace(p.policy, quality={"models": {"a": model}}),
    )
    scores = exact_scores(p, [{"a": {0: 40}}, {"a": {0: 50}}], ((0, 24),))
    assert scores[0]["occupied_recall"] == 1
    assert scores[0]["false_active_seconds"] == 12
    assert scores[0]["sufficient_statistics"]["present/automatic"][1] == 12
    assert scores[0]["cost"] == scores[1]["cost"]
    assert model.calls == 101


def test_empty_problem_patterns_and_outside_regions():
    from tuner_under_test.engine.hybrid.exact import exact_scores

    p = problem({"a": [(70,), (0,), (20,), (20,)]})
    p = replace(p, rows={"a": Rows(p.days.rows["a"])})
    assert exact_scores(p, [], ((0, 24),)) == []
    got = exact_scores(p, [{"a": {0: 40}}], ((30, 40),))[0]
    assert got["cost"] is None
    assert got["covered_seconds"] == 0


@pytest.mark.parametrize("threshold", [-1, 101, 40.5, float("nan"), True])
def test_invalid_thresholds_rejected(threshold):
    from tuner_under_test.engine.hybrid.exact import exact_scores

    p = problem({"a": [(70,), (0,), (20,), (20,)]})
    p = replace(p, rows={"a": Rows(p.days.rows["a"])})
    with pytest.raises(ValueError, match="threshold"):
        exact_scores(p, [{"a": {0: threshold}}], ((0, 24),))


def test_exact_budget_includes_quiet_evaluations(monkeypatch):
    from tuner_under_test.engine.hybrid import evaluate

    p = problem({"a": [(70,), (0,), (20,), (20,)]})
    p = replace(p, rows={"a": Rows(p.days.rows["a"])})
    monkeypatch.setattr(evaluate, "EXACT_BUDGET", 16)
    assert evaluate.plan_scores(p, 4).mode == "exact"  # 4 rows x 4 patterns
    plan = evaluate.plan_scores(p, 4, quiet=True)  # baseline plus attenuated passes
    assert plan.mode == "cells_then_exact"
    assert plan.finalist_limit == 2
    assert plan.row_count == 4


def test_exact_scoring_degrades_to_cells_beyond_the_row_budget(monkeypatch):
    from tuner_under_test.engine.hybrid import evaluate

    p = problem({"a": [(70,), (0,), (20,), (20,)]})
    p = replace(p, rows={"a": Rows(p.days.rows["a"])})
    patterns = [{"a": {0: t}} for t in (90, 20, 40, 60)]
    monkeypatch.setattr(evaluate, "EXACT_BUDGET", 7)  # even start plus one contender exceed
    plan = evaluate.plan_scores(p, len(patterns))
    assert plan.mode == "cells"
    got = evaluate.evaluate_patterns(p, patterns, plan)
    assert [s["pattern_index"] for s in got] == [0, 1, 2, 3]
    assert {s["scoring_mode"] for s in got} == {"cells"}
    assert p.rows["a"].calls == []


def test_start_is_retained_when_preranking(monkeypatch):
    from tuner_under_test.engine.hybrid import evaluate

    p = problem({"a": [(70,), (60,), (20,), (20,)]})
    rows = Rows(p.days.rows["a"])
    p = replace(p, rows={"a": rows}, zones=(*p.zones, replace(p.zones[0], group_id="other")))
    patterns = [{"a": {0: t}} for t in (90, 20, 40, 60)]  # the start is the worst
    monkeypatch.setattr(evaluate, "EXACT_BUDGET", 8)
    plan = evaluate.plan_scores(p, len(patterns))
    assert (plan.mode, plan.finalist_limit) == ("cells_then_exact", 2)
    got = evaluate.evaluate_patterns(p, patterns, plan)
    assert [s["pattern_index"] for s in got][0] == 0
    assert len(got) == 2
    assert {s["scoring_mode"] for s in got} == {"exact"}
    assert got[0]["cost"] == 2 * got[0]["zones"]["z"]["cost"]


def test_all_compared_patterns_share_fidelity(monkeypatch):
    from tuner_under_test.engine.hybrid import evaluate

    p = problem({"a": [(70,), (60,), (20,), (20,)]})
    p = replace(p, rows={"a": Rows(p.days.rows["a"])})
    patterns = [{"a": {0: t}} for t in (90, 20, 40, 60)]
    monkeypatch.setattr(evaluate, "EXACT_BUDGET", 8)
    plan = evaluate.plan_scores(p, len(patterns), comparison=True)
    assert plan.mode == "cells"  # a fixed bundle is never pre-ranked away
    got = evaluate.evaluate_patterns(p, patterns, plan)
    assert len(got) == len(patterns)
    assert {s["scoring_mode"] for s in got} == {"cells"}
    monkeypatch.setattr(evaluate, "EXACT_BUDGET", 16)
    plan = evaluate.plan_scores(p, len(patterns), comparison=True)
    assert {s["scoring_mode"] for s in evaluate.evaluate_patterns(p, patterns, plan)} == {"exact"}


def test_cell_fallback_keeps_partial_coverage_and_label_edge_cells(monkeypatch):
    from tuner_under_test.engine.hybrid import evaluate

    p = problem({"a": [(70,), (0,), (20,), (20,)]})
    physical = [(1, 2, [70] + [255] * 17), (7, 8, [0] + [255] * 17)]
    p = replace(
        p,
        days=Days({"a": physical}),
        start=1,
        end=8,
        rows={"a": Rows(physical)},
        zones=(replace(p.zones[0], truth=(truth(1, 2), truth(7, 8, "not_present", episode="e"))),),
    )
    monkeypatch.setattr(evaluate, "EXACT_BUDGET", 0)
    plan = evaluate.plan_scores(p, 2, comparison=True)
    scores = evaluate.evaluate_patterns(p, [{"a": {0: 40}}, {"a": {0: 100}}], plan)
    assert {s["scoring_mode"] for s in scores} == {"cells"}
    assert scores[0]["covered_seconds"] == 2
    assert scores[0]["occupied_recall"] == 1
    assert scores[0]["cost"] == 0


def test_days_only_problem_uses_common_cell_fidelity():
    from tuner_under_test.engine.hybrid.evaluate import evaluate_patterns, plan_scores

    p = problem({"a": [(70,), (0,), (20,), (20,)]})
    plan = plan_scores(p, 2)
    assert plan.mode == "cells"
    got = evaluate_patterns(p, [{"a": {0: 40}}, {"a": {0: 100}}], plan)
    assert {s["scoring_mode"] for s in got} == {"cells"}


@pytest.mark.parametrize("mode", ("exact", "cells"))
def test_linked_zones_score_with_their_own_support_policy(mode):
    """Each zone's independent-support provenance reaches finalist scoring."""
    from array import array

    from tuner_under_test.engine.hybrid.digest import fold_day
    from tuner_under_test.engine.hybrid.evaluate import CellEvaluator
    from tuner_under_test.engine.hybrid.exact import Evaluator
    from tuner_under_test.engine.hybrid.physical_index import physical_rows
    from tuner_under_test.engine.hybrid.types import RowBlock, TruthSpan

    row_block = RowBlock(
        array("d", [0, 600, 660, 900]),
        array("d", [600, 660, 900, 2400]),
        (bytes([80, 0, 90, 0]),) + (bytes([255]) * 4,) * 17,
    )

    class OneDay:
        def dates(self, rid):
            return [0]

        def digest(self, rid, date, channels):
            return fold_day([row_block], 0, 2400, (0,))

    spans = (
        TruthSpan(0, 600, "present", "human", 1, "p", ()),
        TruthSpan(600, 900, "present", "automatic", 1, "ap", ()),
        TruthSpan(900, 2400, "not_present", "human", 1, "e", ()),
    )
    first = ZoneSpec("z1", ("r",), spans)
    support = {"human_episodes": {"present": 1}, "automatic_episode_mass": {"present": 0}}
    p = FitProblem(
        {"r": RadarSpec("r", (0,), {0: tuple(range(101))}, {0: 95}, {})},
        (first, replace(first, group_id="z2")),
        0,
        2400,
        OneDay(),
        {"r": physical_rows([row_block], 0, 2400, (0,))},
        EvidencePolicy(1, False, {"zones": {"z1": support, "z2": support}}),
    )
    kind = Evaluator if mode == "exact" else CellEvaluator
    with kind(p, ((0, 2400),)) as evaluator:
        score = evaluator.scores([{"r": {0: 40}}])[0]
    assert score["zones"]["z1"]["cost"] == 0
    assert score["zones"]["z2"]["cost"] == 0
