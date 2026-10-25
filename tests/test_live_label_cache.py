"""Synthetic live folds preserve the uncached label precedence and expiry."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import label_lookup, labels
from tuner_under_test.presence import reference_training as training


def device():
    return {
        "label_revision": 0,
        "group_labels": [{"start": 0, "end": 100, "state": "not_present"}],
        "history_labels": [
            {"start": 10, "end": 60, "state": "present"},
            {"start": 20, "end": 30, "state": "unknown"},
        ],
        "training_label_start": 40,
        "training_expires_at": 50,
        "training_state": "not_present",
    }


def test_reader_matches_direct_label_precedence_at_every_boundary():
    item = device()
    reader = label_lookup._history_label_reader(item)
    for timestamp in (-1, 0, 10, 20, 30, 40, 49, 50, 60, 100):
        assert reader(timestamp) == labels._manual_history_state(item, timestamp)


def test_live_fold_exactly_matches_one_fresh_reader_per_call():
    item = device()
    runtime = SimpleNamespace(data={"devices": {"a": item}})
    runtime._manual_history_state = labels._manual_history_state
    expected = deepcopy(item)
    other = SimpleNamespace(data={"devices": {"a": expected}})
    other._manual_history_state = labels._manual_history_state
    values = [20] * 18
    for start in range(0, 100, 5):
        training.observe_spans(runtime, item, [(start, start + 5, values, None, 0, False)])
        other._reference_label_readers = {}
        training.observe_spans(other, expected, [(start, start + 5, values, None, 0, False)])
    assert item == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("label_revision", 1),
        ("training_label_start", 45),
        ("training_expires_at", 48),
        ("training_state", "unknown"),
    ],
)
def test_cached_reader_reuses_index_until_label_inputs_change(field, value):
    item = device()
    cache = {}
    first = label_lookup._history_label_reader(item, cache=cache)
    assert label_lookup._history_label_reader(item, cache=cache) is first
    item[field] = value
    changed = label_lookup._history_label_reader(item, cache=cache)
    assert changed is not first
    for timestamp in (0, 20, 40, 45, 48, 50, 100):
        assert changed(timestamp) == labels._manual_history_state(item, timestamp)


def test_live_edges_refresh_after_room_revision_and_saved_label_edits():
    item = device()
    runtime = SimpleNamespace(
        data={
            "devices": {"a": item},
            "room_labels": {
                "room": [{"start": 12, "end": 14, "state": "not_present"}],
            },
            "room_label_revision": {"room": 1},
        }
    )
    first = training._live_label_edges(runtime, item)
    assert training._live_label_edges(runtime, item) == first
    assert 12 in first and 14 in first
    runtime.data["room_labels"]["room"][0]["end"] = 15
    runtime.data["room_label_revision"]["room"] += 1
    second = training._live_label_edges(runtime, item)
    assert 15 in second and 14 not in second
    item["history_labels"].append({"start": 75, "end": 80, "state": "unknown"})
    item["label_revision"] += 1
    third = training._live_label_edges(runtime, item)
    assert 75 in third and 80 in third


def test_cached_reader_separates_group_mode_and_device_replacement():
    item = device()
    cache = {}
    grouped = label_lookup._history_label_reader(item, cache=cache)
    own = label_lookup._history_label_reader(item, groups=False, cache=cache)
    assert grouped(0) == "not_present"
    assert own(0) is None
    replacement = deepcopy(item)
    replacement["history_labels"] = []
    changed = label_lookup._history_label_reader(replacement, cache=cache)
    assert changed(15) == "not_present"
    assert changed is not grouped


def test_live_edges_follow_sensor_room_labels_and_reused_zone_ids():
    from tuner_under_test.rooms import room_labels

    item = device()
    runtime = SimpleNamespace(
        data={"devices": {"a": item}, "room_labels": {}}, _schedule_save=lambda: None
    )
    room_labels.write_sensor_range(runtime, "zone", 12, 14, "not_present", 0.9)
    assert 14 in training._live_label_edges(runtime, item)
    # Extending an open sensor label changes no revision at all.
    room_labels.write_sensor_range(runtime, "zone", 14, 18, "not_present", 0.9, bump=False)
    assert 18 in training._live_label_edges(runtime, item)
    # A removed zone takes its revision with it, so a new zone restarts the count.
    runtime.data["room_labels"] = {"zone": [{"start": 70, "end": 72, "state": "not_present"}]}
    runtime.data["room_label_revision"] = {}
    edges = training._live_label_edges(runtime, item)
    assert 70 in edges and 18 not in edges
