"""Recording operations on the shared runtime state."""

from __future__ import annotations

import asyncio
import math
import struct
import time
import zlib
from bisect import bisect_left
from copy import deepcopy
from functools import lru_cache
from itertools import compress
from typing import Any

from homeassistant.core import callback

from ..const import (
    AUTO_SAMPLE_INTERVAL,
    HISTOGRAM_BINS,
    HISTORY_BLOCK_SAMPLES,
    HISTORY_KEYS,
    HISTORY_SAMPLE_INTERVAL,
    TRAINING_FIELDS,
)
from ..presence import reference_training
from . import guesses, label_counts, labels
from .cleanup import accept_cleanup as _accept_cleanup
from .cleanup import clean_history
from .codec import (
    DELTA_CODED,
    SPAN,
    STRIDES,
    VERSION,
    decode_column_list,
    decode_payload,
    encode_payload,
)
from .policy import STORED, retained, retention_seconds, settings


def _record_history_sample(runtime, device_id: str, values: dict[str, float], now: float) -> None:
    """Keep a compact time-indexed history so past periods can be labelled later."""
    state = runtime._history_runtime.setdefault(
        device_id, {"last_sample": 0.0, "samples": [], "runs": [], "start": None}
    )
    device = runtime.data.get("devices", {}).get(device_id)
    if device is None or not device.get("recording_enabled", True):
        return
    if "history_legacy_histograms" not in device:
        device["history_legacy_histograms"] = deepcopy(device.get("histograms", {}))
    if now - float(state.get("last_sample", 0.0)) < HISTORY_SAMPLE_INTERVAL:
        return
    previous, state["last_sample"] = state.get("last_sample") or None, now
    if state.get("start") is None:
        state["start"] = now
    row = bytes(
        max(0, min(100, int(round(values.get(key, 255))))) if key in values else 255
        for key in HISTORY_KEYS
    )
    if state["samples"] and now - state["samples"][0][0] > SPAN:
        runtime._flush_history_block(device_id)
    state["samples"].append((now, row))
    _record_guess(runtime, device, state, row, now, previous)
    _record_manual_histogram(runtime, device, row, now)
    if len(state["samples"]) >= HISTORY_BLOCK_SAMPLES:
        runtime._flush_history_block(device_id)


def _record_guess(runtime, device, state, row, now, previous) -> None:
    """Fold the radar's guess for the sample at ``now`` into the open run, the references and
    the observation counts."""
    inferred, percent, basis = _current_guess(device, now)
    sample_guess = guesses.guess(inferred, percent, basis)
    guesses.advance(state.setdefault("runs", []), previous, now, sample_guess)
    weight = guesses.counted(sample_guess, row, device.get("histograms"))
    reference_training.observe(
        runtime,
        device,
        now,
        row,
        inferred if weight else None,
        weight or 0.0,
        external=basis == guesses.EXTERNAL,
    )
    if sample_guess:
        observations = device.setdefault("auto", {}).setdefault("observations", {})
        observations[inferred] = observations.get(inferred, 0) + 1


def _flush_history_block(runtime, device_id: str) -> None:
    state = runtime._history_runtime.get(device_id)
    if not state or not state.get("samples"):
        return
    samples = state["samples"]
    start = float(samples[0][0])
    raw = bytearray()
    for timestamp, row in samples:
        offset = max(0, min(SPAN, int(round(timestamp - start))))
        raw.extend(struct.pack(">H", offset))
        raw.extend(row)
    block = {
        "version": VERSION,
        "start": start,
        "count": len(samples),
        "end": float(samples[-1][0]),
        "data": encode_payload(bytes(raw)),
    }
    # Runs that can no longer change are stored; the open one waits in memory (saving adds it).
    closed, state["runs"] = guesses.split_open(state.get("runs", []), samples[-1][0])
    device = runtime.data.get("devices", {}).get(device_id)
    if device is not None:
        history = device.setdefault("history", [])
        history.append(block)
        cutoff = time.time() - retention_seconds(runtime.data)
        device["history"] = retained(history, cutoff)
        chunks = device.setdefault("guess_runs", [])
        if closed:  # Most flushes close none; the hourly cleanup also drops aged-out chunks.
            device["guess_runs"] = retained(guesses.upsert(chunks, closed), cutoff)
    state["samples"] = []
    state["start"] = None


@callback
def _enforce_history_retention(runtime, *_args) -> None:
    if runtime._cleanup_task is None or runtime._cleanup_task.done():
        runtime._cleanup_task = runtime.hass.async_create_task(runtime.async_clean_history())


async def async_clean_history(runtime, *, persist=True):
    """Normalize stored history at startup and hourly, without racing writes."""
    changed = False
    changed = await _clean_devices(runtime, changed)
    if changed:
        runtime._history_cache.clear()
        if persist:
            runtime._schedule_save()
    return changed


async def flush_history(runtime) -> None:
    for device_id in runtime._history_runtime:
        runtime._flush_history_block(device_id)
    runtime._schedule_save()
    if runtime._save_task:
        await asyncio.shield(runtime._save_task)


def newest_sample(runtime, device_id):
    """The time of ``device_id``'s newest recorded sample, stored or pending; None for none."""
    pending = pending_samples(runtime, device_id)
    device = runtime.data["devices"].get(device_id) or {}
    ends = [
        bounds[1]
        for block in device.get("history", [])
        if (bounds := block_bounds(block)) and math.isfinite(bounds[1])
    ]
    return max([*ends, *(ts for ts, _ in pending[-1:])], default=None)


def pending_samples(runtime, device_id) -> list:
    """``device_id``'s recorded samples not yet stored in a history block."""
    return runtime._history_runtime.get(device_id, {}).get("samples", [])


def _iter_history_samples(runtime, device: dict[str, Any], since=0, until=math.inf):
    """Retained samples at or after ``since``. ``until`` skips stored blocks that start at or
    after it (they hold no earlier sample); the caller still filters samples by time."""
    cutoff = max(since, time.time() - retention_seconds(runtime.data))
    device_id = _device_id(runtime, device)
    first = _first_block(runtime, device_id, device.get("history", []), cutoff)
    yield from _stored_history_samples(device, cutoff, first, until)

    yield from _pending_history_samples(runtime, device_id, cutoff)


def _device_id(runtime, device):
    return next(
        (key for key, candidate in runtime.data["devices"].items() if candidate is device), None
    )


class BlockIndex:
    """Facts about one device's history list, kept while that list lives.

    A history list is only ever appended to in place (``_flush_history_block``) or has a
    block replaced by one with the same times and count (``source_labels``); any other change
    installs a new list, which gets a new index. So facts about the blocks seen so far stay
    true and are extended over blocks appended since."""

    __slots__ = ("history", "reach", "folded", "samples", "oldest", "newest")

    def __init__(self, history):
        self.history = history
        self.reach = []  # Running maximum of block ends (``_block_end``).
        self.folded, self.samples, self.oldest, self.newest = 0, 0, None, None

    def shrunk(self):
        """A list shortened in place (never done) is not indexed incrementally."""
        return max(len(self.reach), self.folded) > len(self.history)

    def first(self, cutoff):
        """Index of the first block that can hold a sample at or after ``cutoff``: every
        earlier block ends before it, so ``_block_samples`` yields nothing for it."""
        reach = self.reach
        latest = reach[-1] if reach else -math.inf
        for block in self.history[len(reach) :]:
            latest = max(latest, _block_end(block))
            reach.append(latest)
        return bisect_left(reach, cutoff)

    def summary(self):
        """``(samples, oldest, newest)``: total block counts and the first and last block
        start, folded exactly as ``sum``, ``min`` and ``max`` over the blocks would."""
        for block in self.history[self.folded :]:
            count, start = int(block.get("count", 0)), float(block.get("start", 0))
            self.samples += count
            self.oldest = start if self.oldest is None or start < self.oldest else self.oldest
            self.newest = start if self.newest is None or start > self.newest else self.newest
            self.folded += 1
        return self.samples, self.oldest, self.newest


def block_index(runtime, device_id, history):
    """The ``BlockIndex`` of ``device_id``'s current history list (None: not cached)."""
    known = getattr(runtime, "_block_index", None)
    if known is None or device_id is None:
        return None
    index = known.get(device_id)
    if index is None or index.history is not history or index.shrunk():
        index = known[device_id] = BlockIndex(history)
    return index


def history_summary(runtime, device_id):
    """``(blocks, samples, oldest, newest)`` of a device's stored history for the snapshot,
    without walking every block on each poll."""
    history = runtime.data["devices"][device_id].get("history", [])
    index = block_index(runtime, device_id, history) or BlockIndex(history)
    return len(history), *index.summary()


def _block_end(block):
    """The end ``_block_samples`` compares with the cutoff; infinite when it cannot tell (an
    unreadable or NaN end), so such a block is always read (and skipped) as before."""
    bounds = block_bounds(block)
    return bounds[1] if bounds and bounds[1] == bounds[1] else math.inf


def block_bounds(block):
    """``(start, end)`` of a stored block as ``_block_samples`` reads them; None for a block
    that never yields a sample (its start or end cannot be read)."""
    try:
        start = float(block["start"])
        return start, float(block.get("end", start + SPAN))
    except (KeyError, TypeError, ValueError):
        return None


def _first_block(runtime, device_id, history, cutoff):
    index = block_index(runtime, device_id, history)
    return 0 if index is None else index.first(cutoff)


def _history_view(runtime, device_id, members=True):
    from ..rooms import attribution
    from ..runtime.coordinator import TunerRuntime

    device = runtime.data["devices"].get(device_id)
    if not device:
        raise ValueError("Unknown device")
    snapshot = {key: device.get(key) for key in TRAINING_FIELDS}
    snapshot.update(_stored_copy(device))
    snapshot["history_labels"] = deepcopy(device.get("history_labels", []))
    view = TunerRuntime(
        None,
        None,
        {
            "devices": {device_id: snapshot},
            "storage_settings": settings(runtime.data),
            # Learning settings (the automatic evidence weight) travel to executor fits.
            "learning_schedule": deepcopy(runtime.data.get("learning_schedule", {})),
        },
    )
    view._history_runtime[device_id] = _pending_copy(runtime, device_id)
    # Executor jobs cannot read the registries, so room attribution inputs travel with the view.
    saved = view.data["room_snapshot"] = attribution.snapshot(runtime, device_id)
    # Room estimates read the other members' recorded guesses, so their history travels too.
    for key in (saved or {}).get("members", {}) if members else ():
        other = runtime.data["devices"].get(key)
        if key != device_id and other is not None:
            view.data["devices"][key] = _stored_copy(other)
            view._history_runtime[key] = _pending_copy(runtime, key)
    return view


def _stored_copy(device):
    """What a view shares of a device's stored history: the lists (their blocks and chunks
    are never edited, only replaced), and a copy of the labelled histograms, which recording
    keeps counting into (they set the radar's quiet baseline, ``trust.ceilings``)."""
    return {
        **{key: list(device.get(key, [])) for key in STORED},
        "histograms": deepcopy(device.get("histograms", {})),
    }


def _pending_copy(runtime, device_id):
    """A view's own copy of the samples and guess runs not yet stored (the open run is
    still extended by recording)."""
    return {
        "samples": list(pending_samples(runtime, device_id)),
        "runs": [tuple(run) for run in guesses.pending(runtime, device_id)],
    }


@lru_cache(maxsize=1024)
def _decode_history_block(encoded, version, count):
    return decode_payload(encoded, version, count)


def _record_manual_histogram(runtime, device, row, now):
    label = runtime._history_label_at(device, now)
    if label in {"present", "not_present"}:
        for index, key in enumerate(HISTORY_KEYS):
            if row[index] > 100:
                continue
            series = device.setdefault("histograms", {}).setdefault(
                key, {"present": [0] * HISTOGRAM_BINS, "not_present": [0] * HISTOGRAM_BINS}
            )
            series[label][row[index]] += 1
            runtime._compress_histogram(series[label])


def _stored_history_samples(device, cutoff, first=0, until=math.inf):
    history = device.get("history", [])
    for index in range(first, len(history)):
        if until == math.inf or _starts_before(history[index], until):
            yield from block_rows(history[index], cutoff)


def _starts_before(block, until):
    """Whether a block may hold a sample before ``until`` (its samples start at its start)."""
    try:
        return float(block["start"]) < until
    except (KeyError, TypeError, ValueError):
        return False  # Unreadable: it yields no sample at all.


def block_rows(block, cutoff):
    """One stored block's samples at or after ``cutoff``; none when it cannot be read."""
    try:
        yield from _block_samples(block, cutoff)
    except (KeyError, TypeError, ValueError, zlib.error, struct.error):
        return


def _block_samples(block, cutoff):
    start = float(block["start"])
    if float(block.get("end", start + SPAN)) < cutoff:
        return
    count, version = int(block["count"]), block.get("version", 1)
    raw = _decode_history_block(block["data"], version, count)
    stride = STRIDES[version]
    if len(raw) < count * stride:
        return
    for pos in range(0, count * stride, stride):
        timestamp = start + (raw[pos] << 8 | raw[pos + 1])  # Big-endian offset.
        if timestamp >= cutoff:
            yield timestamp, raw[pos + 2 : pos + 2 + len(HISTORY_KEYS)]  # The energies.


def block_columns(block, cutoff):
    """One stored block's samples at or after ``cutoff`` as ``(times, columns)``: the sample
    times, and for each of the ``HISTORY_KEYS`` a bytes of the gate's readings; None when
    the block cannot be read or holds none of them."""
    try:
        return _block_columns(block, cutoff)
    except (KeyError, TypeError, ValueError, zlib.error, struct.error):
        return None


def _block_columns(block, cutoff):
    start = float(block["start"])
    if float(block.get("end", start + SPAN)) < cutoff:
        return None
    columns = _decode_history_columns(block["data"], block.get("version", 1), int(block["count"]))
    times = [start + (high << 8 | low) for high, low in zip(columns[0], columns[1], strict=True)]
    readings = columns[2 : 2 + len(HISTORY_KEYS)]
    if start < cutoff:  # Offsets are not negative: later blocks need no check.
        late = [t >= cutoff for t in times]
        if not all(late):
            times = list(compress(times, late))
            readings = [bytes(compress(column, late)) for column in readings]
    return (times, readings) if times else None


MISSING_ENERGY = bytes([255])  # What a gate that did not read is kept as.


@lru_cache(maxsize=2048)
def _decode_history_columns(encoded, version, count):
    if version not in STRIDES:
        raise ValueError("Invalid history block length or version")
    stride = STRIDES[version]
    if version in DELTA_CODED:
        return tuple(decode_column_list(encoded, stride, count))
    raw = decode_payload(encoded, version, count)
    return tuple(raw[column::stride] for column in range(stride))


def _history_columns(runtime, device: dict[str, Any], since=0, until=math.inf):
    """The samples ``_iter_history_samples`` reads, in batches of ``(times, columns)`` (see
    ``block_columns``): a block's at a time, then the pending samples."""
    cutoff = max(since, time.time() - retention_seconds(runtime.data))
    device_id = _device_id(runtime, device)
    history = device.get("history", [])
    for index in range(_first_block(runtime, device_id, history, cutoff), len(history)):
        if until == math.inf or _starts_before(history[index], until):
            if found := block_columns(history[index], cutoff):
                yield found
    pending = list(_pending_history_samples(runtime, device_id, cutoff))
    if pending:
        rows = [
            bytes(row[: len(HISTORY_KEYS)]).ljust(len(HISTORY_KEYS), MISSING_ENERGY)
            for _, row in pending
        ]
        yield (
            [ts for ts, _ in pending],
            [b"".join(rows)[column :: len(HISTORY_KEYS)] for column in range(len(HISTORY_KEYS))],
        )


def _pending_history_samples(runtime, device_id, cutoff):
    for timestamp, row in pending_samples(runtime, device_id):
        if timestamp >= cutoff:
            yield timestamp, row


async def _clean_devices(runtime, changed):
    # Registry discovery can change the device dictionary while cleanup awaits its executor.
    devices = runtime.data.get("devices", {}).copy()
    for device_id, device in devices.items():
        snapshot = {
            key: deepcopy(device[key])
            for key in (
                *STORED,
                "history_labels",
                "histograms",
                "history_legacy_histograms",
                "history_legacy_since",
                *TRAINING_FIELDS,
            )
            if key in device
        }
        pending = list(pending_samples(runtime, device_id))
        revision = device.get("label_revision", 0)
        policy = settings(runtime.data)
        updated, stats, counts = await runtime.hass.async_add_executor_job(
            _clean_and_count,
            snapshot,
            pending,
            time.time(),
            retention_seconds(runtime.data),
            policy,
        )
        # Sampling, Clear or a human correction may have run in the meantime.
        # Leave their new data intact; the next maintenance pass retries.
        if (
            settings(runtime.data) != policy
            or runtime.data.get("devices", {}).get(device_id) is not device
            or device.get("label_revision", 0) != revision
            or any(device.get(key) != snapshot.get(key) for key in snapshot)
            or pending_samples(runtime, device_id) != pending
        ):
            await label_counts.ensure(runtime, device_id)  # Counts do not wait for a retry.
            continue
        if any(device.get(key) != value for key, value in updated.items()):
            _accept_cleanup(device, updated, stats, revision)
            changed = True
        # The live history now equals the cleaned one block for block.
        label_counts.remember(runtime, device_id, counts, updated["history"])
    return changed


def _clean_and_count(snapshot, pending, now, retention, policy):
    """In the executor: clean a device's history, then count its labelled samples as the
    histogram rebuild would (``label_counts``), so a later label edit stays cheap."""
    updated, stats = clean_history(snapshot, pending, now, retention, policy)
    after = {**snapshot, **updated}
    index = labels.label_index(after)
    counts = label_counts.count(after.get("history", []), pending, max(0, now - retention), index)
    return updated, stats, counts


def _current_guess(device, now):
    """``(state, percent, basis)`` of the radar's guess for a sample taken ``now``."""
    last = device.get("auto", {}).get("last_classification", {})
    basis = guesses.EXTERNAL if last.get("basis") == "external" else guesses.REFERENCE
    if last.get("buffered"):
        return "unknown", 0, basis  # Confirm only after the external end buffer has elapsed.
    state = (
        last.get("state")
        if now - last.get("timestamp", 0) <= AUTO_SAMPLE_INTERVAL * 2
        else "unknown"
    )
    return state, max(0, min(100, round(last.get("confidence", 0) * 100))), basis
