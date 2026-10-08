"""Read bounded room evidence without relabelling or copying one zone into another."""

from ..calibration.constants import MIN_AUTO_CONFIDENCE, SAMPLE_SECONDS
from ..const import HISTORY_KEYS
from ..history.labels import _history_label_reader
from .intervals import Timeline

LABELS = ("present", "not_present")


def read(view, device_id, keys, start, end):
    device = view.data["devices"][device_id]
    records = [
        (ts, row)
        for ts, row in view._iter_history_samples(device, include_auto=True)
        if start <= ts <= end
    ]
    indices = {key: HISTORY_KEYS.index(key) for key in keys}
    rows, barriers = [], []
    for ts, row in records:
        values = {key: row[index] for key, index in indices.items() if row[index] <= 100}
        if len(values) == len(keys):
            rows.append((ts, values, "unknown"))
        else:
            barriers.append(ts)
    spans = _automatic_cells(records)
    labels = _history_label_reader(device)
    boundaries = {start, end, *(point for span in spans for point in span[:2])}
    for item in device.get("history_labels", []):
        boundaries.update((item["start"], item["end"]))
    boundaries.update(
        device.get(k)
        for k in ("training_label_start", "training_expires_at")
        if device.get(k) is not None
    )
    return {
        "rows": rows,
        "barriers": barriers,
        "manual": labels,
        "automatic": Timeline(spans),
        "boundaries": {t for t in boundaries if start <= t <= end},
    }


def _automatic_cells(records):
    spans = []
    for index, (_ts, row) in enumerate(records):
        start, end = _cell_edges(records, index)
        if len(row) < len(HISTORY_KEYS) + 2:
            continue
        label = {1: "present", 2: "not_present"}.get(row[len(HISTORY_KEYS)])
        confidence = row[len(HISTORY_KEYS) + 1] / 100
        if label and confidence >= MIN_AUTO_CONFIDENCE and end > start:
            spans.append((start, end, (label, "automatic", confidence)))
    return spans


def device_label(evidence, timestamp):
    manual = evidence["manual"](timestamp)
    if manual in LABELS:
        return manual, "human", 1.0
    if manual is not None:
        return None  # An explicit Unknown prevents fallback to inferred labels.
    return evidence["automatic"].at(timestamp)


def room_label(evidence, timestamp):
    labels = [device_label(item, timestamp) for item in evidence.values()]
    present = [item for item in labels if item and item[0] == "present"]
    if present:
        # A confirmed occupied coverage area proves room occupancy. It says
        # nothing about other zones, whose assessments use only their members.
        return max(present, key=lambda item: (item[1] == "human", item[2]))
    if any(item is None for item in labels):
        return None
    source = "human" if all(item[1] == "human" for item in labels) else "automatic"
    return "not_present", source, min(item[2] for item in labels)


def _cell_edges(records, index):
    ts = records[index][0]
    before = records[index - 1][0] if index else ts
    after = records[index + 1][0] if index + 1 < len(records) else ts
    start = (before + ts) / 2 if ts - before <= SAMPLE_SECONDS * 2 else ts
    end = (ts + after) / 2 if after - ts <= SAMPLE_SECONDS * 2 else ts
    return start, end
