"""Read bounded room evidence without relabelling or copying one zone into another."""

from array import array
from itertools import compress, islice, repeat
from math import inf, nextafter
from operator import is_, le

from ..calibration.constants import AGREEING_MEMBERS
from ..calibration.metrics import bit_planes
from ..const import HISTORY_KEYS
from ..history import guesses
from ..history.labels import _history_label_reader

LABELS = ("present", "not_present")


class Readings:
    """One sample's energies of the gates of a search, read like the ``{gate: energy}`` they
    replace: a byte each and a layout shared by every sample, where a dictionary took
    hundreds of bytes a sample. Only ``get``, ``in``, ``[]`` and ``items`` are read."""

    __slots__ = ("layout", "values")

    def __init__(self, layout, values):
        self.layout, self.values = layout, values

    def get(self, key, default=None):
        position = self.layout.get(key)
        return default if position is None else self.values[position]

    def __getitem__(self, key):
        return self.values[self.layout[key]]

    def __contains__(self, key):
        return key in self.layout

    def __len__(self):
        return len(self.values)

    def items(self):
        return [(key, self.values[position]) for key, position in self.layout.items()]


# Byte -> 1 for a reading above 100, which is no reading: a sample with one is a barrier.
OVER = bytes(int(value > 100) for value in range(256))


class Rows:
    """A radar's readings of some gates, in the order read, kept as the sample ``times`` and a
    bytes of readings for each gate (``columns``, in the order of the ``layout``): a byte
    for each reading where a sample of objects took hundreds. It reads like the list of
    ``(time, Readings, "unknown")`` it replaces."""

    __slots__ = ("times", "columns", "layout", "planes")

    def __init__(self, times, columns, layout):
        self.times, self.columns, self.layout, self.planes = times, columns, layout, {}

    def __len__(self):
        return len(self.times)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return Rows(self.times[index], [column[index] for column in self.columns], self.layout)
        values = bytes(column[index] for column in self.columns)
        return self.times[index], Readings(self.layout, values), "unknown"

    def __iter__(self):
        return map(self.__getitem__, range(len(self)))

    def column(self, key):
        return self.columns[self.layout[key]]

    def planes_of(self, key):
        """The ``bit_planes`` of the readings of the gate ``key``, made once."""
        if key not in self.planes:
            self.planes[key] = bit_planes(self.column(key))
        return self.planes[key]

    def in_time_order(self):
        """These rows if their times are in order, else them in order (ties as they were)."""
        if all(map(le, self.times, islice(self.times, 1, None))):
            return self
        order = sorted(range(len(self)), key=self.times.__getitem__)
        return Rows(
            _like(self.times, map(self.times.__getitem__, order)),
            [bytes(map(column.__getitem__, order)) for column in self.columns],
            self.layout,
        )


def read(view, device_id, keys, start, end):
    device = view.data["devices"][device_id]
    layout = {key: position for position, key in enumerate(keys)}
    indices = [HISTORY_KEYS.index(key) for key in keys]
    times, parts = [], [[] for _ in indices]
    # ``until`` is exclusive and the window includes ``end``: only blocks inside it are read.
    for batch_times, columns in view._history_columns(device, start, nextafter(end, inf)):
        times.extend(batch_times)
        for part, index in zip(parts, indices, strict=True):
            part.append(columns[index])
    if all(map(is_, map(type, times), repeat(float))):  # Kept as read if not floats.
        times = array("d", times)
    rows, barriers = _usable(times, [b"".join(part) for part in parts], layout, start, end)
    automatic = guesses.evidence(view, device_id, start)
    spans = automatic.cells(start, end)
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
        "automatic": automatic,
        "boundaries": {t for t in boundaries if start <= t <= end},
    }


def member_label(manual, evidence, timestamp):
    """A member's label at ``timestamp``, given its ``manual`` one there."""
    if manual in LABELS:
        return manual, "human", 1.0
    if manual is not None:
        return None  # An explicit Unknown prevents fallback to inferred labels.
    return evidence["automatic"].at(timestamp)


def _like(times, items):
    return array("d", items) if isinstance(times, array) else list(items)


def _usable(times, columns, layout, start, end):
    """``(rows, barriers)`` of the samples inside ``[start, end]``: those that read at most 100
    on every gate are rows, the times of the others are barriers."""
    inside = bytes(start <= t <= end for t in times)
    over = 0
    for column in columns:
        over |= int.from_bytes(column.translate(OVER), "little")
    if not over and inside.count(1) == len(times):
        return Rows(times, columns, layout), []
    flagged = over.to_bytes(len(times), "little") if over else bytes(len(times))
    keep = [here and not other for here, other in zip(inside, flagged, strict=True)]
    barriers = list(
        compress(times, [here and other for here, other in zip(inside, flagged, strict=True)])
    )
    if all(keep):
        return Rows(times, columns, layout), barriers
    return (
        Rows(
            _like(times, compress(times, keep)),
            [bytes(compress(column, keep)) for column in columns],
            layout,
        ),
        barriers,
    )


def device_label(evidence, timestamp):
    return member_label(evidence["manual"](timestamp), evidence, timestamp)


def agreed(present, members):
    """Whether ``present`` of a room's ``members`` radars saying present make the room present
    on automatic evidence: ``AGREEING_MEMBERS`` of them (every member of a smaller group)."""
    return present >= min(AGREEING_MEMBERS, members)


def room_label(evidence, timestamp):
    return combine([device_label(item, timestamp) for item in evidence.values()])


def combine(labels):
    """The room's label from its members' ``labels`` (``device_label`` of each)."""
    present = [item for item in labels if item and item[0] == "present"]
    if present:
        # A confirmed occupied coverage area proves room occupancy. It says
        # nothing about other zones, whose assessments use only their members.
        best = max(present, key=lambda item: (item[1] == "human", item[2]))
        return best if best[1] == "human" or agreed(len(present), len(labels)) else None
    if any(item is None for item in labels):
        return None
    source = "human" if all(item[1] == "human" for item in labels) else "automatic"
    return "not_present", source, min(item[2] for item in labels)
