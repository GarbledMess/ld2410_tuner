"""Synthetic fragmented-label parity and deterministic work bounds."""

import json
import sqlite3
from dataclasses import asdict, is_dataclass, replace
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
            return asdict(item)
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
