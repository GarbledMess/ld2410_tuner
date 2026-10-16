"""One-way conversion of history kept in the JSON store into day files. Run once at startup,
in the executor, before anything reads history; the caller then saves the JSON without it.

This module is the only reader of the old formats: sampled blocks (versions 1 to 4: a 2-byte
offset and 18 energies per row; versions 2 and 3 also hold the radar's guess, a code and a
confidence; versions 3 and 4 are delta-coded byte columns) and guess run chunks. A sampled
row holds until the next row; a gap longer than ``GAP`` ends coverage half a sample after the
last row before it; a later row at the same time replaces the earlier one. Transition blocks
and a saved open block were never written by a release; they count as unreadable. Untimed
legacy summaries cannot be placed on days and are dropped. Converting
again (an interrupted start) rewrites the same files.
"""

import base64
import struct
import zlib
from itertools import accumulate

from ..calibration.constants import SAMPLE_SECONDS
from . import dayfile, guesses
from .guesses import GAP

STORED = ("history", "guess_runs", "open_event_history")
# Untimed summaries from before history was timed (and the raw value arrays before them):
# they cannot be placed on any day.
UNTIMED = ("history_legacy_histograms", "history_legacy_since", "samples")
BATCH = 50_000  # Records appended between flushes, bounding memory on large histories.
ORDER = {dayfile.End: 0, dayfile.Change: 1, dayfile.Guess: 2}
STRIDES = {1: 20, 2: 22, 3: 22, 4: 20}  # Bytes per row of each sampled block version.
DELTA_CODED = (3, 4)
RUN = struct.Struct(">ddBBB")  # A guess run chunk row: start, end, state code, level, basis.
UNREADABLE = (KeyError, TypeError, ValueError, zlib.error, struct.error)
STATES = (None, "present", "not_present")  # Stored guess state codes.


def run(data, days, now):
    """Convert every device still holding JSON history; True when anything was converted."""
    converted = False
    for device_id, device in data.get("devices", {}).items():
        if any(key in device for key in (*STORED, *UNTIMED)):
            _convert(device_id, device, days, now)
            converted = True
    return converted


def _convert(device_id, device, days, now):
    days.remove_device(device_id)
    stats = {"invalid_blocks": 0}
    records = sorted(_records(device, stats), key=lambda record: (record.time, ORDER[type(record)]))
    for index, record in enumerate(records, 1):
        days.append(device_id, record)
        if index % BATCH == 0:
            days.flush(record.time)  # As of the history so far: later records follow.
    days.flush(now)
    if [device.pop(key) for key in UNTIMED if key in device]:
        stats["untimed_summaries"] = 1
    for key in STORED:
        device.pop(key, None)
    _invalidate(device, stats)


def _invalidate(device, stats):
    """Learned results came from history that could not all be kept: they go."""
    if any(stats.values()):
        device["history_cleanup"] = stats
        device["label_revision"] = device.get("label_revision", 0) + 1
        device.pop("last_learning", None)


def _records(device, stats):
    rows = {}
    for block in device.get("history", []):
        try:
            rows.update(_block_rows(block))  # A later row at the same time replaces it.
        except UNREADABLE:
            stats["invalid_blocks"] += 1
    if device.get("open_event_history"):
        stats["invalid_blocks"] += 1
    yield from _sampled(sorted(rows.items()))
    yield from _guesses(_chunk_runs(device.get("guess_runs", []), stats))


def _block_rows(block):
    """``{time: (energies, guess)}`` of one sampled block (``guess`` None when it has none)."""
    if block.get("kind") == "transitions":
        raise ValueError("Transition blocks were never released")
    start, count, version = float(block["start"]), int(block["count"]), block.get("version", 1)
    if version not in STRIDES:
        raise ValueError("Unknown history block version")
    stride = STRIDES[version]
    raw = _payload(block["data"], stride, count, version in DELTA_CODED)
    found = {}
    for position in range(0, count * stride, stride):
        timestamp = start + (raw[position] << 8 | raw[position + 1])
        energies = raw[position + 2 : position + 20]
        guess = _from_code(*raw[position + 20 : position + 22]) if stride == 22 else None
        found[timestamp] = (energies, guess)
    return found


def _from_code(code, percent):
    """The guess an old-layout row held: a state code and a confidence in percent."""
    valid = code < len(STATES) and 1 <= percent <= 100
    return guesses.guess(STATES[code], percent) if valid else None


def _payload(encoded, stride, count, delta):
    raw = zlib.decompress(base64.b64decode(encoded, validate=True))
    if count < 0 or len(raw) != count * stride:
        raise ValueError("Invalid history block length")
    if not delta:
        return raw
    restored = bytearray(count * stride)
    for column in range(stride):
        deltas = raw[column * count : (column + 1) * count]
        restored[column::stride] = bytes(value & 255 for value in accumulate(deltas))
    return bytes(restored)


def _vector(energies):
    return tuple(None if value > 100 else value for value in energies)


def _diff(previous, vector):
    return {
        gate: value
        for gate, value in enumerate(vector)
        if previous is None or previous[gate] != value
    }


def _sampled(rows):
    """Energies held from each row to the next and, from old-layout rows, their guesses."""
    previous, guess, last = None, None, None
    for timestamp, (energies, row_guess) in rows:
        if last is not None and timestamp - last > GAP:
            yield dayfile.End(last + SAMPLE_SECONDS / 2)
            previous, guess = None, None
        vector = _vector(energies)
        if changed := _diff(previous, vector):
            yield dayfile.Change(timestamp, changed)
        fields = _guess_fields(row_guess)
        if fields != guess:
            yield dayfile.Guess(timestamp, *(fields or (None, 0, 0)))
        previous, guess, last = vector, fields, timestamp
    if last is not None:
        yield dayfile.End(last + SAMPLE_SECONDS / 2)


def _guess_fields(sample_guess):
    if not sample_guess:
        return None
    state, percent, basis = sample_guess
    return state, guesses.level(percent), basis


def _chunk_runs(chunks, stats):
    runs = []
    for chunk in chunks:
        try:
            raw = _payload(chunk["data"], RUN.size, int(chunk["count"]), True)
        except UNREADABLE:
            stats["invalid_blocks"] += 1
            continue
        runs.extend(
            (start, end, STATES[code], level, basis)
            for start, end, code, level, basis in RUN.iter_unpack(raw)
            if 0 < code < len(STATES) and level
        )
    return sorted(runs)


def _guesses(runs):
    starts = {run[0] for run in runs}
    for start, end, state, level, basis in runs:
        yield dayfile.Guess(start, state, level, basis)
        if end not in starts:
            yield dayfile.Guess(end, None, 0, 0)
