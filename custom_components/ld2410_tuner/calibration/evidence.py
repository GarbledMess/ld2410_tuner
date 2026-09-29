"""Collect the same bounded, labelled recordings for fitting and comparison."""

from collections import deque

from ..const import HISTORY_KEYS
from ..history.labels import _history_label_reader
from .constants import MAX_CLASS_SAMPLES, MIN_AUTO_CONFIDENCE


def collect_samples(runtime, device_id, keys):
    device = runtime.data["devices"][device_id]
    groups = {label: deque(maxlen=MAX_CLASS_SAMPLES) for label in ("present", "not_present")}
    automatic = {label: deque(maxlen=MAX_CLASS_SAMPLES) for label in groups}
    indices = [HISTORY_KEYS.index(key) for key in keys]
    label_at = _history_label_reader(device)
    for ts, row in runtime._iter_history_samples(device, include_auto=True):
        if not any(row[index] <= 100 for index in indices):
            continue
        label = label_at(ts)
        if label in groups:
            groups[label].append((ts, row, label))
        elif label is None and len(row) >= len(HISTORY_KEYS) + 2:
            _append_auto_sample(automatic, ts, row)

    def values(row):
        return {key: row[i] for i, key in enumerate(HISTORY_KEYS) if row[i] <= 100}

    rows = [(ts, values(row), label) for group in groups.values() for ts, row, label in group]
    guesses = [
        (ts, values(row), label, confidence)
        for group in automatic.values()
        for ts, row, label, confidence in group
    ]
    return rows, guesses


def _append_auto_sample(automatic, ts, row):
    inferred = {1: "present", 2: "not_present"}.get(row[len(HISTORY_KEYS)])
    confidence = row[len(HISTORY_KEYS) + 1] / 100
    if inferred and confidence >= MIN_AUTO_CONFIDENCE:
        automatic[inferred].append((ts, row, inferred, confidence))
