"""Exact observed-duration room attribution, preserving the existing union rule."""

from array import array
from bisect import bisect_left, bisect_right
from itertools import accumulate
from math import ceil, floor, inf, nextafter

from ..calibration.constants import (
    ATTRIBUTION_FULL_CONFIDENCE_SAMPLES,
    ATTRIBUTION_MARGIN,
    ATTRIBUTION_SUSTAIN_SECONDS,
    ATTRIBUTION_TAIL_QUANTILE,
    SAMPLE_SECONDS,
)
from ..const import HISTORY_KEYS
from ..history import pace, spans
from ..history.label_lookup import _history_label_reader
from ..presence.inference import MIN_REFERENCE


class Quiet:
    def __init__(self, bins):
        self.cumulative = tuple(accumulate(bins))
        self.mass = self.cumulative[-1]

    def __len__(self):
        return floor(self.mass)

    def ceiling(self):
        rank = self.mass * ATTRIBUTION_TAIL_QUANTILE
        return min(100, bisect_right(self.cumulative, rank)) + ATTRIBUTION_MARGIN


REST_CELLS = 4096  # Cells between ``pace.rest`` calls: each reads the thread's clock.


def _cells(physical, edges):
    for count, cell in enumerate(physical):
        if not count % REST_CELLS:
            pace.rest()  # Background work: Seen by and learning share the CPU politely.
        cuts = [
            cell.start,
            *edges[bisect_right(edges, cell.start) : bisect_left(edges, cell.end)],
            cell.end,
        ]
        for low, high in zip(cuts, cuts[1:], strict=False):
            yield low, high, cell.values


def _places(cells, edges):
    """Where each cell starts among ``edges``: the stretch between two edges, and whether it
    starts on the edge itself. Cells of one place read alike from every reader here."""
    places = []
    for low, _high, _values in cells:
        index = bisect_right(edges, low)
        places.append((index, index > 0 and edges[index - 1] == low))
    return places


def _read_cells(read, cells, places):
    """``read`` at each cell's start, read once for each run of cells in one place: own
    labels, room truth and the estimate can change their answer only at the edges."""
    answers, last, answer = [], None, None
    for (low, _high, _values), place in zip(cells, places, strict=True):
        if place != last:
            answer, last = read(low), place
        answers.append(answer)
    return answers


def _baseline(cells, quiet):
    """The radar's quiet readings (``quiet``: each cell's answer) as tail-quantile bins."""
    bins = [[0.0] * 101 for _ in HISTORY_KEYS]
    for count, ((low, high, values), still) in enumerate(zip(cells, quiet, strict=True)):
        if not count % REST_CELLS:
            pace.rest()
        if still:
            for histogram, value in zip(bins, values, strict=True):
                if value is not None:
                    histogram[ceil(value)] += high - low
    return {
        i: Quiet([seconds / SAMPLE_SECONDS for seconds in hist])
        for i, hist in enumerate(bins)
        if sum(hist) >= MIN_REFERENCE * SAMPLE_SECONDS
    }


def _episodes(cells, labels):
    """``(first, stop)`` cell indices of each run of contiguous cells read Present."""
    first = None
    for index, ((low, _high, values), label) in enumerate(zip(cells, labels, strict=True)):
        if not index % REST_CELLS:
            pace.rest()
        present = (
            label is not None
            and label[0] == "present"
            and any(value is not None for value in values)
        )
        if first is not None and (not present or cells[index - 1][1] != low):
            yield first, index
            first = None
        if present and first is None:
            first = index
    if first is not None:
        yield first, len(cells)


def _sustained(cells, gate, ceiling):
    begin, reach = None, None
    for low, high, values in cells:
        value = values[gate]
        if value is None or ceil(value) <= ceiling:
            begin, reach = None, None
            continue
        begin = low if begin is None or reach != low else begin
        reach = high
        if reach - begin >= ATTRIBUTION_SUSTAIN_SECONDS:
            return True
    return False


def _decide(cells, own, baseline):
    return _decision({own(low) for low, _high, _values in cells}, cells, baseline)


def _decision(states, cells, baseline):
    """An episode's answer: from the radar's own labels (``states``) inside it, else whether
    it read sustained above its quiet baseline."""
    for state, answer in (("present", "yes"), ("not_present", "no")):
        if state in states:
            return {"state": answer, "confidence": 1.0, "inferred": False}
    if not baseline:
        return {"state": "unknown", "confidence": 0.0, "inferred": True}
    seen = [gate for gate, quiet in baseline.items() if _sustained(cells, gate, quiet.ceiling())]
    mass = max(baseline[gate].mass for gate in seen or baseline)
    return {
        "state": "yes" if seen else "no",
        "inferred": True,
        "confidence": min(1.0, mass / ATTRIBUTION_FULL_CONFIDENCE_SAMPLES),
    }


def _closed(end):
    """An episode's cells cover [start, end); consumers read sampled episodes as closed
    ``(first, last)`` spans, so the native one ends at the last instant before ``end``."""
    return nextafter(end, -inf)


def _cached(runtime, key, edges, reads, own):
    """Each truth's episodes once per job: a view reads history as of its own time, so a
    result never outlives the view (open coverage moves with time)."""
    frozen = key[2] and getattr(runtime, "_history_until", None) is not None  # A job's view.
    kept = runtime.__dict__.setdefault("_native_found_job", {}) if frozen else {}
    if key in kept:
        return kept[key]
    result = kept[key] = _found(runtime, key[0], edges, reads, own)
    return result


def _found(runtime, device_id, edges, reads, own):
    """The expensive part: cells, the quiet baseline and every group's episode decisions."""
    from . import attribution

    cells = list(_cells(_physical(runtime, device_id), edges))
    places = _places(cells, edges)
    quiet = _read_cells(attribution.quiet_reader(own, reads), cells, places)
    baseline = _baseline(cells, quiet)
    owned = _read_cells(own, cells, places)
    found = {
        group: [
            (
                (cells[first][0], _closed(cells[stop - 1][1])),
                _decision(set(owned[first:stop]), cells[first:stop], baseline),
            )
            for first, stop in _episodes(cells, _read_cells(read, cells, places))
        ]
        for group, read in reads.items()
    }
    return array("d", (low for low, _high, _values in cells)), found


def _physical(runtime, device_id):
    """The radar's spans in the job's window (Seen by bounds it to recent hours). A job's view
    keeps the latest radar's: its human-truth and full-truth attributions read the same."""
    since = getattr(runtime, "_attribution_since", 0) or float("-inf")
    until = getattr(runtime, "_attribution_until", float("inf"))
    if getattr(runtime, "_history_until", None) is None:
        return spans.events(runtime, device_id, start=since, end=until)
    key = device_id, since, until
    kept = runtime.__dict__.get("_native_physical")
    if kept is None or kept[0] != key:
        kept = runtime._native_physical = (
            key,
            list(spans.events(runtime, device_id, start=since, end=until)),
        )
    return kept[1]


def calculate(runtime, device_id, saved, human, others):
    from . import attribution, truth

    contexts = attribution._contexts(
        saved,
        None
        if human
        else attribution._member_guesses(runtime, saved)
        if others is None
        else others,
    )
    reads = {group: attribution._reader(context) for group, context in contexts.items()}
    if human:
        reads = {group: attribution._human_only(read) for group, read in reads.items()}
    own = _history_label_reader(runtime.data["devices"][device_id], groups=False)
    edges = sorted(
        {*own.edges, *(edge for context in contexts.values() for edge in truth.edges(context))}
    )
    times, found = _cached(runtime, (device_id, human, others is None), edges, reads, own)
    return {
        "times": times,
        "own": own,
        "truths": reads,
        "contexts": contexts,
        "found": found,
    }
