"""Synthetic fragmented-label parity and deterministic work bounds."""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, fields, is_dataclass, replace
from math import ceil
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from test_hybrid_facade import alternating, worker_view
from test_hybrid_reports import problem
from tuner_under_test.engine.hybrid import diagnostics, engine, facade, search
from tuner_under_test.engine.hybrid.evidence import _labels
from tuner_under_test.engine.hybrid.types import ScorePlan, TruthSpan


def legacy_masked_cells(t, n, mask):
    total = 0
    for a, b in mask:
        lo = max(0, ceil((a - t) / 6))
        hi = min(n, ceil((b - t) / 6))
        total += max(0, hi - lo)
    return total


def legacy_removed_rows(store, zone, mask):
    start, stop = min(a for a, _ in mask), max(b for _, b in mask)
    rows = store._database.execute(
        "SELECT state,source,vector,t,n FROM search_pure WHERE zone=? AND t<? "
        "AND t>=?-(SELECT max_n-1 FROM search_bounds WHERE zone=?)*6 AND t+(n-1)*6>=?",
        (zone.group_id, stop, start, zone.group_id, start),
    )
    for state, source, vector, t, n in rows:
        removed = legacy_masked_cells(t, n, mask)
        if removed:
            yield state, source, vector, removed


def legacy_pieces(p, device_id, truth):
    radar = p.radars[device_id]
    for lo, hi, span in _labels(truth):
        lo, hi = max(lo, p.start), min(hi, p.end)
        for block in p.rows[device_id].blocks(lo, hi):
            for i, a in enumerate(block.times):
                values = tuple(block.columns[c][i] for c in radar.channels)
                if 255 not in values:
                    yield max(lo, a), min(hi, block.ends[i]), span, values


def wire(value):
    def default(item):
        if is_dataclass(item):
            return {field.name: getattr(item, field.name) for field in fields(item)}
        if isinstance(item, Mapping):
            return dict(item)
        return list(item)

    return json.dumps(value, default=default, separators=(",", ":")).encode()


def mask_store(count):
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE search_pure(zone,state,source,vector,t,n);"
        "CREATE TABLE search_bounds(zone,max_n);"
        "CREATE INDEX pure_time ON search_pure(zone,t);"
        "INSERT INTO search_bounds VALUES ('z',4);"
    )
    db.executemany(
        "INSERT INTO search_pure VALUES ('z','present','human',?,?,?)",
        ((bytes([i % 101]), i * 24, 4) for i in reversed(range(count))),
    )
    return SimpleNamespace(_database=db), SimpleNamespace(group_id="z")


def test_mask_merge_work_is_linear(monkeypatch):
    store, zone = mask_store(1660)
    mask = tuple((i * 24 + 1, i * 24 + 13) for i in range(1660))
    expected = sorted(legacy_removed_rows(store, zone, mask))
    calls = 0
    original = search.ceil

    def counted(value):
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(search, "ceil", counted)
    try:
        assert sorted(search._removed_rows(store, zone, mask)) == expected
        assert calls <= 4 * 1660
    finally:
        store._database.close()


@pytest.mark.parametrize("mask", [((1, 13), (49, 61)), ((-20, 200),), ((6, 6),), ((90, 100),)])
def test_mask_merge_matches_original_boundaries(mask):
    store, zone = mask_store(4)
    try:
        assert sorted(search._removed_rows(store, zone, mask)) == sorted(
            legacy_removed_rows(store, zone, mask)
        )
    finally:
        store._database.close()


def fragmented_problem(tmp_path, count=248):
    values, _ = alternating(count)
    view = worker_view(tmp_path, "r", values)
    p = problem()
    spans = tuple(
        TruthSpan(
            i * 120 + 0.25,
            (i + 1) * 120 - 0.5,
            "present" if i % 2 == 0 else "not_present",
            "human" if i % 11 == 0 else "entity",
            0.9,
            str(i),
            (),
        )
        for i in range(count)
    )
    from tuner_under_test.engine.hybrid.adapters import _WindowRows
    from tuner_under_test.history.fit_rows import CapturedRowSource

    rows = _WindowRows(CapturedRowSource(view, "r", (0,)), 0, count * 120)
    return replace(p, end=count * 120, zones=(replace(p.zones[0], truth=spans),), rows={"r": rows})


def test_diagnostics_opens_each_day_once_per_pass(tmp_path, monkeypatch):
    p = fragmented_problem(tmp_path)
    source = p.rows["r"].source
    original = source._columns
    opens = []

    def counted(date):
        opens.append(date)
        return original(date)

    expected = list(legacy_pieces(p, "r", p.zones[0].truth))
    monkeypatch.setattr(source, "_columns", counted)
    assert list(diagnostics._pieces(p, "r", p.zones[0].truth)) == expected
    assert len(opens) == 1


def test_diagnostics_bytes_match_original(tmp_path, monkeypatch):
    p = fragmented_problem(tmp_path)
    plan = ScorePlan("exact", (), 248, 2)
    current = diagnostics.diagnostics(p, {"r": {0: 40}}, plan)
    monkeypatch.setattr(diagnostics, "_pieces", legacy_pieces)
    monkeypatch.setattr(diagnostics, "_radar", reference_radar)
    monkeypatch.setattr(diagnostics, "_append_session", reference_append_session)
    monkeypatch.setattr(diagnostics, "_totals", reference_totals)
    assert wire(current) == wire(diagnostics.diagnostics(p, {"r": {0: 40}}, plan))


def test_fit_thresholds_diagnostics_and_report_bytes_match_original(tmp_path, monkeypatch):
    from test_hybrid_facade import device_fit

    values, labels = alternating(248)
    values[100:120] = [10] * 20
    view = worker_view(tmp_path, "r", values, labels)
    fits = []
    original = engine.fit

    def recorded(*args, **kwargs):
        result = original(*args, **kwargs)
        fits.append(wire(asdict(result)))
        return result

    monkeypatch.setattr(engine, "fit", recorded)
    current = wire(device_fit(view))
    current_fits = list(fits)
    fits.clear()
    monkeypatch.setattr(search, "_removed_rows", legacy_removed_rows)
    monkeypatch.setattr(diagnostics, "_pieces", legacy_pieces)
    monkeypatch.setattr(diagnostics, "_radar", reference_radar)
    monkeypatch.setattr(diagnostics, "_append_session", reference_append_session)
    monkeypatch.setattr(diagnostics, "_totals", reference_totals)
    assert wire(facade.fit(view, "r", ["g0_move"], {"g0_move": 90}, None, 0.0)) == current
    assert fits == current_fits


def test_masked_fit_and_report_bytes_match_original(monkeypatch):
    from test_hybrid_anomaly import phone_case, problem_for

    rows, truth = phone_case()
    p = problem_for(rows, truth)
    current = engine.fit(p)
    assert current.diagnostics["anomaly"]["status"] == "discarded"
    audit = {"cutoff": None, "validated": False, "reason": "synthetic", "selected": "reference"}
    current_report = wire(facade._device_report(p, current, audit))
    monkeypatch.setattr(search, "_removed_rows", legacy_removed_rows)
    monkeypatch.setattr(diagnostics, "_pieces", legacy_pieces)
    monkeypatch.setattr(diagnostics, "_radar", reference_radar)
    monkeypatch.setattr(diagnostics, "_append_session", reference_append_session)
    monkeypatch.setattr(diagnostics, "_totals", reference_totals)
    previous = engine.fit(p)
    assert wire(asdict(current)) == wire(asdict(previous))
    assert current_report == wire(facade._device_report(p, previous, audit))


@pytest.mark.parametrize("bounds", [(0, 240), (17.5, 200.25), (50, 50), (300, 400)])
def test_piece_merge_preserves_gaps_overrides_missing_and_crossing_rows(bounds):
    from test_hybrid_reports import Rows

    p = problem()
    truth = (
        TruthSpan(-10, 300, "present", "entity", 0.8, "a", ()),
        TruthSpan(30, 60, "unknown", "human", 1, "u", ()),
        TruthSpan(80, 170, "not_present", "human", 1, "h", ()),
    )
    p = replace(
        p,
        start=bounds[0],
        end=bounds[1],
        zones=(replace(p.zones[0], truth=truth),),
        rows={"r": Rows([(0, 100, 70), (100, 140, 255), (160, 200, 10), (210, 240, 90)])},
    )
    assert wire(list(diagnostics._pieces(p, "r", truth))) == wire(
        list(legacy_pieces(p, "r", truth))
    )


def test_diagnostics_reuses_repeated_gate_classification(monkeypatch):
    from test_hybrid_reports import problem

    p = problem(split=200)
    calls = 0
    original = diagnostics._error_gates

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(diagnostics, "_error_gates", counted)
    diagnostics.diagnostics(p, {"r": {0: 40}}, ScorePlan("exact", (), 800, 2))
    assert calls <= 4


def test_totals_does_not_decode_gate_vectors(monkeypatch):
    p = problem(split=200)
    calls = 0
    original = diagnostics._block_pieces

    def counted(*args):
        nonlocal calls
        calls += 1
        yield from original(*args)

    monkeypatch.setattr(diagnostics, "_block_pieces", counted)
    assert diagnostics._totals(p, "r", p.zones[0].truth) == {"present": 120.0, "not_present": 120.0}
    assert calls == 0


def test_review_does_not_sort_unchanged_gates(monkeypatch):
    from builtins import sorted as original

    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(diagnostics, "sorted", counted, raising=False)
    p = problem(split=200)
    diagnostics.diagnostics(p, {"r": {0: 40}}, ScorePlan("exact", (), 800, 2))
    assert calls <= 10


def reference_radar(problem, device_id, pattern, truth):
    channels = problem.radars[device_id].channels
    totals = diagnostics._totals(problem, device_id, truth)
    remaining = {s: 0.8 * totals[s] for s in diagnostics.STATES}
    cutoffs, histograms = {}, diagnostics._histograms(channels)
    samples = {s: diagnostics.deque(maxlen=5000) for s in diagnostics.STATES}
    counts = dict.fromkeys(diagnostics.STATES, 0)
    review = diagnostics._Review()
    for a, b, span, values in diagnostics._pieces(problem, device_id, truth):
        if b <= a:
            continue
        state = span.state
        if state not in cutoffs and remaining[state] < b - a:
            cutoffs[state] = a + remaining[state]
        remaining[state] = max(0, remaining[state] - (b - a))
        diagnostics._add_histograms(histograms, channels, values, span, b - a)
        diagnostics._samples(samples, counts, a, b, span, channels, values)
        hit = any(v > pattern[device_id][c] for c, v in zip(channels, values, strict=True))
        errors = (b - a) * (hit != (state == "present"))
        review.add(
            a,
            b,
            span,
            errors,
            diagnostics._error_gates(channels, values, pattern[device_id], state),
        )
    return {
        "samples": {s: diagnostics.SampleSlots(channels, v) for s, v in samples.items()},
        "sample_counts": counts,
        "histograms": histograms,
        "recent_cutoffs": cutoffs,
        "review": review.result(),
        "observed_seconds": totals,
    }


def test_diagnostics_bulk_constant_runs(monkeypatch):
    from array import array

    from tuner_under_test.engine.hybrid.types import RowBlock

    class ConstantRows:
        def blocks(self, lo, hi):
            yield RowBlock(
                array("d", range(int(lo), int(hi), 6)),
                array("d", range(int(lo) + 6, int(hi) + 6, 6)),
                (bytes([70]) * int((hi - lo) / 6),) + (bytes([255]) * int((hi - lo) / 6),) * 17,
            )

    p = replace(problem(), rows={"r": ConstantRows()})
    calls = 0
    original = diagnostics._Review.add

    def counted(self, *args):
        nonlocal calls
        calls += 1
        return original(self, *args)

    monkeypatch.setattr(diagnostics._Review, "add", counted)
    current = diagnostics.diagnostics(p, {"r": {0: 40}}, ScorePlan("exact", (), 40, 2))
    assert calls <= 4
    monkeypatch.setattr(diagnostics, "_radar", reference_radar)
    monkeypatch.setattr(diagnostics, "_append_session", reference_append_session)
    monkeypatch.setattr(diagnostics, "_totals", reference_totals)
    assert wire(current) == wire(
        diagnostics.diagnostics(p, {"r": {0: 40}}, ScorePlan("exact", (), 40, 2))
    )


def reference_append_session(pending, a, b, span, errors, gates):
    identity = (span.state, span.source, span.episode_id)
    if pending and pending[0] == identity and pending[1]["end"] == a:
        row = pending[1]
        row["gates"] = sorted(set(row["gates"]) | set(gates))
        row["end"] = b
        row["samples"] += (b - a) / 6
        row["errors"] += errors / 6
        row["observed_span_seconds"] += b - a
        row["short_burst"] = row["observed_span_seconds"] < 6
        return pending, None
    row = diagnostics._session(a, b, span, errors)
    row["gates"] = gates
    return (identity, row), pending


def reference_totals(problem, device_id, truth):
    totals = dict.fromkeys(diagnostics.STATES, 0.0)
    for a, b, span, _ in legacy_pieces(problem, device_id, truth):
        totals[span.state] += b - a
    return totals


@pytest.mark.parametrize("fractional", [False, True])
def test_multichannel_run_bytes_match_reference(fractional, monkeypatch):
    from array import array

    from tuner_under_test.engine.hybrid.types import RowBlock

    class ColumnRows:
        def blocks(self, lo, hi):
            offset = 0.25 if fractional else 0
            times = [i * 6 + offset for i in range(12000)]
            selected = [i for i, t in enumerate(times) if t < hi and t + 6 > lo]
            yield RowBlock(
                array("d", (max(lo, times[i]) for i in selected)),
                array("d", (min(hi, times[i] + 6) for i in selected)),
                tuple(
                    bytes(255 if i % 997 == 0 else (i // 100 + c) % 101 for i in selected)
                    for c in range(18)
                ),
            )

    p = problem()
    truth = tuple(
        replace(span, start=span.start * 300, end=span.end * 300) for span in p.zones[0].truth
    )
    p = replace(
        p,
        end=72000,
        rows={"r": ColumnRows()},
        radars={"r": replace(p.radars["r"], channels=tuple(range(18)))},
        zones=(replace(p.zones[0], truth=truth),),
    )
    plan = ScorePlan("exact", (), 12000, 2)
    pattern = {"r": dict.fromkeys(range(18), 40)}
    current = wire(diagnostics.diagnostics(p, pattern, plan))
    monkeypatch.setattr(diagnostics, "_radar", reference_radar)
    monkeypatch.setattr(diagnostics, "_append_session", reference_append_session)
    monkeypatch.setattr(diagnostics, "_totals", reference_totals)
    assert current == wire(diagnostics.diagnostics(p, pattern, plan))


def test_diagnostic_gate_lists_are_owned_by_each_report():
    p = problem(split=10)
    pattern = {"r": {0: 40}}
    plan = ScorePlan("exact", (), 40, 2)
    first = diagnostics.diagnostics(p, pattern, plan)
    expected = wire(first)
    for row in first["radars"]["r"]["review"]["sessions"]:
        row["gates"].append("changed_by_caller")
    assert wire(diagnostics.diagnostics(p, pattern, plan)) == expected


def test_sample_run_encodes_values_once(monkeypatch):
    from collections import deque

    calls = 0
    original = bytes

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(diagnostics, "bytes", counted, raising=False)
    samples = {s: deque(maxlen=5000) for s in diagnostics.STATES}
    counts = dict.fromkeys(diagnostics.STATES, 0)
    span = problem().zones[0].truth[0]
    diagnostics._samples(samples, counts, 0, 120000, span, (0,), (70,))
    assert counts["present"] == 20000
    assert len(samples["present"]) == 5000
    assert calls == 1


def test_empty_channel_diagnostics_match_original(monkeypatch):
    p = problem()
    p = replace(p, radars={"r": replace(p.radars["r"], channels=(), domains={}, current={})})
    plan = ScorePlan("exact", (), 4, 2)
    current = wire(diagnostics.diagnostics(p, {"r": {}}, plan))
    monkeypatch.setattr(diagnostics, "_radar", reference_radar)
    monkeypatch.setattr(diagnostics, "_append_session", reference_append_session)
    monkeypatch.setattr(diagnostics, "_totals", reference_totals)
    assert current == wire(diagnostics.diagnostics(p, {"r": {}}, plan))


def legacy_fold_rows(folder, block, start, end, first, after):
    for i in range(first, after):
        lo, hi = max(start, block.times[i]), min(end, block.ends[i])
        values = {c: block.columns[c][i] for c in folder.selected}
        if hi > lo and 255 not in values.values():
            folder.row(lo, hi, values)


def legacy_source_evidence(cells, runs, owner, keys):
    from tuner_under_test.calibration import event_quality as eq
    from tuner_under_test.calibration.episodes import Episode
    from tuner_under_test.calibration.quality_evidence import (
        AutomaticEpisode,
        Overlap,
        _intersections,
    )
    from tuner_under_test.const import HISTORY_KEYS
    from tuner_under_test.history import guesses

    parents = dict(zip(runs, eq._auto_parents(runs, owner), strict=True))
    automatic, overlaps = {}, {}
    positions = tuple(HISTORY_KEYS.index(key) for key in keys)
    usable = (
        (a, b, parent if state in eq.LABELS else state)
        for a, b, values, state, parent in cells
        if any(values[position] is not None for position in positions)
    )
    for low, high, human, run in _intersections(usable, runs):
        confidence = guesses.confidence(run[3])
        if not eq.MIN_AUTO_CONFIDENCE <= confidence <= 1:
            continue
        duration = high - low
        if human is None:
            eq._add(automatic, (parents[run], run[4]), confidence * duration, duration)
        elif isinstance(human, Episode):
            eq._add(overlaps, (human, run[4], run[2]), duration * (human.state == run[2]), duration)
    return (
        tuple(
            AutomaticEpisode(ep, basis, mass / duration, owner)
            for (ep, basis), (mass, duration) in automatic.items()
        ),
        tuple(Overlap(*key, mass, duration) for key, (mass, duration) in overlaps.items()),
    )


def test_burst_and_offset_rows_fold_without_scalar_calls(monkeypatch):
    from test_hybrid_digest import block
    from tuner_under_test.engine.hybrid import digest

    rows = [(0.25 + i / 8, 0.25 + (i + 1) / 8, [i % 101] * 18) for i in range(24000)]
    source = block(rows)
    with monkeypatch.context() as previous:
        previous.setattr(digest, "_fold_rows", legacy_fold_rows)
        expected = digest.fold_day([source], 0, 3006, (0, 1))
    calls = []
    original = digest._Folder.row

    def counted(folder, *args):
        calls.append(1)
        return original(folder, *args)

    monkeypatch.setattr(digest._Folder, "row", counted)
    actual = digest.fold_day([source], 0, 3006, (0, 1))
    assert actual.channels == expected.channels
    assert actual.cov == expected.cov
    assert dict(actual.partial) == dict(expected.partial)
    assert len(calls) <= 2


def test_source_intersections_follow_runs_and_labels(monkeypatch):
    from tuner_under_test.calibration import event_quality as eq
    from tuner_under_test.calibration.episodes import Episode
    from tuner_under_test.history import guesses

    parent = Episode(10.125, 2000.375, "present", ("r",))
    cells = [
        (
            i / 8,
            (i + 1) / 8,
            (20,) * 18,
            "present" if 81 <= i < 16003 else None,
            parent if 81 <= i < 16003 else None,
        )
        for i in range(24000)
    ]
    runs = (
        (0.0, 1000.375, "present", guesses.level(90), 1),
        (1000.375, 3000.0, "not_present", guesses.level(80), 2),
    )
    expected = legacy_source_evidence(cells, runs, ("r",), ["g0_move"])
    original, calls = guesses.confidence, []

    def counted(value):
        calls.append(1)
        return original(value)

    monkeypatch.setattr(guesses, "confidence", counted)
    assert wire(eq._source_evidence(iter(cells), runs, ("r",), ["g0_move"])) == wire(expected)
    assert len(calls) <= len(runs)


def test_quantized_zero_fingerprint_formats_parent_once():
    from hashlib import sha256

    from tuner_under_test.calibration import event_quality as eq
    from tuner_under_test.calibration.episodes import Episode

    class Parent(Episode):
        representations = 0

        def __repr__(self):
            type(self).representations += 1
            return super().__repr__()

    parent = Parent(0, 100, "present", ("r",))
    cells = [(i, i + 1, (0,) * 18, "present", parent) for i in range(100)]
    expected = sha256(b"[" + b"".join(repr(c).encode() + b"\0" for c in cells) + b"]")
    Parent.representations = 0
    actual = sha256()
    list(eq._observed(cells, eq._Human(["g0_move"]), actual, integer_values=True))
    assert actual.digest() == expected.digest()
    assert Parent.representations == 1


def test_finished_physical_cache_ignores_later_cutoff_and_labels(tmp_path, monkeypatch):
    from tuner_under_test.history import fit_days

    view = worker_view(tmp_path, "r", [10, 20, 30, 40])
    entry = next(iter(view._days.entries.values()))
    entry["persistent"] = True
    date = view._days.days("r")[0]
    view._history_until = 2 * 86400
    original = fit_days._cached_physical
    calls = []

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(fit_days, "_cached_physical", counted)
    first = fit_days.FittingDays(view).physical("r", date, (0,))
    expected = first.window(0, 86400)
    first.close()
    before = len(calls)
    view._history_until = 3 * 86400
    view.data["devices"]["r"]["history_labels"] = [{"start": 0, "end": 20, "state": "present"}]
    second = fit_days.FittingDays(view).physical("r", date, (0,))
    assert second.window(0, 86400) == expected
    second.close()
    assert len(calls) == before


def test_native_cells_match_original_without_span_allocations(tmp_path, monkeypatch):
    from tuner_under_test.calibration import event_quality as eq
    from tuner_under_test.engine.hybrid import quality
    from tuner_under_test.history.fit_rows import CapturedRowSource
    from tuner_under_test.history.label_lookup import index_reader

    view = worker_view(tmp_path, "r", [0, 20, 255, 40, 0, 60])
    source = CapturedRowSource(view, "r", (0,))
    reader = index_reader(
        ([0.0, 3.125, 12, 19.5, 30], ["present", None, "unknown", "not_present", "present"])
    )
    bounds = (1.0, 35.5)
    expected = list(eq._cells(quality._physical(source, *bounds), reader, ("r",), *bounds, *bounds))
    original, calls = quality.spans.Span, []

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(quality.spans, "Span", counted)
    assert wire(list(quality._native_cells(source, reader, ("r",), *bounds))) == wire(expected)
    assert calls == []


def test_physical_builder_coalesces_without_row_vector_calls(monkeypatch):
    from test_hybrid_digest import block
    from tuner_under_test.engine.hybrid import physical_index

    rows = [(i / 8, (i + 1) / 8, [i // 7 % 101] * 18) for i in range(24000)]
    original, calls = physical_index._fresh, []

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(physical_index, "_fresh", counted)
    result = physical_index.physical_rows([block(rows)], 0, 3000, (0, 1))
    assert result.count == ceil(24000 / 7)
    assert len(calls) < 10


@pytest.mark.parametrize("epoch", [0, 1790000000])
def test_source_interval_bytes_match_seeded_scalar_oracle(epoch):
    import random

    from tuner_under_test.calibration import event_quality as eq
    from tuner_under_test.calibration.episodes import Episode
    from tuner_under_test.history import guesses

    rng = random.Random(137)
    parents = [
        Episode(
            epoch + i * 100, epoch + (i + 1) * 100, "present" if i % 2 else "not_present", ("r",)
        )
        for i in range(12)
    ]
    cells, t = [], epoch - 1.0
    for i in range(9000):
        lo = t + rng.choice((0, 0.001, 0.125))
        t = lo + rng.choice((0.001, 0.123, 0.25, 1))
        parent = parents[i // 750] if i % 7 else None
        state = parent.state if parent is not None else rng.choice((None, "unknown"))
        cells.append(
            (lo, t, tuple(rng.choice((None, 0, 20, 100)) for _ in range(18)), state, parent)
        )
    runs = tuple(
        (epoch + a, epoch + b, state, guesses.level(confidence), i % 3)
        for i, (a, b, state, confidence) in enumerate(
            [
                (-1, 10.123, "present", 90),
                (10.123, 1300.99, "present", 80),
                (1350.1, 2900, "not_present", 100),
                (2900, 4000, "present", 5),
            ]
        )
    )
    from tuner_under_test.const import HISTORY_KEYS

    keys = ["g0_move", HISTORY_KEYS[17]]
    expected = legacy_source_evidence(cells, runs, ("r",), keys)
    assert expected[0] and expected[1]
    assert wire(eq._source_evidence(iter(cells), runs, ("r",), keys)) == wire(expected)


def test_quality_thresholds_and_full_report_bytes_match_original_collectors(tmp_path, monkeypatch):
    from test_hybrid_facade import device_fit
    from tuner_under_test.calibration import event_quality as eq
    from tuner_under_test.engine.hybrid import adapters, digest, quality
    from tuner_under_test.history import dayfile
    from tuner_under_test.worker import history

    values, labels = alternating(40)
    values[100:120] = [0] * 20
    view = worker_view(tmp_path, "r", values, labels)
    entry = next(iter(view._days.entries.values()))
    from pathlib import Path

    path = Path(entry["path"])
    day = dayfile.decode_day(path.read_bytes())
    day.guesses = [(0, 2400.125, "present", 18, 1), (2400.125, 4800, "not_present", 16, 2)]
    path.write_bytes(dayfile.encode_day(day))
    entry["identity"] = history._identity(path.stat())
    contexts = []
    original = adapters.collect_quality

    def recorded(*args, **kwargs):
        result = original(*args, **kwargs)
        contexts.append(wire(result))
        return result

    monkeypatch.setattr(adapters, "collect_quality", recorded)
    current = wire(device_fit(view, weight=0.5))
    current_contexts = list(contexts)
    assert current_contexts
    contexts.clear()
    monkeypatch.setattr(digest, "_fold_rows", legacy_fold_rows)
    monkeypatch.setattr(eq, "_source_evidence", legacy_source_evidence)
    monkeypatch.setattr(
        quality,
        "_native_cells",
        lambda source, reader, owner, start, end: eq._cells(
            quality._physical(source, start, end), reader, owner, start, end, start, end
        ),
    )
    assert wire(device_fit(view, weight=0.5)) == current
    assert contexts == current_contexts


def test_source_interval_accumulation_uses_bulk_arithmetic():
    import sys
    from array import array

    from tuner_under_test.calibration import event_quality as eq

    times = array("d", (i / 1000 for i in range(10000)))
    ends = array("d", ((i + 1) / 1000 for i in range(10000)))
    expected = {"r": (0.123, 1.234)}
    for a, b in zip(times, ends, strict=True):
        eq._add(expected, "r", 0.85 * (b - a), b - a)
    actual, steps = {"r": (0.123, 1.234)}, []

    def trace(frame, event, _argument):
        if frame.f_code is eq._add_interval.__code__ and event == "line":
            steps.append(1)
        return trace

    sys.settrace(trace)
    try:
        eq._add_interval(actual, "r", 0.85, 0, 10, times, ends)
    finally:
        sys.settrace(None)
    assert wire(actual) == wire(expected)
    assert len(steps) < 100


def test_native_cells_reuse_repeated_quantized_vectors(tmp_path):
    from tuner_under_test.engine.hybrid import quality
    from tuner_under_test.history.fit_rows import CapturedRowSource
    from tuner_under_test.history.label_lookup import index_reader

    view = worker_view(tmp_path, "r", [10, 20, 30] * 1000)
    source = CapturedRowSource(view, "r", (0,))
    reader = index_reader(([], []))
    cells = list(quality._native_cells(source, reader, ("r",), 0, 18000))
    assert len(cells) == 3000
    assert {cell[2][0] for cell in cells} == {10, 20, 30}
    assert len({id(cell[2]) for cell in cells}) == 3


def test_source_intervals_reuse_identical_label_objects():
    from tuner_under_test.calibration import event_quality as eq
    from tuner_under_test.calibration.episodes import Episode

    class Parent(Episode):
        comparisons = 0

        def __eq__(self, other):
            type(self).comparisons += 1
            return super().__eq__(other)

    parent = Parent(0, 3000, "present", ("r",))
    cells = [(i, i + 1, (20,) * 18, "present", parent) for i in range(3000)]
    intervals = list(eq._usable_intervals(cells, (0,)))
    assert len(intervals) == 1
    assert intervals[0][:2] == (0, 3000)
    assert Parent.comparisons <= 2


def test_bulk_fold_excludes_zero_and_reversed_duration_rows():
    from test_hybrid_digest import block
    from tuner_under_test.engine.hybrid import digest

    source = block([(0, 1, [20] * 18), (1, 1, [100] * 18), (1, 0.5, [99] * 18), (1, 6, [30] * 18)])
    result = digest.fold_day([source], 0, 6, (0,))
    assert result.channels[0] == bytes([30])
    assert result.cov == b"\x01"
