"""Which label applies at a time: last-label-wins over overlapping periods (pure; no Home
Assistant, so offline tools read labels by the same rules)."""

import heapq
from bisect import bisect_right
from collections import defaultdict


def _history_label_reader(device):
    """Resolve labels once, keeping last-label-wins semantics in logarithmic time."""
    return index_reader(label_index(device))


def label_index(device):
    """``(times, states)``: the device's label (saved periods, then its live label) from each
    time until the next one; None before the first or where no label applies."""
    intervals = list(device.get("history_labels", []))
    start = device.get("training_label_start")
    if start is not None:
        intervals.append(
            {
                "start": start,
                "end": device.get("training_expires_at") or float("inf"),
                "state": device.get("training_state", "unknown"),
            }
        )
    return _label_index(_label_events(intervals))


def index_reader(index):
    """Read the label at a time from a ``label_index``."""
    times, states = index

    def read(timestamp):
        position = bisect_right(times, timestamp) - 1
        return states[position] if position >= 0 else None

    read.edges = times  # Where the answer can change, in order.
    return read


def _label_events(intervals):
    events = defaultdict(list)
    for order, label in enumerate(intervals):
        start, end = float(label.get("start", 0)), float(label.get("end", 0))
        if end > start:
            events[start].append((-order, end, label.get("state", "unknown")))
            events[end]  # Endpoints are needed even when no new interval starts.
    return events


def _label_index(events):
    times, states, active = [], [], []
    for timestamp, additions in sorted(events.items()):
        for entry in additions:
            heapq.heappush(active, entry)
        while active and active[0][1] <= timestamp:
            heapq.heappop(active)
        times.append(timestamp)
        states.append(active[0][2] if active else None)
    return times, states
