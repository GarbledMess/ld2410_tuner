"""Frozen streamed attribution reference from the pre-optimization implementation."""

from math import inf

from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history.label_lookup import _history_label_reader


class _StreamEpisode:
    """One attribution episode, with one sustain clock per configured gate."""

    def __init__(self, low, baseline):
        self.start = self.end = low
        self.baseline = baseline
        self.states, self.seen, self.begin = set(), set(), {}

    def add(self, low, high, values, own):
        from tuner_under_test.calibration.constants import ATTRIBUTION_SUSTAIN_SECONDS

        self.states.add(own(low))
        for gate, quiet in self.baseline.items():
            value = values[gate]
            if value is None or value <= quiet.ceiling():
                self.begin.pop(gate, None)
                continue
            begin = self.begin.setdefault(gate, low)
            if high - begin >= ATTRIBUTION_SUSTAIN_SECONDS:
                self.seen.add(gate)
        self.end = high

    def result(self):
        from tuner_under_test.calibration.constants import ATTRIBUTION_FULL_CONFIDENCE_SAMPLES

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
    from tuner_under_test.history.fit_rows import CapturedRowSource
    from tuner_under_test.rooms.event_attribution import _cells

    start = max(
        start,
        getattr(view, "_attribution_since", None) or -inf,
        getattr(view, "_physical_history_start", -inf),
    )
    rows = CapturedRowSource(view, device_id, tuple(range(len(HISTORY_KEYS))))
    yield from _cells(_physical(rows, start, end), edges)


def _stream_baseline(view, device_id, edges, reads, own, start, end):
    from tuner_under_test.rooms.attribution import quiet_reader
    from tuner_under_test.rooms.event_attribution import _baseline

    quiet = quiet_reader(own, reads)
    cells = _stream_cells(view, device_id, edges, start, end)
    # _baseline consumes its two iterables in lockstep, so tee holds one cell.
    from itertools import tee

    physical, answers = tee(cells)
    return _baseline(physical, (quiet(low) for low, _high, _values in answers))


def _stream_found(view, device_id, edges, reads, own, baseline, start, end):
    from math import nextafter

    from tuner_under_test.rooms.event_attribution import _present

    active, found = {}, {group: [] for group in reads}
    for low, high, values in _stream_cells(view, device_id, edges, start, end):
        for group, read in reads.items():
            _stream_update(
                active, found, group, _present(read(low), values), low, high, values, own, baseline
            )
    for group, run in active.items():
        found[group].append(((run.start, nextafter(run.end, -inf)), run.result()))
    return found


def _stream_update(active, found, group, present, low, high, values, own, baseline):
    from math import nextafter

    run = active.get(group)
    if run is not None and (run.end != low or not present):
        found[group].append(((run.start, nextafter(run.end, -inf)), run.result()))
        active.pop(group)
    if present:
        active.setdefault(group, _StreamEpisode(low, baseline)).add(low, high, values, own)


def _stream_attribution(view, device_id, saved, others, start, end):
    from tuner_under_test.rooms import attribution, truth

    contexts = attribution._contexts(saved, others)
    reads = {group: attribution._reader(context) for group, context in contexts.items()}
    if others is None:
        reads = {group: attribution._human_only(read) for group, read in reads.items()}
    own = _history_label_reader(view.data["devices"][device_id], groups=False)
    edges = sorted(
        {*own.edges, *(edge for context in contexts.values() for edge in truth.edges(context))}
    )
    baseline = _stream_baseline(view, device_id, edges, reads, own, start, end)
    found = _stream_found(view, device_id, edges, reads, own, baseline, start, end)
    return {"own": own, "truths": reads, "contexts": contexts, "found": found}


def _physical(source, start, end):
    from tuner_under_test.history import pace, spans

    start = max(start, getattr(source.view, "_physical_history_start", float("-inf")))
    for block in source.blocks(start, end):
        pace.rest()
        for index, (lo, hi) in enumerate(zip(block.times, block.ends, strict=True)):
            values = tuple(None if col[index] == 255 else col[index] for col in block.columns)
            yield spans.Span(lo, hi, values, "event")
