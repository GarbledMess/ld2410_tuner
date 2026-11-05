"""Exact streamed attribution with work proportional to truth runs."""

import json

import pytest
import test_tuner  # noqa: F401
from attr_synthetic import Source, fixture
from tuner_under_test.engine.hybrid import adapters, quality
from tuner_under_test.history import fit_days, fit_rows, guesses  # noqa: F401
from tuner_under_test.rooms import evidence, truth


@pytest.fixture(autouse=True)
def captured(monkeypatch):
    monkeypatch.setattr(fit_rows, "CapturedRowSource", Source)
    monkeypatch.setattr(guesses, "of", lambda *_args: guesses.EMPTY)


def serialized(value):
    return json.dumps(value, separators=(",", ":")).encode()


def test_truth_work_depends_on_label_runs(monkeypatch):
    view = fixture()
    calls, original = [], truth.room_truth

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(truth, "room_truth", counted)
    result = evidence._stream_attribution(
        view, "r", view.data["room_snapshot"], {}, 0, view._history_until
    )
    assert result["found"]["room"]
    assert len(calls) <= 2 * (248 + 1)


def test_episode_construction_depends_on_episodes(monkeypatch):
    view = fixture()
    calls, original = [], evidence._StreamEpisode.__init__

    def counted(self, *args):
        calls.append(1)
        original(self, *args)

    monkeypatch.setattr(evidence._StreamEpisode, "__init__", counted)
    result = evidence._stream_attribution(
        view, "r", view.data["room_snapshot"], {}, 0, view._history_until
    )
    assert len(calls) == len(result["found"]["room"])


@pytest.mark.parametrize("human", [True, False])
def test_attribution_bytes_equal_frozen_stream(human):
    import attr_reference

    view = fixture(seconds=3600)
    others = None if human else {}
    args = view, "r", view.data["room_snapshot"], others, 0, view._history_until
    expected = attr_reference._stream_attribution(*args)
    actual = evidence._stream_attribution(*args)
    assert serialized(actual["found"]) == serialized(expected["found"])
    for t in (0, 1, 300, 3599, 3600):
        assert actual["truths"]["room"](t) == expected["truths"]["room"](t)


@pytest.mark.parametrize("cutoff", [1800.25, 3600])
@pytest.mark.parametrize("human", [True, False])
def test_overlaps_unknowns_estimates_and_cutoff_equal_reference(human, cutoff):
    import attr_reference

    view = fixture(seconds=3600)
    saved = view.data["room_snapshot"]
    saved["members"]["r"]["history_labels"] = [
        {"start": 12.5, "end": 350.25, "state": "not_present"},
        {"start": 100.5, "end": 175.75, "state": "unknown"},
        {"start": 1200.5, "end": 1300.25, "state": "present"},
    ]
    saved["room_labels"]["room"] += [
        {"start": 20.5, "end": 200.25, "state": "present", "source": "manual"},
        {"start": 175.5, "end": 180.25, "state": "unknown", "source": "manual"},
    ]
    recorded = guesses.Guesses([(0, 1500.5, "present", 20, guesses.EXTERNAL)])
    others = None if human else dict.fromkeys(("r", "s"), recorded)
    args = view, "r", saved, others, 0.25, cutoff
    assert serialized(evidence._stream_attribution(*args)["found"]) == serialized(
        attr_reference._stream_attribution(*args)["found"]
    )


def test_final_learn_report_bytes_equal_frozen_stream(tmp_path, monkeypatch):
    import attr_reference
    from test_hybrid_facade import alternating, worker_view
    from tuner_under_test.engine.hybrid import facade

    values, labels = alternating(10)
    view = worker_view(tmp_path, "r", values)
    view.data["devices"]["s"] = {"history_labels": []}
    view.data["room_snapshot"] = {
        "device_id": "r",
        "own": ["room"],
        "groups": {"room": {"device_ids": ["r", "s"]}},
        "members": view.data["devices"],
        "room_labels": {"room": [{**item, "source": "manual"} for item in labels]},
    }
    # This case uses actual captured day files through the worker reader.
    monkeypatch.undo()
    monkeypatch.setattr(adapters, "CapturedRowSource", fit_rows.CapturedRowSource)
    monkeypatch.setattr(quality, "CapturedRowSource", fit_rows.CapturedRowSource)
    optimized = facade.fit(view, "r", ["g0_move"], {"g0_move": 90}, None, 0.5)
    monkeypatch.setattr(evidence, "_stream_attribution", attr_reference._stream_attribution)
    monkeypatch.setattr(quality, "_physical", attr_reference._physical)
    baseline = facade.fit(view, "r", ["g0_move"], {"g0_move": 90}, None, 0.5)
    assert serialized(optimized) == serialized(baseline)


def test_physical_columns_are_transposed_without_per_cell_indexing():
    from array import array
    from types import SimpleNamespace

    from tuner_under_test.engine.hybrid.types import RowBlock

    calls = []

    class Column(bytes):
        def __getitem__(self, index):
            calls.append(index)
            return super().__getitem__(index)

    block = RowBlock(array("d", [0, 6, 12]), array("d", [6, 12, 18]), (Column([0, 100, 255]),) * 18)
    source = SimpleNamespace(view=SimpleNamespace(), blocks=lambda *_: [block])
    rows = list(quality._physical(source, 0, 18))
    assert [row.values for row in rows] == [(0,) * 18, (100,) * 18, (None,) * 18]
    assert len(calls) <= 18


def test_attribution_consumes_columns_without_expanding_physical_cells(monkeypatch):
    calls, original = [], evidence._stream_cells

    def counted(*args):
        for cell in original(*args):
            calls.append(1)
            yield cell

    monkeypatch.setattr(evidence, "_stream_cells", counted)
    view = fixture(seconds=3600)
    result = evidence._stream_attribution(view, "r", view.data["room_snapshot"], {}, 0, 3600)
    assert result["found"]["room"]
    assert not calls


def test_column_baselines_and_gap_sustain_equal_reference(monkeypatch):
    from array import array

    import attr_reference
    from tuner_under_test.engine.hybrid.types import RowBlock

    class Blocks:
        def __init__(self, view, *_args):
            self.view = view

        def blocks(self, start, end):
            for offset in (0, 160, 320, 480):
                times = [offset + i * 6 for i in range(20)]
                times[10:] = [t + 0.25 for t in times[10:]]
                selected = [t for t in times if t < end and t + 6 > start]
                if not selected:
                    continue
                columns = tuple(
                    bytes(255 if i == 5 else 60 if t < 320 else 0 for i, t in enumerate(selected))
                    if gate < 2
                    else bytes([255]) * len(selected)
                    for gate in range(18)
                )
                yield RowBlock(
                    array("d", (max(start, t) for t in selected)),
                    array("d", (min(end, t + 6) for t in selected)),
                    columns,
                )

    monkeypatch.setattr(fit_rows, "CapturedRowSource", Blocks)
    view = fixture(seconds=600)
    saved = view.data["room_snapshot"]
    saved["room_labels"]["room"] = [
        {"start": 0.1, "end": 320.1, "state": "present", "source": "manual"},
        {"start": 320.1, "end": 600, "state": "not_present", "source": "manual"},
    ]
    args = view, "r", saved, None, 0.1, 600
    assert serialized(evidence._stream_attribution(*args)["found"]) == serialized(
        attr_reference._stream_attribution(*args)["found"]
    )

    def own(_t):
        return None

    reads = {"room": lambda t: ("not_present", "human", 1) if t >= 320.1 else None}
    args = view, "r", [0.1, 320.1, 600], reads, own, 0.1, 600
    old = attr_reference._stream_baseline(*args)
    new = evidence._stream_baseline(*args)
    assert {g: q.cumulative for g, q in old.items()} == {g: q.cumulative for g, q in new.items()}
    assert old


def test_sustain_reuses_threshold_translation():
    from array import array

    from tuner_under_test.engine.hybrid.types import RowBlock
    from tuner_under_test.rooms import stream_attribution

    calls = []

    class Ceiling(int):
        def __lt__(self, value):
            calls.append(value)
            return super().__lt__(value)

    block = RowBlock(array("d", [0, 6]), array("d", [6, 12]), (bytes([80, 80]),))
    for _ in range(2):
        stream_attribution._sustain(block, 0, Ceiling(37), None)
    assert len(calls) <= 256


def test_absent_human_evidence_does_not_read_physical_history(monkeypatch):
    view = fixture(seconds=3600)
    calls, original = [], Source.blocks

    def counted(self, *args):
        calls.append(1)
        yield from original(self, *args)

    monkeypatch.setattr(Source, "blocks", counted)
    result = evidence._stream_attribution(view, "r", view.data["room_snapshot"], None, 0, 3600)
    assert result["found"] == {"room": []}
    assert not calls


@pytest.mark.parametrize("chunk", [1, 17, 137])
def test_fragmented_columns_equal_reference_at_every_boundary(monkeypatch, chunk):
    from array import array
    from math import inf, nextafter
    from random import Random

    import attr_reference
    from tuner_under_test.engine.hybrid.types import RowBlock
    from tuner_under_test.rooms import attribution

    rng = Random(183)
    times, ends, columns = [], [], [bytearray() for _ in range(18)]
    clock = 0.125
    for i in range(500):
        clock += 0.25 if rng.randrange(20) == 0 else 0
        times.append(clock)
        clock += rng.choice((0.5, 6, 6, 6, 20.25))
        ends.append(clock)
        for gate, column in enumerate(columns):
            value = (65 if i // 40 % 2 else 1) + gate % 3
            column.append(255 if rng.randrange(12) == 0 else value)

    class Blocks:
        def __init__(self, view, *_args):
            self.view = view

        def blocks(self, start, end):
            for first in range(0, len(times), chunk):
                chosen = [
                    i
                    for i in range(first, min(first + chunk, len(times)))
                    if times[i] < end and ends[i] > start
                ]
                if chosen:
                    yield RowBlock(
                        array("d", (max(start, times[i]) for i in chosen)),
                        array("d", (min(end, ends[i]) for i in chosen)),
                        tuple(bytes(col[i] for i in chosen) for col in columns),
                    )

    monkeypatch.setattr(fit_rows, "CapturedRowSource", Blocks)
    view = fixture(seconds=3600)
    saved = view.data["room_snapshot"]
    saved["own"].append("zone")
    saved["groups"]["zone"] = {"device_ids": ["r"], "kind": "zone"}
    saved["room_labels"]["zone"] = [
        {"start": 120.375, "end": 810.25, "state": "present", "source": "manual"}
    ]
    saved["members"]["r"]["history_labels"] = [
        {"start": 0, "end": 120.25, "state": "not_present"},
        {"start": 250.125, "end": 275.875, "state": "unknown"},
    ]
    others = dict.fromkeys(
        ("r", "s"),
        guesses.Guesses(
            [
                (0, 1000.5, "present", 20, guesses.EXTERNAL),
                (1100.25, 4000, "not_present", 18, guesses.EXTERNAL),
            ]
        ),
    )
    args = view, "r", saved, others, 0.125, clock
    old = attr_reference._stream_attribution(*args)
    new = evidence._stream_attribution(*args)
    assert serialized(new["found"]) == serialized(old["found"])
    targets = [attribution._targets("r", item, saved["own"], automatic=True) for item in (old, new)]
    points = {t for context in new["contexts"].values() for t in truth.edges(context)}
    points.update(t for group in old["found"].values() for span, _ in group for t in span)
    for t in points:
        for at in (nextafter(t, -inf), t, nextafter(t, inf)):
            assert targets[0](at) == targets[1](at)

    def own(_t):
        return "not_present"

    args = view, "r", sorted(points), {}, own, 0.125, clock
    old_bins = attr_reference._stream_baseline(*args)
    new_bins = evidence._stream_baseline(*args)
    assert {g: q.cumulative for g, q in old_bins.items()} == {
        g: q.cumulative for g, q in new_bins.items()
    }
