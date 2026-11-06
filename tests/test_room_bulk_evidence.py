"""Room evidence read as whole ranges: every value the row walk gave, without the row walk."""

import random
from dataclasses import fields
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from room_speed_fixture import build, request
from test_evidence_quality import view
from tuner_under_test.calibration import event_quality
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.engine.hybrid import quality
from tuner_under_test.history import guesses, spans, worker_snapshot
from tuner_under_test.history.label_lookup import _history_label_reader
from tuner_under_test.rooms import adaptive
from tuner_under_test.worker import calculations
from tuner_under_test.worker.manifest import expand


def assert_same_context(new, old):
    """Byte for byte: ``repr`` tells 0.1 + 0.2 from 0.3, unlike any tolerance."""
    for field in fields(old):
        if field.name != "fingerprint":
            left, right = getattr(new, field.name), getattr(old, field.name)
            assert repr(left) == repr(right), field.name
    assert len(new.fingerprint) == len(old.fingerprint)


def _stretches(rng):
    found = []
    for low, high in ((0, 50), (70, 110), (130, 180)):
        at, events = low + 0.125, []
        while at < high:
            events.append((at, tuple(rng.choice((None, 0, 10.1, 30, 99.9)) for _ in HISTORY_KEYS)))
            at += rng.choice((0.0625, 0.375, 1.0, 2.5))
        found.append((events, high))
    return found


LABELS = [
    {"start": 0, "end": 30, "state": "present"},
    {"start": 30, "end": 90, "state": "not_present"},
    {"start": 100, "end": 115, "state": "unknown"},
    {"start": 120.0625, "end": 121, "state": "present"},
    {"start": 122.5, "end": 200, "state": "not_present"},
]
RUNS = [
    (0, 45, "present", 20, guesses.EXTERNAL),
    (45, 120, "not_present", 14, guesses.EXTERNAL),
    (120, 180, "present", 1, guesses.EXTERNAL),
]
INDEPENDENT = _history_label_reader(
    {"history_labels": [{"start": 15, "end": 170, "state": "present"}]}
)


@pytest.mark.parametrize("seed", [5, 71, 839])
@pytest.mark.parametrize(
    "keys",
    [
        [HISTORY_KEYS[0]],
        [HISTORY_KEYS[17], HISTORY_KEYS[3]],
        list(HISTORY_KEYS[:5]),
        list(HISTORY_KEYS),
        [],
    ],
)
@pytest.mark.parametrize(
    "window",
    [(-1000.0, 1000.0), (2.375, 177.625), (0, 180), (50, 60), (55.5, 135.25), (300, 400)],
)
def test_bulk_context_matches_the_row_walk(seed, keys, window):
    source = view(_stretches(random.Random(seed)), LABELS, RUNS)
    args = (source, "any-name", keys, *window, INDEPENDENT, None)
    assert_same_context(quality.collect_room_context(*args), event_quality.collect(*args))


def test_bulk_context_without_runs_labels_or_independent_truth():
    rng = random.Random(1)
    for labels, runs in (([], RUNS), (LABELS, ()), ([], ())):
        source = view(_stretches(rng), labels, runs)
        args = (source, "any-name", [HISTORY_KEYS[1]], 0, 180, None, None)
        assert_same_context(quality.collect_room_context(*args), event_quality.collect(*args))


@pytest.mark.parametrize("seed", [17, 18])
def test_bulk_context_keeps_every_limit_of_a_view(seed):
    source = view(_stretches(random.Random(seed)), LABELS, RUNS)
    source._physical_history_start = 2.25
    source._attribution_until = 150.5
    source._learning_windows = {"any-name": [(3.75, 45.5), (72.5, 140)]}
    args = (source, "any-name", list(HISTORY_KEYS[:3]), 0, 170, INDEPENDENT, None)
    assert_same_context(quality.collect_room_context(*args), event_quality.collect(*args))


def test_bulk_context_owner_and_fingerprint_inputs():
    source = view(_stretches(random.Random(3)), LABELS, RUNS)
    args = (source, "any-name", [HISTORY_KEYS[1]], 0, 180)
    plain = quality.collect_room_context(*args, None, None)
    assert_same_context(
        quality.collect_room_context(*args, None, ("room", "x")),
        event_quality.collect(*args, None, ("room", "x")),
    )
    assert plain.fingerprint == quality.collect_room_context(*args, None, None).fingerprint
    assert plain.fingerprint != quality.collect_room_context(*args, INDEPENDENT, None).fingerprint
    other = view(_stretches(random.Random(4)), LABELS, RUNS)
    args = (other, "any-name", [HISTORY_KEYS[1]], 0, 180)
    assert plain.fingerprint != quality.collect_room_context(*args, None, None).fingerprint


def test_overlapping_rows_are_refused_as_the_row_walk_refuses_them():
    source = view(_stretches(random.Random(3)), LABELS, RUNS)
    blocks = quality.CapturedRowSource(source, "any-name", (0,)).blocks(0, 180)
    block = next(blocks)
    block.times[3] = block.times[2]
    with pytest.raises(ValueError, match="overlapping coverage"):
        list(quality._checked_rows([block]))


@pytest.fixture(scope="module")
def room(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch_:
        patch_.setattr(worker_snapshot, "_executor_only", lambda: None)
        root = tmp_path_factory.mktemp("room")
        fixture = build(root, days=4, open_seconds=3600, sensor_days=1)
        job = request(fixture, root / "job", window_days=3, attribution_days=2)
        expanded = expand(job)
        shared = calculations.JobHistory()
        views = {
            key: calculations.restore(value, shared) for key, value in expanded["views"].items()
        }
        parameters = calculations.codec.decode(job["parameters"])
        members = {
            group: calculations._members(items, views)
            for group, items in parameters["members"].items()
        }
        yield members, parameters


def test_real_shaped_room_contexts_match_the_row_walk(room):
    members, parameters = room
    start, end, rooms = parameters["start"], parameters["end"], parameters["rooms"]
    new = adaptive.collect_contexts(members, start, end, rooms)
    with patch.object(adaptive, "collect", wraps=_legacy) as legacy:
        old = adaptive.collect_contexts(members, start, end, rooms)
    assert legacy.call_count == 2
    assert set(new) == set(old) == {"radar_a", "radar_b"}
    for key in new:
        assert old[key].automatic_episodes
        assert old[key].overlaps
        assert old[key].episodes
        assert_same_context(new[key], old[key])


def _legacy(view_, key, keys, *, start, end, human_reader):
    return event_quality.collect(view_, key, keys, start, end, human_reader, None)


def _forbidden(*_args, **_kwargs):
    raise AssertionError("A room context walked its rows")


def test_room_contexts_build_no_row_tuple(room):
    members, parameters = room
    with (
        patch.object(event_quality, "_observed", _forbidden),
        patch.object(event_quality, "_usable_intervals", _forbidden),
        patch.object(event_quality, "_cells", _forbidden),
        patch.object(spans, "events", _forbidden),
        patch.object(spans, "day_spans", _forbidden),
        patch.object(quality, "_physical", _forbidden),
        patch.object(quality, "_block_vectors", _forbidden),
    ):
        adaptive.collect_contexts(
            members, parameters["start"], parameters["end"], parameters["rooms"]
        )


def test_a_room_context_visits_a_labelled_stretch_once_not_each_row(room):
    members, parameters = room
    calls = []
    original = quality._add_room_rows

    def counted(*args):
        calls.append(args)
        return original(*args)

    with patch.object(quality, "_add_room_rows", counted):
        adaptive.collect_contexts(
            members, parameters["start"], parameters["end"], parameters["rooms"]
        )
    rows = sum(len(call[2]) for call in calls)
    assert rows > 20_000
    assert len(calls) * 20 < rows
