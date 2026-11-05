"""Synthetic fragmented room labels and bounded six-second column blocks."""

from array import array
from types import SimpleNamespace

from tuner_under_test.engine.hybrid.types import RowBlock


class Source:
    def __init__(self, view, device_id, channels):
        self.view, self.device_id = view, device_id

    def blocks(self, start, end):
        stop = min(end, self.view._history_until)
        for first in range(0, self.view.samples, 4096):
            indices = range(first, min(first + 4096, self.view.samples))
            selected = [i for i in indices if i * 6 < stop and (i + 1) * 6 > start]
            if not selected:
                continue
            times = array("d", (max(start, i * 6) for i in selected))
            ends = array("d", (min(stop, (i + 1) * 6) for i in selected))
            columns = tuple(_column(selected[0], len(selected), gate) for gate in range(18))
            yield RowBlock(times, ends, columns)


def fixture(days=1, seconds=86400):
    end = days * seconds
    devices = {key: {"history_labels": []} for key in ("r", "s")}
    labels = [
        {
            "start": d * seconds + i * seconds / 248,
            "end": d * seconds + (i + 1) * seconds / 248,
            "state": "present" if i % 2 else "not_present",
            "source": "sensor",
            "confidence": 0.8,
        }
        for d in range(days)
        for i in range(248)
    ]
    saved = {
        "device_id": "r",
        "own": ["room"],
        "groups": {"room": {"device_ids": ["r", "s"]}},
        "members": devices,
        "room_labels": {"room": labels},
    }
    return SimpleNamespace(
        data={"devices": devices, "room_snapshot": saved},
        samples=end // 6,
        _history_until=end,
        _attribution_until=end,
    )


def _column(first, count, gate):
    if gate >= 4:
        return bytes([255]) * count
    pattern = bytes(i * (gate + 1) % 71 for i in range(71))
    offset = first % 71
    return (pattern * ((count + offset) // 71 + 1))[offset : offset + count]
