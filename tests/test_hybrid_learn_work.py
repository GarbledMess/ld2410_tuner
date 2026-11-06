"""A real-shaped Learn keeps its report while its work follows decisions, not rows."""

import json
import random
from array import array
from bisect import bisect_left, bisect_right
from dataclasses import replace
from pathlib import Path

import pytest
import test_tuner  # noqa: F401
from hybrid_real_shape import recorded
from test_hybrid_fragmented import fragmented_problem, reference_radar, wire
from test_hybrid_search_columns import problem as search_problem
from test_hybrid_search_columns import truth
from tuner_under_test.calibration import event_quality
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.engine.hybrid import (
    diagnostics,
    exact,
    facade,
    quality,
    scoring,
    search_columns,
)
from tuner_under_test.engine.hybrid.search import collect_training
from tuner_under_test.engine.hybrid.search_scores import conditional_costs, coordinate_costs
from tuner_under_test.engine.hybrid.training import TrainingStore
from tuner_under_test.engine.hybrid.types import RowBlock, ScorePlan, TruthSpan
from tuner_under_test.history.label_lookup import index_reader

BASELINE = Path(__file__).parent / "fixtures" / "hybrid_learn_parity.json"
CASES = {
    "plain": {"timing": None, "weight": 0.0},
    "weighted": {"timing": None, "weight": 0.3},
    "timed": {"timing": {"timeout": 12, "off_delay": 6}, "weight": 0.0},
}


def comparable(value):
    """A report as the baseline records it: six decimals, no input fingerprint."""
    if isinstance(value, dict):
        return {k: comparable(v) for k, v in value.items() if k != "fingerprint"}
    if isinstance(value, (list, tuple)):
        return [comparable(v) for v in value]
    return round(value, 6) if isinstance(value, float) else value


def learn(view, case):
    keys = list(HISTORY_KEYS)
    current = dict.fromkeys(keys, 90)
    return facade.fit(view, "r", keys, current, case["timing"], case["weight"])


@pytest.mark.parametrize("name", CASES)
def test_learn_report_matches_the_recorded_baseline_cold_and_warm(tmp_path, name):
    expected = json.loads(BASELINE.read_text(encoding="utf8"))[name]
    view = recorded(tmp_path, 6, irregular=True)
    for _ in range(2):
        found = json.loads(json.dumps(comparable(learn(view, CASES[name])), default=repr))
        assert found == expected


def _labelled_rows(view):
    return sum(
        int(label["end"] - label["start"]) // 6 + 2
        for label in view.data["devices"]["r"]["history_labels"]
    )


def test_quality_visits_only_rows_inside_explicit_labels(tmp_path, monkeypatch):
    view = recorded(tmp_path, 3, irregular=True)
    calls, original = [], quality._Densities.add

    def counted(self, cell):
        calls.append(1)
        return original(self, cell)

    monkeypatch.setattr(quality._Densities, "add", counted)
    context = quality.collect_quality(view, "r", list(HISTORY_KEYS), 0, 3 * 86400, None, None)
    assert context.automatic_episodes
    assert 0 < len(calls) <= _labelled_rows(view)


def _cells_oracle(blocks, reader, owner, start, end):
    """The original per-cell walk that the interval pass must equal."""
    labels = event_quality._LabelCells(reader, owner, start, end)
    for block in blocks:
        values = (map(quality._PHYSICAL_VALUES.__getitem__, c) for c in block.columns)
        rows = zip(*values, strict=True)
        for lo, hi, row in zip(block.times, block.ends, rows, strict=True):
            low, high = max(start, lo), min(end, hi)
            while low < high:
                state, parent, edge = labels.at(low)
                stop = min(high, edge)
                yield low, stop, row, state, parent
                low = stop


class _Source:
    def __init__(self, blocks, channels):
        self.view, self.channels, self.rows = object(), channels, blocks

    def blocks(self, _lo, _hi):
        return iter(self.rows)


# Row lengths, gaps before a row and readings. An empty row sends a whole block down the
# one-row-at-a-time path, so the other shapes must not contain one or they test nothing.
SHAPES = {
    "empty_rows": ((6, 6, 6, 0.25, 0), (0, 0, 0, 0.5, 40), (255, 3, 40, 90)),
    "many_gaps": ((6,), (0, 0, 0, 0, 0, 0, 0, 18), (3, 40, 90)),
    "bursts_and_gaps": ((6, 6, 6, 2.25, 0.5), (0, 0, 0, 0, 0, 0, 1.5, 40), (3, 40, 90)),
    "gaps_and_missing": ((6, 6, 6, 0.25), (0, 0, 0, 0, 0, 12), (255, 3, 40, 90, 7, 12, 60, 21)),
}


def _messy_block(rng, rows, channels, shape):
    lengths, gaps, readings = SHAPES[shape]
    times, ends, at = [], [], rng.uniform(0, 3)
    for _ in range(rows):
        at += rng.choice(gaps)
        stop = at + rng.choice(lengths)
        times.append(at)
        ends.append(stop)
        at = stop
    columns = tuple(
        bytes(rng.choice(readings) if c in channels else 255 for _ in range(rows))
        for c in range(18)
    )
    return RowBlock(array("d", times), array("d", ends), columns)


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("channels", [(0,), (0, 1, 2, 3, 4, 5)])
@pytest.mark.parametrize("shape", SHAPES)
def test_interval_evidence_equals_the_per_cell_walk(seed, channels, shape):
    rng = random.Random(seed)
    block = _messy_block(rng, 400, channels, shape)
    span = block.ends[-1]
    edges = sorted(rng.uniform(0, span) for _ in range(9))
    states = tuple(rng.choice((None, "present", "not_present", "unknown")) for _ in edges)
    reader = index_reader((edges, states))
    keys = [HISTORY_KEYS[c] for c in channels]
    runs = tuple(
        (a, b, rng.choice(("present", "not_present")), 18, i % 3)
        for i, (a, b) in enumerate(zip(edges, edges[1:], strict=False))
        if b > a
    )
    start, end = span * 0.05, span * 0.95
    owner = ("r",)

    oracle = quality._Densities(keys)
    cells = _cells_oracle([block], reader, owner, start, end)
    observed = (cell for cell in cells if oracle.add(cell) is None)
    expected = event_quality._source_evidence(observed, runs, owner, keys)
    oracle.finish()

    found = quality._Densities(keys)
    digest = quality.sha256()
    source = _Source([block], channels)
    intervals = quality._native_intervals(source, reader, owner, start, end, found, digest)
    actual = event_quality._interval_evidence(intervals, runs, owner, keys)
    found.finish()

    assert actual == expected
    assert found.episodes == oracle.episodes
    assert found.counts == oracle.counts
    assert found.empty == oracle.empty
    assert found.sums == oracle.sums


def test_a_readable_run_with_several_gaps_keeps_every_row():
    times = array("d", (0.0, 6.0, 30.0, 36.0, 60.0, 66.0, 90.0))
    ends = array("d", (6.0, 12.0, 36.0, 42.0, 66.0, 72.0, 96.0))
    assert list(quality._joined_rows(times, ends, 0, 7)) == [(0, 2), (2, 4), (4, 6), (6, 7)]
    assert list(quality._joined_rows(times, ends, 2, 6)) == [(2, 4), (4, 6)]


class _Rows:
    def __init__(self, block):
        self.block = block

    def blocks(self, lo, hi):
        first = bisect_right(self.block.ends, lo)
        stop = bisect_left(self.block.times, hi)
        if stop > first:
            times, ends = self.block.times[first:stop], self.block.ends[first:stop]
            times[0], ends[-1] = max(lo, times[0]), min(hi, ends[-1])
            columns = tuple(column[first:stop] for column in self.block.columns)
            yield RowBlock(times, ends, columns)


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("shape", SHAPES)
def test_diagnostics_of_messy_blocks_equal_the_scalar_collector(tmp_path, monkeypatch, seed, shape):
    rng = random.Random(seed)
    channels = (0, 1, 2)
    block = _messy_block(rng, 1200, channels, shape)
    span = block.ends[-1]
    edges = sorted(rng.uniform(0, span) for _ in range(60))
    truth_spans = tuple(
        TruthSpan(a, b, rng.choice(("present", "not_present")), "entity", 0.9, str(i), ())
        for i, (a, b) in enumerate(zip(edges, edges[1:], strict=False))
        if b > a
    )
    problem = fragmented_problem(tmp_path)
    problem = replace(
        problem,
        start=0,
        end=span,
        rows={"r": _Rows(block)},
        radars={"r": replace(problem.radars["r"], channels=channels)},
        zones=(replace(problem.zones[0], truth=truth_spans),),
    )
    pattern = {"r": dict.fromkeys(channels, 30)}
    plan = ScorePlan("exact", (), 1200, 2)
    actual = json.loads(wire(diagnostics.diagnostics(problem, pattern, plan)))
    monkeypatch.setattr(diagnostics, "_radar", reference_radar)
    expected = json.loads(wire(diagnostics.diagnostics(problem, pattern, plan)))
    assert _close(actual, expected)


def _close(left, right):
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_close(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(map(_close, left, right))
    if isinstance(left, float) or isinstance(right, float):
        return abs(left - right) <= 1e-6
    return left == right


@pytest.mark.parametrize("shape", SHAPES)
def test_column_cache_round_trips_messy_blocks(shape):
    from tuner_under_test.history import column_cache

    block = _messy_block(random.Random(5), 900, (0, 1), shape)
    found = column_cache._decode(column_cache._encode(block), 900)
    assert (found.times, found.ends, found.columns) == (block.times, block.ends, block.columns)


def test_quality_fingerprint_follows_readings_and_labels():
    keys = [HISTORY_KEYS[0]]

    def fingerprint(block, states):
        digest = quality.sha256()
        reader = index_reader(([0.0, 30.0], states))
        intervals = quality._native_intervals(
            _Source([block], (0,)), reader, ("r",), 0, 60, quality._Densities(keys), digest
        )
        list(intervals)
        return digest.hexdigest()

    times, ends = array("d", (0.0, 6.0)), array("d", (6.0, 12.0))
    block = RowBlock(times, ends, (bytes((1, 2)),) + (bytes((255, 255)),) * 17)
    changed = RowBlock(times, ends, (bytes((1, 3)),) + (bytes((255, 255)),) * 17)
    states = ("present", None)
    assert fingerprint(block, states) == fingerprint(block, states)
    assert fingerprint(block, states) != fingerprint(changed, states)
    assert fingerprint(block, states) != fingerprint(block, ("not_present", None))


def _changing_rows(gap_at=None):
    class Rows:
        def blocks(self, lo, hi):
            times = [i * 6.0 for i in range(14400) if gap_at is None or i != gap_at]
            selected = [i for i, t in enumerate(times) if t < hi and t + 6 > lo]
            yield RowBlock(
                array("d", (max(lo, times[i]) for i in selected)),
                array("d", (min(hi, times[i] + 6) for i in selected)),
                tuple(
                    bytes((50 if i // 58 % 2 == 0 else 5) + (i * (c + 3)) % 19 for i in selected)
                    for c in range(18)
                ),
            )

    return Rows()


def test_diagnostics_stay_compact_around_an_outage(tmp_path, monkeypatch):
    problem = fragmented_problem(tmp_path)
    truth = tuple(
        TruthSpan(
            i * 348 + 0.125,
            (i + 1) * 348 + 0.125,
            "present" if i % 2 == 0 else "not_present",
            "human" if i % 11 == 0 else "entity",
            0.9,
            str(i),
            (),
        )
        for i in range(248)
    )
    problem = replace(
        problem,
        end=86400,
        rows={"r": _changing_rows(gap_at=7000)},
        radars={"r": replace(problem.radars["r"], channels=tuple(range(18)))},
        zones=(replace(problem.zones[0], truth=truth),),
    )
    pattern = {"r": dict.fromkeys(range(18), 40)}
    plan = ScorePlan("exact", (), 14400, 2)
    with monkeypatch.context() as old:
        old.setattr(diagnostics, "_radar", reference_radar)
        expected = wire(diagnostics.diagnostics(problem, pattern, plan))
    calls, original = [], diagnostics._Review.add

    def counted(self, *args):
        calls.append(1)
        return original(self, *args)

    monkeypatch.setattr(diagnostics._Review, "add", counted)
    assert wire(diagnostics.diagnostics(problem, pattern, plan)) == expected
    assert len(calls) < 6 * len(truth)


def _search_problem(weight, *, human_classes=("present", "not_present")):
    rng = random.Random(7)
    vectors = {"a": [(rng.choice((0, 9, 20, 40, 70)),) for _ in range(200)]}
    spans = [
        truth(
            i * 30,
            (i + 1) * 30,
            "present" if i % 2 else "not_present",
            source="entity",
            confidence=0.75,
            episode=str(i),
        )
        for i in range(30)
    ]
    for i, state in enumerate(human_classes):
        spans.append(truth(900 + i * 120, 1020 + i * 120, state, source="human", episode=f"h{i}"))
    problem = search_problem(vectors, spans)
    return replace(problem, policy=replace(problem.policy, automatic_weight=weight))


def _lanes(problem, tmp_path):
    with TrainingStore(tmp_path) as store:
        collect_training(problem, store)
        block = search_columns.column_block(problem, store, problem.zones[0], False)
        return None if block is None else set(block.sections)


def test_zero_weight_search_never_reads_unpriced_automatic_readings(tmp_path):
    zero = _lanes(_search_problem(0.0), tmp_path)
    weighted = _lanes(_search_problem(0.5), tmp_path)
    assert zero == {"present/human", "not_present/human"}
    assert {"present/automatic", "not_present/automatic"} <= weighted


def test_automatic_only_class_stays_priced_at_zero_weight(tmp_path):
    problem = _search_problem(0.0, human_classes=("present",))
    assert "not_present/automatic" in _lanes(problem, tmp_path)


@pytest.mark.parametrize("weight", [0.0, 0.5])
@pytest.mark.parametrize("attenuate", [False, True])
def test_settled_lanes_cost_the_same_as_streaming(tmp_path, monkeypatch, weight, attenuate):
    from tuner_under_test.engine.hybrid import search_masks

    monkeypatch.setattr(search_masks, "packed_block", lambda *args: None)
    problem = _search_problem(weight)
    pattern = {"a": {0: 40}}
    domain = tuple(range(101))
    with TrainingStore(tmp_path) as store:
        collect_training(problem, store)
        results = []
        for size in (0, 32 * 1024**2):
            monkeypatch.setattr(search_columns, "COLUMN_BYTES", size)
            store.__dict__.pop("_column_blocks", None)
            results.append(
                (
                    coordinate_costs(problem, store, pattern, "a", 0, domain, attenuate=attenuate),
                    conditional_costs(problem, store, pattern, "a", 0, domain, attenuate=attenuate),
                )
            )
    assert results[0][0] == pytest.approx(results[1][0], abs=1e-9)
    assert results[0][1] == pytest.approx(results[1][1], abs=1e-9)


def test_support_tables_are_built_once_per_gate(tmp_path, monkeypatch):
    problem = _search_problem(0.5)

    class Support:
        calls = 0

        def supports(self, _key, value):
            type(self).calls += 1
            return value >= 10

    quality_map = {**problem.policy.quality, "models": {"a": Support()}}
    problem = replace(problem, policy=replace(problem.policy, quality=quality_map))
    with TrainingStore(tmp_path) as store:
        collect_training(problem, store)
        assert search_columns.column_block(problem, store, problem.zones[0], False) is not None
    assert Support.calls <= 18 * 101


def test_scorers_of_one_zone_share_its_resolved_labels(tmp_path, monkeypatch):
    problem = fragmented_problem(tmp_path)
    calls, original = [], exact._labels

    def counted(spans):
        calls.append(1)
        return original(spans)

    monkeypatch.setattr(exact, "_labels", counted)
    monkeypatch.setattr(scoring, "_labels", counted)
    patterns = [{"r": {0: threshold}} for threshold in (40, 50, 60, 70)]
    with exact.Evaluator(problem, exact.regions(problem.zones[0].truth)) as evaluator:
        evaluator.scores(patterns, zones=problem.zones)
    # One for the zone's scorers, one for its quiet variant; none per pattern.
    assert len(calls) <= 2
