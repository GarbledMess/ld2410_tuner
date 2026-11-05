"""Synthetic equivalence and work bounds for cold evidence collection."""

import random
from bisect import bisect_left, bisect_right
from hashlib import sha256

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.calibration import event_quality
from tuner_under_test.calibration.episodes import Episode
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history.spans import Span


def test_cells_reuse_episode_between_label_edges():
    def reader(timestamp):
        return "present" if timestamp < 12 else "not_present"

    reader.edges = [0, 12, 24]
    physical = [Span(i, i + 1, (i,) * 18, "event") for i in range(24)]
    cells = list(event_quality._cells(physical, reader, ("r",), 0, 24, 0, 24))
    expected = [
        (i, i + 1, (i,) * 18, reader(i), Episode(0, 12, "present", ("r",)))
        if i < 12
        else (i, i + 1, (i,) * 18, reader(i), Episode(12, 24, "not_present", ("r",)))
        for i in range(24)
    ]
    assert cells == expected
    assert len({id(cell[4]) for cell in cells}) == 2


def test_source_evidence_resolves_gate_positions_once(monkeypatch):
    class Keys(list):
        lookups = 0

        def index(self, value):
            self.lookups += 1
            return super().index(value)

    keys = Keys(HISTORY_KEYS)
    monkeypatch.setattr(event_quality, "HISTORY_KEYS", keys)
    cells = [(i, i + 1, (None,) * 17 + (20,), None, None) for i in range(100)]
    selected = [HISTORY_KEYS[0], HISTORY_KEYS[17]]
    assert event_quality._source_evidence(cells, (), ("r",), selected) == ((), ())
    assert keys.lookups <= len(selected)


def test_label_reader_is_called_once_between_edges():
    calls = []

    def reader(timestamp):
        calls.append(timestamp)
        return "present" if timestamp < 12 else "not_present"

    reader.edges = [0, 12, 24]
    physical = [Span(i, i + 1, (i,) * 18, "event") for i in range(24)]
    cells = list(event_quality._cells(physical, reader, ("r",), 0, 24, 0, 24))
    assert [cell[3] for cell in cells] == ["present"] * 12 + ["not_present"] * 12
    assert len(calls) == 2


def test_observation_fingerprint_reuses_repeated_value_and_episode_text():
    class Parent(Episode):
        representations = 0

        def __repr__(self):
            type(self).representations += 1
            return super().__repr__()

    parent = Parent(0, 100, "present", ("r",))
    cells = [(i / 8, (i + 1) / 8, (20,) * 18, "present", parent) for i in range(100)]
    expected = sha256(b"[" + b"".join(repr(cell).encode() + b"\0" for cell in cells) + b"]")
    Parent.representations = 0
    accumulator, actual = event_quality._Human([HISTORY_KEYS[0]]), sha256()
    assert list(event_quality._observed(iter(cells), accumulator, actual)) == cells
    assert actual.digest() == expected.digest()
    assert accumulator.result()[0] == (parent,)
    assert Parent.representations == 1


def test_no_automatic_runs_drain_observations_without_reading_values():
    class Values(tuple):
        reads = 0

        def __getitem__(self, index):
            type(self).reads += 1
            return super().__getitem__(index)

    values = Values((20,) * 18)
    cells = [(i, i + 1, values, None, None) for i in range(100)]
    assert event_quality._source_evidence(iter(cells), (), ("r",), HISTORY_KEYS) == ((), ())
    assert Values.reads == 0


def test_fingerprint_distinguishes_equal_numbers_with_different_text():
    parent = Episode(0, 100, "present", ("r",))
    values = (20, 20.0, 0.0, -0.0, 0, 20, 20.0)
    cells = [(i, i + 1, (value,) * 18, "present", parent) for i, value in enumerate(values)]
    expected = sha256(b"[" + b"".join(repr(cell).encode() + b"\0" for cell in cells) + b"]")
    actual = sha256()
    list(event_quality._observed(cells, event_quality._Human([HISTORY_KEYS[0]]), actual))
    assert actual.digest() == expected.digest()


def _scalar_cells(physical, reader, owner, start, end, first, last):
    for cell in physical:
        low, high = max(start, cell.start), min(end, cell.end)
        cuts = [
            low,
            *reader.edges[bisect_right(reader.edges, low) : bisect_left(reader.edges, high)],
            high,
        ]
        for a, b in zip(cuts, cuts[1:], strict=False):
            if b > a:
                state = reader(a)
                yield (
                    a,
                    b,
                    cell.values,
                    state,
                    event_quality._parent(reader, a, state, owner, first, last),
                )


@pytest.mark.parametrize("epoch", [0, 1790000000])
def test_quality_cells_and_fingerprints_match_scalar_oracle(epoch):
    from tuner_under_test.history.label_lookup import index_reader

    rng = random.Random(2352)
    edges = [epoch + value for value in (0, 4.125, 8, 23.5, 70, 120, 170, 250)]
    states = (None, "present", "unknown", "not_present", "present", None, "present", "not_present")
    reader = index_reader((edges, states))
    physical = []
    timestamp = epoch - 2
    for _ in range(300):
        timestamp += rng.choice((0, 0.125, 1))
        stop = timestamp + rng.choice((0.125, 0.5, 1, 6))
        values = tuple(rng.choice((None, 0, 0.0, -0.0, 20, 20.1, 100)) for _ in HISTORY_KEYS)
        physical.append(Span(timestamp, stop, values, "event"))
        timestamp = stop
    bounds = (epoch + 1.25, epoch + 270.75, epoch - 2, epoch + 400)
    expected = list(_scalar_cells(physical, reader, ("r",), *bounds))
    actual = list(event_quality._cells(physical, reader, ("r",), *bounds))
    assert actual == expected
    scalar = event_quality._human(expected, HISTORY_KEYS)
    human, digest = event_quality._Human(HISTORY_KEYS), sha256()
    list(event_quality._observed(actual, human, digest))
    assert human.result() == scalar
    assert (
        digest.digest()
        == sha256(b"[" + b"".join(repr(cell).encode() + b"\0" for cell in expected) + b"]").digest()
    )


def test_fingerprint_formats_parent_once_for_changing_readings():
    class Parent(Episode):
        representations = 0

        def __repr__(self):
            type(self).representations += 1
            return super().__repr__()

    parent = Parent(0, 100, "present", ("r",))
    cells = [(i, i + 1, (i + 1,) * 18, "present", parent) for i in range(100)]
    expected = sha256(b"[" + b"".join(repr(cell).encode() + b"\0" for cell in cells) + b"]")
    Parent.representations = 0
    actual = sha256()
    list(event_quality._observed(cells, event_quality._Human([HISTORY_KEYS[0]]), actual))
    assert actual.digest() == expected.digest()
    assert Parent.representations == 1


def test_fingerprint_hashes_observations_in_bounded_batches():
    class Digest:
        def __init__(self):
            self.value, self.updates = sha256(), 0

        def update(self, value):
            self.updates += 1
            self.value.update(value)

    cells = [(i, i + 1, (20,) * 18, None, None) for i in range(10000)]
    expected = sha256(b"[" + b"".join(repr(cell).encode() + b"\0" for cell in cells) + b"]")
    actual = Digest()
    list(event_quality._observed(cells, event_quality._Human([HISTORY_KEYS[0]]), actual))
    assert actual.value.digest() == expected.digest()
    assert actual.updates < 100


def test_cells_inside_label_interval_reuse_resolved_state(monkeypatch):
    def reader(timestamp):
        return "present" if timestamp < 12 else "not_present"

    reader.edges = [0, 12, 24]
    physical = [Span(i, i + 1, (i,) * 18, "event") for i in range(24)]
    expected = list(_scalar_cells(physical, reader, ("r",), 0, 24, 0, 24))
    original, calls = event_quality._LabelCells.at, []

    def counted(labels, timestamp):
        calls.append(timestamp)
        return original(labels, timestamp)

    monkeypatch.setattr(event_quality._LabelCells, "at", counted)
    actual = list(event_quality._cells(physical, reader, ("r",), 0, 24, 0, 24))
    assert actual == expected
    assert len(calls) <= 3


@pytest.mark.parametrize("start,end", [(0, 24.0), (-0.0, 24), (1.0, 23), (-1, 30)])
def test_label_fast_path_preserves_clipping_number_representations(start, end):
    def reader(timestamp):
        return "present" if timestamp < 12 else "not_present"

    reader.edges = [0.0, 12.0, 24.0]
    physical = [Span(-1, -1, (20,) * 18, "event")]
    physical += [Span(i, i + 1, (20,) * 18, "event") for i in range(-1, 26)]
    physical += [Span(26, 25, (20,) * 18, "event")]
    expected = list(_scalar_cells(physical, reader, ("r",), start, end, -1.0, 30.0))
    actual = list(event_quality._cells(physical, reader, ("r",), start, end, -1.0, 30.0))
    assert repr(actual) == repr(expected)
