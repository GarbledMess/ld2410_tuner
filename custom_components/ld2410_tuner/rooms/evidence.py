"""Room truth and streamed attribution inputs shared by the hybrid adapters.

Packed rows below serve physical diagnostic utilities; fitting consumes label_inputs
and streamed_room_targets without constructing a row-mask objective.
"""

from array import array
from itertools import islice
from math import inf
from operator import le

from ..calibration.constants import AGREEING_MEMBERS
from ..calibration.event_replay import _timing
from ..calibration.metrics import bit_planes
from ..const import HISTORY_KEYS
from ..history import guesses, spans
from ..history.label_lookup import _history_label_reader

LABELS = ("present", "not_present")


class Readings:
    """One sample's energies of a configured gate layout, read like the ``{gate: energy}`` they
    replace: a byte each and a layout shared by every sample, where a dictionary took
    hundreds of bytes a sample. Only ``get``, ``in``, ``[]`` and ``items`` are read."""

    __slots__ = ("layout", "values")

    def __init__(self, layout, values):
        self.layout, self.values = layout, values

    def get(self, key, default=None):
        position = self.layout.get(key)
        if position is None or self.values[position] > 100:
            return default
        return self.values[position]

    def __getitem__(self, key):
        value = self.values[self.layout[key]]
        if value > 100:
            raise KeyError(key)  # The gate did not read.
        return value

    def __contains__(self, key):
        position = self.layout.get(key)
        return position is not None and self.values[position] <= 100

    def __len__(self):
        return sum(value <= 100 for value in self.values)

    def items(self):
        return [
            (key, self.values[position])
            for key, position in self.layout.items()
            if self.values[position] <= 100
        ]


# Byte -> 1 for a reading above 100, which is no reading: a sample with one is a barrier.
OVER = bytes(int(value > 100) for value in range(256))
MISSING = 255  # A gate that did not read.
# A gate that did not read compares as reading 0: it is above no threshold.
UNREAD_AS_ZERO = bytes(value if value <= 100 else 0 for value in range(256))


class Rows:
    """A radar's readings of some gates, in the order read, kept as the sample ``times`` and a
    bytes of readings for each gate (``columns``, in the order of the ``layout``): a byte
    for each reading where a sample of objects took hundreds. It reads like the list of
    ``(time, Readings, "unknown")`` it replaces."""

    __slots__ = ("times", "columns", "layout", "planes", "ends")

    def __init__(self, times, columns, layout, *, ends=None):
        self.times, self.columns, self.layout, self.planes = times, columns, layout, {}
        self.ends = ends

    def __len__(self):
        return len(self.times)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return Rows(
                self.times[index],
                [column[index] for column in self.columns],
                self.layout,
                ends=None if self.ends is None else self.ends[index],
            )
        values = bytes([column[index] for column in self.columns])
        return self.times[index], Readings(self.layout, values), "unknown"

    def __iter__(self):
        layout = self.layout
        values = map(bytes, zip(*self.columns, strict=True)) if self.columns else None
        if values is None:
            return ((time, Readings(layout, b""), "unknown") for time in self.times)
        return (
            (time, Readings(layout, row), "unknown")
            for time, row in zip(self.times, values, strict=True)
        )

    def column(self, key):
        return self.columns[self.layout[key]]

    def planes_of(self, key):
        """The ``bit_planes`` of the readings of the gate ``key``, made once."""
        if key not in self.planes:
            self.planes[key] = bit_planes(self.column(key).translate(UNREAD_AS_ZERO))
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
            ends=None if self.ends is None else [self.ends[index] for index in order],
        )


def read(view, device_id, keys, start, end, *, automatic_reader=None, timing=None):
    device = view.data["devices"][device_id]
    # Allow an on-delay to settle and the resulting hold/off-delay tail to expire.
    # Query clipping keeps the value of a physical cell straddling this boundary;
    # coverage gaps still reset timing. Attribution has its own prepared context.
    warmup = sum(_timing(timing))
    rows, barriers = _recorded(view, device_id, keys, end, start=start - warmup)
    automatic = (
        guesses.evidence(view, device_id, start) if automatic_reader is None else automatic_reader
    )
    cells = automatic.cells(start, end)
    labels = _history_label_reader(device, groups=False)
    boundaries = {start, end, *(point for span in cells for point in span[:2])}
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


def _recorded(view, device_id, keys, end, *, start=-inf):
    """``(rows, barriers)`` of the recorded spans in ``[start, end)``: a span where any of
    ``keys`` read is a row (from its start to its end; a gate that did not read is kept as
    ``MISSING``, which compares as no reading); one where none of them read is a barrier at
    its start."""
    indices = [HISTORY_KEYS.index(key) for key in keys]
    times, ends, barriers, read = array("d"), array("d"), [], bytearray()
    blank = bytes([MISSING]) * len(keys)
    for cell in spans.search_events(view, device_id, indices, start=start, end=end):
        if cell.values == blank:
            barriers.append(cell.start)
            continue
        times.append(cell.start)
        ends.append(cell.end)
        read.extend(cell.values)
    # Every row's readings end to end; each gate's column is every ``len(keys)``th byte.
    joined, width = bytes(read), len(keys)
    layout = {key: position for position, key in enumerate(keys)}
    rows = Rows(times, [joined[gate::width] for gate in range(width)], layout, ends=ends)
    return rows, sorted(set(barriers))


def member_label(manual, evidence, timestamp):
    """A member's label at ``timestamp``, given its ``manual`` one there."""
    if manual in LABELS:
        return manual, "human", 1.0
    if manual is not None:
        return None  # An explicit Unknown prevents fallback to inferred labels.
    return evidence["automatic"].at(timestamp)


def _like(times, items):
    return array("d", items) if isinstance(times, array) else list(items)


def label_inputs(view, device_id, automatic):
    """Label/provenance inputs without reading or retaining any physical rows."""
    manual = _history_label_reader(view.data["devices"][device_id], groups=False)
    boundaries = {*manual.edges}
    boundaries.update(point for run in automatic.model.context.raw_runs for point in run[:2])
    return {"manual": manual, "automatic": automatic, "edges": tuple(sorted(boundaries))}


class _StreamEpisode:
    """One attribution episode, with one sustain clock per configured gate."""

    def __init__(self, low, baseline):
        self.start = self.end = low
        self.baseline = baseline
        self.states, self.seen, self.begin = set(), set(), {}

    def result(self):
        from ..calibration.constants import ATTRIBUTION_FULL_CONFIDENCE_SAMPLES

        for state, answer in (("present", "yes"), ("not_present", "no")):
            if state in self.states:
                return {"state": answer, "confidence": 1.0, "inferred": False}
        mass = max((self.baseline[g].mass for g in self.seen or self.baseline), default=0)
        return {
            "state": "yes" if self.seen else "no" if self.baseline else "unknown",
            "confidence": min(1, mass / ATTRIBUTION_FULL_CONFIDENCE_SAMPLES),
            "inferred": True,
        }


def _stream_cells(view, device_id, edges, start, end):
    from ..engine.hybrid.quality import _physical
    from ..history.fit_rows import CapturedRowSource
    from .event_attribution import _cells

    start = max(
        start,
        getattr(view, "_attribution_since", None) or -inf,
        getattr(view, "_physical_history_start", -inf),
    )
    rows = CapturedRowSource(view, device_id, tuple(range(len(HISTORY_KEYS))))
    yield from _cells(_physical(rows, start, end), edges)


def _stream_baseline(view, device_id, edges, reads, own, start, end):
    from . import stream_attribution
    from .attribution import quiet_reader

    quiet = quiet_reader(own, reads)
    if not any(map(quiet, _stream_points(edges, start, end))):
        return {}
    blocks = stream_attribution.pieces(view, device_id, edges, start, end)
    return stream_attribution.baseline(blocks, quiet)


def _stream_found(view, device_id, edges, reads, own, baseline, start, end):
    from . import stream_attribution

    blocks = stream_attribution.pieces(view, device_id, edges, start, end)
    return stream_attribution.found(blocks, reads, own, baseline)


def streamed_room_targets(view, device_id, raw, start, end):
    """The attribution union rule with bounded physical passes and no Day objects."""
    from ..engine.hybrid.quality import recorded_guesses
    from . import attribution

    saved = attribution._snapshot_for(view, device_id)
    if saved is None:
        return None
    others = {
        key: raw if key == device_id else recorded_guesses(view, key, start, end)
        for key in saved["members"]
    }
    human = _stream_attribution(view, device_id, saved, None, start, end)
    full = _stream_attribution(view, device_id, saved, others, start, end)
    result = attribution.select(
        attribution._targets(device_id, human, saved["own"]),
        attribution._targets(device_id, full, saved["own"], automatic=True),
    )
    result.dependencies = tuple(saved["members"])
    result.contexts = full["contexts"]
    result.dependencies_at = lambda timestamp, state: _room_dependencies(
        full["contexts"], result.dependencies, timestamp, state
    )
    return result


def _room_dependencies(contexts, dependencies, timestamp, state):
    sensors = (context["sensor"](timestamp) for context in contexts.values())
    return () if any(label and label[0] == state for label in sensors) else dependencies


def _stream_reader(read, edges):
    """Resolve truth once per label interval, shared by both physical passes."""
    from ..history.label_lookup import index_reader

    points = [-inf, *edges]
    return index_reader((points, list(map(read, points))))


def _stream_attribution(view, device_id, saved, others, start, end):
    from . import attribution, truth

    contexts = attribution._contexts(saved, others)
    reads = {group: attribution._reader(context) for group, context in contexts.items()}
    if others is None:
        reads = {group: attribution._human_only(read) for group, read in reads.items()}
    own = _history_label_reader(view.data["devices"][device_id], groups=False)
    edges = sorted(
        {*own.edges, *(edge for context in contexts.values() for edge in truth.edges(context))}
    )
    reads = {group: _stream_reader(read, edges) for group, read in reads.items()}
    found = {group: [] for group in reads}
    if _stream_occupied(reads, edges, start, end):
        baseline = _stream_baseline(view, device_id, edges, reads, own, start, end)
        found = _stream_found(view, device_id, edges, reads, own, baseline, start, end)
    return {"own": own, "truths": reads, "contexts": contexts, "found": found}


def device_label(evidence, timestamp):
    return member_label(evidence["manual"](timestamp), evidence, timestamp)


def agreed(present, members):
    """Whether ``present`` of a room's ``members`` radars saying present make the room present
    on automatic evidence: ``AGREEING_MEMBERS`` of them (every member of a smaller group)."""
    return present >= min(AGREEING_MEMBERS, members)


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


def _stream_points(edges, start, end):
    return [start, *(edge for edge in edges if start < edge < end)] if start < end else []


def _stream_occupied(reads, edges, start, end):
    points = _stream_points(edges, start, end)
    return any((read(t) or (None,))[0] == "present" for read in reads.values() for t in points)
