"""Labelled-sample counts behind a radar's histograms, kept so a label edit is cheap.

A device label edit (and a Seen by chip tap, which is one) rebuilds the radar's histograms
from every retained sample under its labels, on the event loop: all history, seconds on a small
machine. The counts are kept per radar instead (``Counts``, on ``runtime._label_counts``). A
rebuild recounts only what differs from the kept counts: blocks added or removed since, the
samples whose label changed, samples crossing the retention edge, and the pending samples.

Cleanup (startup and hourly) rewrites history; it counts it again in the executor (``count``)
and keeps the result (``remember``), so an edit normally finds the counts current. Without kept
counts (or after most blocks were replaced, as by a storage trim) a rebuild counts everything,
as before.

Counts are exact: every sample is counted under the same rules as the full rebuild (the label
at its time, the retention cutoff, and which blocks yield samples at all), so the histograms are
identical to rebuilding from scratch.
"""

from __future__ import annotations

import math
import time
from bisect import bisect_left, insort

from ..const import HISTOGRAM_BINS, HISTORY_KEYS
from . import labels, recording
from .codec import SPAN
from .policy import retention_seconds

STATES = ("present", "not_present")


def empty():
    return {key: {state: [0] * HISTOGRAM_BINS for state in STATES} for key in HISTORY_KEYS}


def _add(raw, state, row, sign):
    for index, key in enumerate(HISTORY_KEYS):
        value = row[index]
        if value <= 100:
            raw[key][state][value] += sign


def _counted(read, timestamp):
    state = read(timestamp)
    return state if state in STATES else None


def _side(side, end):
    """A block's ``(read, cutoff)`` count, or None when it counts nothing: no reader, or the
    block ends before the cutoff (``_block_samples`` skips it; a NaN end is never skipped)."""
    read, cutoff = side
    return None if read is None or end < cutoff else side


def _state(side, timestamp):
    """The state a sample counts under on one ``_side`` (None: not counted)."""
    return _counted(side[0], timestamp) if side is not None and timestamp >= side[1] else None


class Counts:
    """One radar's labelled-sample counts and what they were counted from.

    ``raw`` holds per gate and state the value counts of every retained labelled sample:
    ``_rebuild_histograms`` before legacy summaries and compression. Blocks are kept by
    identity (a stored block never changes once written). A block's count can change only
    where its samples or its end (``_block_samples`` skips a block that ends before the
    cutoff) meet a changed span, so each regular block keeps an envelope ``[low, high]`` of
    both, sorted in ``lows`` (no envelope is wider than ``width``). Samples lie within
    ``[start, start + SPAN]``. A block without a usable envelope (a NaN or far-off end) is
    ``irregular`` and re-read on every update; one that can never yield a sample is ``inert``.
    """

    __slots__ = (
        "raw",
        "index",
        "cutoff",
        "regular",
        "irregular",
        "inert",
        "lows",
        "width",
        "pending",
    )

    def __init__(self, index, cutoff):
        self.raw = empty()
        self.index, self.cutoff = index, cutoff
        self.regular, self.irregular, self.inert = {}, {}, {}
        self.lows, self.width = [], 0.0
        self.pending = []

    def blocks(self):
        return {**self.regular, **self.irregular, **self.inert}

    def add_block(self, block, read, cutoff):
        """Count a block not yet kept, at ``cutoff`` under ``read``; keep its envelope."""
        bounds = recording.block_bounds(block)
        if bounds is None:
            self.inert[id(block)] = block
            return
        start, end = bounds
        side = _side((read, cutoff), end)
        rows = [] if side is None else list(recording.block_rows(block, -math.inf))
        if side is not None and not rows:
            self.inert[id(block)] = block  # Undecodable or empty: never yields a sample.
            return
        for timestamp, row in rows:
            self._move(row, None, _state(side, timestamp))
        last = rows[-1][0] if rows else start + SPAN  # Rows come in offset order.
        self._keep(block, min(start, end), max(last, end))

    def _keep(self, block, low, high):
        if not high - low <= 2 * SPAN:  # Also NaN or infinite.
            self.irregular[id(block)] = block
            return
        self.regular[id(block)] = (block, low, high)
        insort(self.lows, (low, id(block)))
        self.width = max(self.width, high - low)

    def drop_block(self, key, read, cutoff):
        """Uncount a kept block (counted at ``cutoff`` under ``read``) and forget it."""
        if self.inert.pop(key, None) is not None:
            return
        block = self.irregular.pop(key, None)
        if block is None:
            block, low, _high = self.regular.pop(key)
            del self.lows[bisect_left(self.lows, (low, key))]
        self.recount(block, (read, cutoff), (None, None))

    def recount(self, block, old, new):
        """Move a block's samples from their ``(read, cutoff)`` count to the new one; a None
        reader counts nothing."""
        end = recording.block_bounds(block)[1]
        old, new = _side(old, end), _side(new, end)
        if old is None and new is None:
            return
        for timestamp, row in recording.block_rows(block, -math.inf):
            self._move(row, _state(old, timestamp), _state(new, timestamp))

    def _move(self, row, was, now):
        """Move one sample's gate values from state ``was`` to ``now`` (None: uncounted)."""
        if was != now:
            if was:
                _add(self.raw, was, row, -1)
            if now:
                _add(self.raw, now, row, 1)

    def touching(self, spans):
        """Kept regular blocks whose envelope meets one of the sorted half-open ``spans``."""
        found, lows = set(), self.lows
        for start, stop in spans:
            for position in range(bisect_left(lows, (start - self.width,)), len(lows)):
                low, key = lows[position]
                if low >= stop:
                    break
                if self.regular[key][2] >= start:
                    found.add(key)
        return found

    def count_pending(self, pending, read, cutoff, sign):
        for timestamp, row in pending:
            if state := _state((read, cutoff), timestamp):
                _add(self.raw, state, row, sign)

    def update(self, current, pending, cutoff, index):
        """Bring the counts to the blocks ``current`` (id -> block), ``pending``, ``cutoff``
        and label ``index``. False, changing nothing, when most kept blocks are gone (a
        rewritten history): counting afresh is then cheaper."""
        old = labels.index_reader(self.index), self.cutoff
        new = labels.index_reader(index), cutoff
        kept = self.blocks()
        removed = kept.keys() - current.keys()
        if 2 * len(removed) > len(kept):
            return False
        for key in removed:
            self.drop_block(key, *old)
        changed = _changed(self.index, index)
        if self.cutoff != cutoff:
            changed = _merge([*changed, (min(self.cutoff, cutoff), max(self.cutoff, cutoff))])
        for key in self.touching(changed) | set(self.irregular):
            if key in current:
                self.recount(current[key], old, new)
        for key in current.keys() - kept.keys():
            self.add_block(current[key], *new)
        self.count_pending(self.pending, *old, -1)
        self.count_pending(pending, *new, 1)
        self.index, self.cutoff, self.pending = index, cutoff, list(pending)
        return True


def _changed(old, new):
    """Sorted, merged half-open spans where two label indexes count a sample differently."""
    old_read, new_read = labels.index_reader(old), labels.index_reader(new)
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


def count(history, pending, cutoff, index):
    """Count from scratch (safe in the executor): every block, then the pending samples."""
    counts = Counts(index, cutoff)
    read = labels.index_reader(index)
    for block in history:
        counts.add_block(block, read, cutoff)
    counts.count_pending(pending, read, cutoff, 1)
    counts.pending = list(pending)
    return counts


def current(runtime, device):
    """The labelled-sample counts of ``device`` now, as ``_rebuild_histograms`` counts them
    over ``runtime._iter_history_samples(device)``: from the kept counts when there are some."""
    device_id = recording._device_id(runtime, device)
    cutoff = max(0, time.time() - retention_seconds(runtime.data))
    index = labels.label_index(device)
    history = device.get("history", [])
    pending = recording.pending_samples(runtime, device_id)
    kept = getattr(runtime, "_label_counts", {})
    # Taken out while updating: counts left half-updated by an error are never reused.
    counts = kept.pop(device_id, None)
    blocks = dict(zip(map(id, history), history, strict=True))
    if counts is None or not counts.update(blocks, pending, cutoff, index):
        counts = count(history, pending, cutoff, index)
    if device_id is not None and len(blocks) == len(history):  # Each block listed once.
        kept[device_id] = counts
    return counts.raw


EDITS = ("label_history_range", "edit_history_label")  # Runtime methods that rebuild.


async def ensure(runtime, device_id):
    """Have kept counts for ``device_id`` before an edit needs them, counting in the executor
    when there are none (after a restart, a trim or a discarded cleanup): the event loop
    never counts all history. Stored blocks never change, so the live ones are counted as
    they are; blocks added or removed while counting are caught up by the edit (``update``)."""
    device = runtime.data["devices"].get(device_id)
    if device is None or device_id in runtime._label_counts:
        return
    history = list(device.get("history", []))
    pending = list(recording.pending_samples(runtime, device_id))
    cutoff = max(0, time.time() - retention_seconds(runtime.data))
    counts = await runtime.hass.async_add_executor_job(
        count, history, pending, cutoff, labels.label_index(device)
    )
    if len({*map(id, history)}) == len(history) and device_id not in runtime._label_counts:
        runtime._label_counts[device_id] = counts


def remember(runtime, device_id, counts, counted):
    """Keep ``counts`` (from ``count`` in the executor) for ``device_id``, whose current
    history equals ``counted`` block for block (the very list, or a copy of it)."""
    live = runtime.data["devices"][device_id].get("history", [])
    if len({*map(id, live)}) != len(live):
        return
    twins = {id(copy): block for copy, block in zip(counted, live, strict=True)}
    counts.regular = {
        id(twins[key]): (twins[key], low, high)
        for key, (_block, low, high) in counts.regular.items()
    }
    counts.irregular = {id(twins[key]): twins[key] for key in counts.irregular}
    counts.inert = {id(twins[key]): twins[key] for key in counts.inert}
    counts.lows = sorted((low, id(twins[key])) for low, key in counts.lows)
    runtime._label_counts[device_id] = counts


def forget(runtime, device_id):
    """Drop a radar's kept counts (its history was cleared)."""
    getattr(runtime, "_label_counts", {}).pop(device_id, None)
