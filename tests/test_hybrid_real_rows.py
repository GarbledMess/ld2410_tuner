"""Raw diagnostics on rows off any clock: work follows pieces and decisions, bytes do not move."""

from array import array
from dataclasses import replace
from itertools import pairwise

import pytest
import test_tuner  # noqa: F401
from real_rows import THRESHOLD, plan, real_problem, shape
from test_hybrid_fragmented import (
    reference_append_session,
    reference_radar,
    wire,
)
from tuner_under_test.engine.hybrid import diagnostics, reports
from tuner_under_test.engine.hybrid.types import RowBlock, TruthSpan

ROWS = 40_000


@pytest.fixture(scope="module")
def real():
    return real_problem(ROWS)


def counted_totals(problem, device_id, truth):
    """Class time of every row with a duration; a reversed row has observed nothing."""
    totals = dict.fromkeys(diagnostics.STATES, 0.0)
    for a, b, span, _ in diagnostics._pieces(problem, device_id, truth):
        if b > a:
            totals[span.state] += b - a
    return totals


def reference(monkeypatch, problem, pattern):
    """What walking every row one at a time reports, as the original collector did."""
    with monkeypatch.context() as old:
        old.setattr(diagnostics, "_radar", reference_radar)
        old.setattr(diagnostics, "_append_session", reference_append_session)
        old.setattr(diagnostics, "_totals", counted_totals)
        return wire(diagnostics.diagnostics(problem, pattern, plan(ROWS)))


def counted(monkeypatch, owner, name):
    calls, original = [], getattr(owner, name)

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, counting)
    return calls


def test_fixture_has_the_real_shape(real):
    found = shape(real[0])
    assert found["rows"] == ROWS
    assert found["contiguous"] == pytest.approx(0.999, abs=0.001)
    assert found["same_10_channels"] == pytest.approx(0.30, abs=0.02)
    assert found["same_2_channels"] == pytest.approx(0.80, abs=0.02)
    assert found["missing"] == pytest.approx(0.002, abs=0.001)
    assert found["labels"] == pytest.approx(248 * found["days"], rel=0.05)
    blocks = real[0].rows["r"].stored
    assert not any(t % 6 == 0 for block in blocks for t in block.times)


def test_fixture_is_deterministic():
    first, second = real_problem(5000)[0], real_problem(5000)[0]
    assert first.rows["r"].stored == second.rows["r"].stored
    assert first.zones == second.zones


def test_full_diagnostics_never_walk_rows_one_at_a_time(real, monkeypatch):
    problem, pattern = real
    gaps = sum(
        map(
            float.__ne__,
            (t for block in problem.rows["r"].stored for t in block.times[1:]),
            (e for block in problem.rows["r"].stored for e in block.ends[:-1]),
        )
    )
    rows_missing = shape(problem)["missing"] * ROWS
    labels = len(problem.zones[0].truth)
    runs_bound = gaps + rows_missing + len(problem.rows["r"].stored) + 1
    pieces_bound = labels * 2 + runs_bound
    scalar = counted(monkeypatch, diagnostics, "_block_pieces")
    histograms = counted(monkeypatch, diagnostics, "_add_histograms")
    review_rows = counted(monkeypatch, diagnostics._Review, "add")
    sessions = counted(monkeypatch, diagnostics, "_session")
    walks = counted(monkeypatch, diagnostics._LabelPieces, "between")
    diagnostics.diagnostics_pair(problem, pattern, plan(ROWS))
    assert scalar == [] and histograms == [] and review_rows == []
    # Runs are walked once by each of the two collectors.
    assert len(walks) <= 2 * runs_bound
    # One review row per session, and the few periods that ever enter the kept top 24.
    assert len(sessions) <= 2 * pieces_bound
    assert pieces_bound < ROWS / 3


def test_the_report_side_writes_no_per_row_records(real, monkeypatch):
    from tuner_under_test.engine.hybrid import diagnostic_columns

    problem, pattern = real
    streams = counted(monkeypatch, diagnostic_columns.tempfile, "SpooledTemporaryFile")
    diagnostics.diagnostics_pair(problem, pattern, plan(ROWS))
    assert len(streams) == 1  # only the human collector still finds recent cutoffs


@pytest.mark.parametrize("channels", [10, 18])
def test_real_shape_bytes_match_walking_every_row(channels, monkeypatch):
    problem, pattern = real_problem(ROWS, channels)
    expected = reference(monkeypatch, problem, pattern)
    assert wire(diagnostics.diagnostics(problem, pattern, plan(ROWS))) == expected


def test_real_shape_pair_matches_each_separate_walk(real, monkeypatch):
    from tuner_under_test.engine.hybrid import fixed

    problem, pattern = real
    full = diagnostics.diagnostics(problem, pattern, plan(ROWS))["radars"]["r"]
    human_problem = replace(problem, zones=(fixed.source(problem.zones[0], "human"),))
    human = diagnostics.diagnostics(human_problem, pattern, plan(ROWS))["radars"]["r"]
    collected, review = diagnostics.diagnostics_pair(problem, pattern, plan(ROWS))
    report = collected["radars"]["r"]
    for channel, hist in report["histograms"].items():
        for state in diagnostics.STATES:
            assert wire(reports._gate_hist(hist, state)) == wire(
                reports._gate_hist(full["histograms"][channel], state)
            )
    assert wire(report["review"]) == wire(full["review"])
    assert wire(report["samples"]["present"]) == wire(full["samples"]["present"])
    assert wire(review["review"]) == wire(human["review"])
    assert review["recent_cutoffs"] == human["recent_cutoffs"]
    assert human["recent_cutoffs"]


def test_recent_cutoffs_exactly_match_row_by_row_subtraction(real, monkeypatch):
    problem, pattern = real
    found = diagnostics.diagnostics(problem, pattern, plan(ROWS))["radars"]["r"]
    with monkeypatch.context() as old:
        old.setattr(diagnostics, "_radar", reference_radar)
        old.setattr(diagnostics, "_totals", counted_totals)
        old.setattr(diagnostics, "_append_session", reference_append_session)
        walked = diagnostics.diagnostics(problem, pattern, plan(ROWS))["radars"]["r"]
    assert set(found["recent_cutoffs"]) == {"present", "not_present"}
    assert found["recent_cutoffs"] == walked["recent_cutoffs"]
    assert found["observed_seconds"] == walked["observed_seconds"]


class Listed:
    """One block per entry of (start, end, reading) rows, in the order given."""

    def __init__(self, *blocks):
        self.listed = blocks

    def blocks(self, lo, hi):
        for rows in self.listed:
            kept = [(max(a, lo), min(b, hi), v) for a, b, v in rows if a < hi and b > lo]
            if kept:
                yield RowBlock(
                    array("d", (row[0] for row in kept)),
                    array("d", (row[1] for row in kept)),
                    (bytes(row[2] for row in kept),),
                )

    def rows_in(self, lo, hi):
        return sum(1 for block in self.blocks(lo, hi) for _ in block.times)


def listed_problem(truth, *blocks):
    problem = real_problem(1, 3)[0]
    radar = replace(problem.radars["r"], channels=(0,), domains={0: tuple(range(101))})
    return replace(
        problem,
        radars={"r": radar},
        start=0,
        end=1000,
        zones=(replace(problem.zones[0], truth=truth),),
        rows={"r": Listed(*blocks)},
    )


def spans(*rows):
    return tuple(TruthSpan(a, b, state, "entity", 0.9, f"{a}", ()) for a, b, state in rows)


ROW_SETS = {
    "gap_inside": [[(0.5, 1.5, 70), (1.5, 2.5, 10), (9.25, 9.75, 70), (9.75, 14.5, 70)]],
    "missing_row": [[(0.5, 1.5, 70), (1.5, 2.5, 255), (2.5, 3.5, 70)]],
    "only_missing": [[(0.5, 1.5, 255), (1.5, 2.5, 255)]],
    "zero_length_row_changes_decision": [[(0.5, 1.5, 70), (1.5, 1.5, 10), (1.5, 2.5, 70)]],
    "reversed_row": [[(0.5, 1.5, 70), (3.0, 2.0, 10), (3.0, 4.5, 70)]],
    "single_row": [[(0.25, 800.5, 70)]],
    "block_edge_continues_period": [
        [(0.5, 1.5, 70), (1.5, 2.5, 70)],
        [(2.5, 3.5, 70), (3.5, 8.5, 5)],
    ],
    "block_edge_after_gap": [[(0.5, 1.5, 70)], [(2.0, 3.5, 70)]],
    "six_second_clock": [[(float(i), float(i + 6), 70 if i % 12 else 5) for i in range(0, 90, 6)]],
    "label_edge_on_row_edges": [
        [(0.0, 10.0, 70), (10.0, 20.0, 5), (20.0, 30.0, 70), (30.0, 40.0, 5)]
    ],
}
LABELS = {
    "one_present": spans((0, 900, "present")),
    "one_empty": spans((0.25, 899.5, "not_present")),
    "edges_at_ten_and_twenty": spans(
        (0, 10, "present"), (10, 20, "not_present"), (20, 900, "present")
    ),
    "inside_a_row": spans((0.75, 1.25, "not_present"), (1.25, 1.3, "present")),
    "gap_between": spans((0, 2, "present"), (3, 40, "not_present")),
}


@pytest.mark.parametrize("rows", ROW_SETS)
@pytest.mark.parametrize("labels", LABELS)
def test_awkward_rows_match_walking_every_row(rows, labels, monkeypatch):
    problem = listed_problem(LABELS[labels], *ROW_SETS[rows])
    pattern = {"r": {0: THRESHOLD}}
    expected = reference(monkeypatch, problem, pattern)
    assert wire(diagnostics.diagnostics(problem, pattern, plan(ROWS))) == expected


def test_no_channels_match_walking_every_row(monkeypatch):
    problem = listed_problem(LABELS["one_present"], *ROW_SETS["gap_inside"])
    radar = replace(problem.radars["r"], channels=(), domains={})
    problem = replace(problem, radars={"r": radar})
    expected = reference(monkeypatch, problem, {"r": {}})
    assert wire(diagnostics.diagnostics(problem, {"r": {}}, plan(ROWS))) == expected


def test_a_period_keeps_every_gate_that_disagreed_in_any_of_its_rows():
    rows = [(float(i), float(i + 1), 70 if i % 2 else 80) for i in range(20)]
    problem = listed_problem(spans((0, 20, "not_present")), rows)
    review = diagnostics.diagnostics(problem, {"r": {0: 40}}, plan(ROWS))["radars"]["r"]["review"]
    assert [(p["start"], p["end"]) for p in review["periods"]] == [(0, 20.0)]
    assert review["periods"][0]["errors"] == pytest.approx(20 / 6)
    assert review["periods"][0]["gates"] == ["g0_move"]


def test_runs_are_sorted_and_disjoint_on_real_rows(real):
    from tuner_under_test.engine.hybrid.diagnostic_columns import Prepared

    problem, _ = real
    block = problem.rows["r"].stored[0]
    channels = problem.radars["r"].channels
    prepared = Prepared(block, channels, dict.fromkeys(channels, THRESHOLD))
    runs = list(prepared.runs)
    assert all(left[1] <= right[0] for left, right in pairwise(runs))
    for first, stop in runs:
        assert block.times[first + 1 : stop] == block.ends[first : stop - 1]


def human_label(start, stop, state):
    return TruthSpan(start, stop, state, "human", 1.0, f"h{start}", ())


def alternating_rows():
    return [(float(i), float(i + 1), 70 if i % 3 else 5) for i in range(80)]


AUTOMATIC = (
    TruthSpan(0, 40, "present", "entity", 0.9, "p", ()),
    TruthSpan(40, 80, "not_present", "entity", 0.9, "n", ()),
)


def test_automatic_histograms_are_skipped_where_human_time_answers(real):
    problem, pattern = real
    collected, _ = diagnostics.diagnostics_pair(problem, pattern, plan(ROWS))
    hist = collected["radars"]["r"]["histograms"][0]
    assert any(hist["human"]["present"]) and any(hist["human"]["not_present"])
    assert not any(hist["automatic"]["present"]) and not any(hist["automatic"]["not_present"])


def test_only_the_class_without_human_labels_keeps_automatic_histograms():
    truth = (*AUTOMATIC, human_label(50, 60, "not_present"))
    problem = listed_problem(truth, alternating_rows())
    pattern = {"r": {0: THRESHOLD}}
    full = diagnostics.diagnostics(problem, pattern, plan(ROWS))["radars"]["r"]["histograms"][0]
    collected, _ = diagnostics.diagnostics_pair(problem, pattern, plan(ROWS))
    hist = collected["radars"]["r"]["histograms"][0]
    assert hist["automatic"]["present"] == full["automatic"]["present"]
    assert not any(hist["automatic"]["not_present"])
    for state in diagnostics.STATES:
        assert reports._gate_hist(hist, state) == reports._gate_hist(full, state)


def test_a_human_label_over_no_rows_still_gets_its_automatic_fallback(monkeypatch):
    truth = (*AUTOMATIC, human_label(50, 60, "not_present"), human_label(90, 95, "present"))
    problem = listed_problem(truth, alternating_rows())
    pattern = {"r": {0: THRESHOLD}}
    full = diagnostics.diagnostics(problem, pattern, plan(ROWS))["radars"]["r"]["histograms"][0]
    passes = counted(monkeypatch, problem.rows["r"], "blocks")
    collected, _ = diagnostics.diagnostics_pair(problem, pattern, plan(ROWS))
    hist = collected["radars"]["r"]["histograms"][0]
    assert len(passes) == 2
    assert not any(hist["human"]["present"])
    assert reports._gate_hist(hist, "present") == reports._gate_hist(full, "present")
    assert reports._gate_hist(hist, "not_present") == reports._gate_hist(full, "not_present")


def test_real_shape_without_human_labels_matches_each_separate_walk(real):
    problem, pattern = real
    truth = tuple(span for span in problem.zones[0].truth if span.source != "human")
    problem = replace(problem, zones=(replace(problem.zones[0], truth=truth),))
    full = diagnostics.diagnostics(problem, pattern, plan(ROWS))["radars"]["r"]
    collected, _ = diagnostics.diagnostics_pair(problem, pattern, plan(ROWS))
    assert wire(collected["radars"]["r"]["histograms"]) == wire(full["histograms"])
