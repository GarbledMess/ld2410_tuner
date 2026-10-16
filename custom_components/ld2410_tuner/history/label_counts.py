"""Labelled recorded time behind a radar's histograms, kept so a label edit is cheap.

A device label edit (and a Seen by chip tap, which is one) rebuilds the radar's histograms from
every retained recorded second under its labels. The counts are kept per radar instead
(``Counts``, on ``runtime._label_counts``). A rebuild recounts only what differs from the kept
counts: finished days added or removed since, the days a changed label or the retention edge
touches, and the day being recorded. Without kept counts (or after most days were replaced, as
by a storage trim) a rebuild counts everything.

Counts are exact: every recorded second is counted under the label at its time and the
retention cutoff, so the histograms equal a rebuild from scratch. They are kept in seconds and
reported in six-second units (``raw``).
"""

from __future__ import annotations

import math
import time
from bisect import bisect_left, bisect_right, insort

from ..calibration.constants import SAMPLE_SECONDS
from ..const import HISTOGRAM_BINS, HISTORY_KEYS, MAX_HISTOGRAM_COUNT
from . import daystore, label_lookup, recording, summaries
from .policy import retention_seconds

STATES = ("present", "not_present")


def empty():
    return {key: {state: [0] * HISTOGRAM_BINS for state in STATES} for key in HISTORY_KEYS}


def stored(raw):
    """Histograms as kept in the saved data, from counts in six-second units: each scaled to
    at most ``MAX_HISTOGRAM_COUNT`` and kept to a thousandth of a sample."""
    result = {}
    for key, series in raw.items():
        result[key] = {}
        for state, histogram in series.items():
            total = sum(histogram)
            scale = min(1.0, MAX_HISTOGRAM_COUNT / total) if total else 1.0
            result[key][state] = [round(value * scale, 3) for value in histogram]
    return result


def _counted(read, timestamp):
    state = read(timestamp)
    return state if state in STATES else None


def _native_spans(block=None, pending=None):
    """The recorded spans of a finished day (``block``) or of the day being recorded."""
    if block is not None:
        day = block["days"].day(block["device_id"], block["date"])
        return summaries.of_day(day) if day is not None else iter(())
    return summaries.of_day(pending) if pending is not None else iter(())


def _add_duration(raw, state, values, weight):
    for key, value in zip(HISTORY_KEYS, values, strict=True):
        if value is not None:
            histogram = raw[key][state]
            bin_ = math.ceil(value)
            histogram[bin_] = max(0.0, histogram[bin_] + weight)


def _count_native(raw, items, read, cutoff, sign):
    if read is None:
        return
    if isinstance(items, summaries.Summary):
        _count_summary(raw, items, read, cutoff, sign)
        return
    for item in items:
        low, high = max(item.start, cutoff), item.end
        if high <= low:
            continue
        edges = read.edges
        cuts = [low, *edges[bisect_right(edges, low) : bisect_left(edges, high)], high]
        for start, end in zip(cuts, cuts[1:], strict=False):
            if state := _counted(read, start):
                _add_duration(raw, state, item.values, sign * (end - start))


def _count_summary(raw, summary, read, cutoff, sign):
    day = summary.day
    if not day.times:
        return
    low, high = max(cutoff, day.times[0]), day.ends[-1]
    edges = read.edges
    cuts = [low, *edges[bisect_right(edges, low) : bisect_left(edges, high)], high]
    for start, end in zip(cuts, cuts[1:], strict=False):
        if state := _counted(read, start):
            for key, histogram in zip(HISTORY_KEYS, summary.histograms(start, end), strict=True):
                raw[key][state] = [
                    max(0.0, a + sign * b) for a, b in zip(raw[key][state], histogram, strict=True)
                ]


class Counts:
    """One radar's labelled recorded seconds and the finished days they were counted from.

    ``seconds`` holds per gate and state the recorded seconds at each reading. Finished days
    are kept by identity (``_day_blocks`` keeps one object per unchanged day) with their
    span, sorted in ``lows``; a day whose file cannot be read is ``inert``. The day being
    recorded (``live``) is counted separately and recounted on every update."""

    __slots__ = ("seconds", "index", "cutoff", "regular", "inert", "lows", "width", "live")

    def __init__(self, index, cutoff):
        self.seconds = empty()
        self.index, self.cutoff = index, cutoff
        self.regular, self.inert = {}, {}
        self.lows, self.width = [], 0.0
        self.live = None

    def blocks(self):
        return {**self.regular, **self.inert}

    @property
    def raw(self):
        """The counts in six-second units."""
        return {
            key: {
                state: [seconds / SAMPLE_SECONDS for seconds in self.seconds[key][state]]
                for state in STATES
            }
            for key in HISTORY_KEYS
        }

    def count(self, items, read, cutoff, sign):
        _count_native(self.seconds, items, read, cutoff, sign)

    def add_block(self, block, read, cutoff, summary=None):
        """Count a finished day not yet kept, at ``cutoff`` under ``read``."""
        try:
            items = summary if summary is not None else _native_spans(block)
        except (TypeError, ValueError, KeyError):
            self.inert[id(block)] = block
            return
        self.count(items, read, cutoff, 1)
        self.regular[id(block)] = (block, block["start"], block["end"])
        insort(self.lows, (block["start"], id(block)))
        self.width = max(self.width, block["end"] - block["start"])

    def drop_block(self, key, read, cutoff):
        """Uncount a kept day (counted at ``cutoff`` under ``read``) and forget it."""
        if self.inert.pop(key, None) is not None:
            return
        block, low, _high = self.regular.pop(key)
        del self.lows[bisect_left(self.lows, (low, key))]
        self.recount(block, (read, cutoff), (None, None))

    def recount(self, block, old, new):
        """Move a day's seconds from their ``(read, cutoff)`` count to the new one; a None
        reader counts nothing."""
        items = _native_spans(block)
        self.count(items, *old, -1)
        self.count(items, *new, 1)

    def touching(self, spans):
        """Kept days whose span meets one of the sorted half-open ``spans``."""
        found, lows = set(), self.lows
        for start, stop in spans:
            for position in range(bisect_left(lows, (start - self.width,)), len(lows)):
                low, key = lows[position]
                if low >= stop:
                    break
                if self.regular[key][2] >= start:
                    found.add(key)
        return found

    def update(self, current, live, cutoff, index):
        """Bring the counts to the finished days ``current`` (id -> block), the ``live`` day,
        ``cutoff`` and label ``index``. False, changing nothing, when most kept days are gone
        (a rewritten history): counting afresh is then cheaper."""
        old = label_lookup.index_reader(self.index), self.cutoff
        new = label_lookup.index_reader(index), cutoff
        kept = self.blocks()
        removed = kept.keys() - current.keys()
        if 2 * len(removed) > len(kept):
            return False
        self.count_live(self.live, *old, -1)
        for key in removed:
            self.drop_block(key, *old)
        changed = _changed(self.index, index)
        if self.cutoff != cutoff:
            changed = _merge([*changed, (min(self.cutoff, cutoff), max(self.cutoff, cutoff))])
        for key in self.touching(changed):
            if key in current:
                self.recount(current[key], old, new)
        for key in current.keys() - kept.keys():
            self.add_block(current[key], *new)
        self.count_live(live, *new, 1)
        self.live, self.index, self.cutoff = live, index, cutoff
        return True

    def count_live(self, live, read, cutoff, sign):
        if live is not None:
            self.count(_native_spans(pending=live), read, cutoff, sign)


def _changed(old, new):
    """Sorted, merged half-open spans where two label indexes count a sample differently."""
    old_read, new_read = label_lookup.index_reader(old), label_lookup.index_reader(new)
    points = sorted({*old[0], *new[0]})
    spans = []
    for position, point in enumerate(points):
        if _counted(old_read, point) != _counted(new_read, point):
            following = points[position + 1] if position + 1 < len(points) else math.inf
            spans.append((point, following))
    return _merge(spans)


def _merge(spans):
    merged = []
    for low, high in sorted(spans):
        if merged and low <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


def count(history, cutoff, index, live=None):
    """Count from scratch (safe in the executor): every finished day, then the live one."""
    counts = Counts(index, cutoff)
    read = label_lookup.index_reader(index)
    for block in history:
        counts.add_block(block, read, cutoff)
    counts.count_live(live, read, cutoff, 1)
    counts.live = live
    return counts


def current(runtime, device):
    """The labelled recorded time of ``device`` now, in six-second units, as a full recount
    would give it: from the kept counts when there are some."""
    device_id = recording._device_id(runtime, device)
    cutoff = max(0, time.time() - retention_seconds(runtime.data))
    index = label_lookup.label_index(device)
    history, live = _day_blocks(runtime, device_id), _live_day(runtime, device_id)
    kept = getattr(runtime, "_label_counts", {})
    # Taken out while updating: counts left half-updated by an error are never reused.
    counts = kept.pop(device_id, None)
    blocks = dict(zip(map(id, history), history, strict=True))
    if counts is None or not counts.update(blocks, live, cutoff, index):
        counts = count(history, cutoff, index, live)
    if device_id is not None:
        kept[device_id] = counts
    return counts.raw


def _day_blocks(runtime, device_id):
    """Each finished recorded day as a block: the same object while the radar's finished days
    are unchanged (``DayStore.mark``), so kept counts are not recounted."""
    days = getattr(runtime, "_days", None)
    if days is None or device_id is None:
        return []
    revision, live = days.mark(device_id)
    kept = runtime.__dict__.setdefault("_day_blocks", {})
    blocks = []
    for date in days.days(device_id):
        if live is not None and date == live[0]:
            continue
        known = kept.get((device_id, date))
        if known is None or known[0] != revision:
            start, end = daystore.bounds(date)
            block = {"kind": "day", "start": start, "end": end, "date": date}
            known = kept[(device_id, date)] = (
                revision,
                {**block, "device_id": device_id, "days": days},
            )
        blocks.append(known[1])
    return blocks


def _live_day(runtime, device_id):
    days = getattr(runtime, "_days", None)
    live = None if days is None or device_id is None else days.mark(device_id)[1]
    if live is None:
        return None
    return days.day(device_id, live[0], until=getattr(runtime, "_history_until", None))


EDITS = ("label_history_range", "edit_history_label")  # Runtime methods that rebuild.


def _worker_count(history, cutoff, index, live):
    """Load optional finished indexes only in the executor, then count each day once."""
    counts = Counts(index, cutoff)
    read = label_lookup.index_reader(index)
    for block in history:
        summary = block["days"].summary(block["device_id"], block["date"])
        counts.add_block(block, read, cutoff, summary)
    counts.count_live(live, read, cutoff, 1)
    counts.live = live
    return counts


async def ensure(runtime, device_id):
    """Have kept counts for ``device_id`` before an edit needs them, counting in the executor
    when there are none (after a restart, a trim or a discarded cleanup): the event loop never
    counts all history. Days added or removed while counting are caught up by the edit."""
    device = runtime.data["devices"].get(device_id)
    if device is None or device_id in runtime._label_counts:
        return
    history, live = _day_blocks(runtime, device_id), _live_day(runtime, device_id)
    cutoff = max(0, time.time() - retention_seconds(runtime.data))
    index = label_lookup.label_index(device)
    counts = await runtime.hass.async_add_executor_job(_worker_count, history, cutoff, index, live)
    if device_id not in runtime._label_counts:
        runtime._label_counts[device_id] = counts


def forget(runtime, device_id):
    """Drop a radar's kept counts (its history was cleared)."""
    getattr(runtime, "_label_counts", {}).pop(device_id, None)
