"""Pure, bounded-file planning: age cleanup first, automatic history before human."""

import json
import struct
import zlib
from bisect import bisect_left
from collections import defaultdict
from copy import deepcopy

from ..const import HISTORY_KEYS, STORAGE_KEY, STORAGE_MINOR_VERSION, STORAGE_VERSION
from . import guesses
from .cleanup import _encode, _read_clean_block, accept_cleanup, clean_history
from .labels import _history_label_reader
from .policy import DAY, MIB, STORED, settings


class BudgetExceeded(ValueError):
    """The protected metadata alone cannot fit the requested ceiling."""


def history_memo(devices):
    """A ``deepcopy`` memo that copies each device's stored lists (history blocks and guess run
    chunks, ``STORED``) shallowly.

    A stored block is never edited once written: flushes append new blocks (or replace the
    last chunk of runs), and cleanup, trims and confirmation replace blocks (and the list)
    rather than change them. A copy may therefore share the blocks, which are nearly all of
    the data."""
    return {
        id(stored): list(stored)
        for device in devices
        if isinstance(device, dict)
        for key in STORED
        if isinstance(stored := device.get(key), list)
    }


def copy_data(data):
    """``deepcopy(data)`` sharing the immutable history blocks (``history_memo``): a
    consistent snapshot of the integration's data at a small fraction of the cost."""
    devices = data.get("devices")
    return deepcopy(data, history_memo(devices.values() if isinstance(devices, dict) else ()))


# id(block) -> (block, length, newlines) of ``json.dumps(block, indent=2)``; holding the block
# keeps its id unique. Rebuilt on each ``storage_bytes`` call, so it holds only current blocks.
_BLOCK_TEXT = {}
HISTORY_LEVEL = 4  # envelope > data > devices > device > history


# TunerStore uses this same standard JSON encoder and HA envelope.
def storage_bytes(data):
    """``len(json.dumps(envelope, indent=2).encode("utf-8"))`` without re-encoding the stored
    history on every save. ASCII output (``ensure_ascii``), so characters are bytes. Every
    stored list (history, guess runs) is measured from its blocks' cached level-0 text: text
    nested ``n`` levels deeper gains ``2n`` spaces after each newline, and nothing else
    changes."""
    global _BLOCK_TEXT
    skeleton, histories = _skeleton(data)
    envelope = {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_MINOR_VERSION,
        "key": STORAGE_KEY,
    }
    size = len(json.dumps({**envelope, "data": skeleton}, indent=2).encode("utf-8"))
    fresh = {}
    for history in histories:
        length, newlines = _list_text(history, _BLOCK_TEXT, fresh)
        size += length + 2 * HISTORY_LEVEL * newlines - len("[]")
    _BLOCK_TEXT = fresh
    return size


def _skeleton(data):
    """``data`` with every non-empty stored list of a device replaced by ``[]``, and those lists."""
    devices = data.get("devices")
    if not isinstance(devices, dict):
        return data, []
    histories, replaced = [], {}
    for key, device in devices.items():
        replaced[key], found = _without_stored(device)
        histories.extend(found)
    return {**data, "devices": replaced}, histories


def _without_stored(device):
    """``device`` with each non-empty stored list (``STORED``) emptied, and those lists."""
    found = []
    for field in STORED if isinstance(device, dict) else ():
        stored = device.get(field)
        if isinstance(stored, list) and stored:
            found.append(stored)
            device = {**device, field: []}
    return device, found


def _list_text(items, known, fresh):
    """``(length, newlines)`` of ``json.dumps(items, indent=2)`` for a non-empty list; each
    item's text comes from ``known`` (else is encoded) and is kept in ``fresh``."""
    length, newlines = 3, 1  # "[", "\n" + "]" closing the list
    for item in items:
        entry = known.get(id(item))
        if entry is None or entry[0] is not item:
            text = json.dumps(item, indent=2)
            entry = (item, len(text), text.count("\n"))
        fresh[id(item)] = entry
        # "\n" and two spaces, then the item one level deeper.
        length += 3 + entry[1] + 2 * entry[2]
        newlines += 1 + entry[2]
    return length + len(items) - 1, newlines  # "," between items


def prepare(data, pending, now, clean=False, target=None):
    data = copy_data(data)
    policy = settings(data)
    ceiling = int(policy["max_mib"] * MIB)
    target = min(ceiling, int(target)) if target is not None else ceiling
    before = storage_bytes(data)
    report = {
        "before_bytes": before,
        "limit_bytes": ceiling,
        "removed_automatic": 0,
        "removed_human": 0,
        "timestamp": now,
    }
    changes = {}
    size = before
    if clean or before > target:
        _clean_all(data, pending, now, policy, changes, report)
        size = storage_bytes(data)
    if size > target:
        size = _limit_history(data, pending, now, policy, target, changes, report, size)
    report["used_bytes"] = size
    if report["used_bytes"] > target:
        raise BudgetExceeded(
            "The size limit is below the remaining settings, summaries and saved results. Increase the limit or clear unused devices; no oversized file was written."
        )
    return data, changes, report


def _clean_all(data, pending, now, policy, changes, report):
    for device_id, device in data.get("devices", {}).items():
        old = _class_counts(device)
        updated, stats = clean_history(
            device, pending.get(device_id, []), now, policy["human_days"] * DAY, policy
        )
        if any(device.get(key) != value for key, value in updated.items()):
            accept_cleanup(device, updated, stats, device.get("label_revision", 0))
            changes[device_id] = (updated, stats)
            new = _class_counts(device)
            report["removed_human"] += max(0, old[1] - new[1])
            report["removed_automatic"] += max(0, old[0] - new[0])


def _rows(block):
    start, _version, raw, stride = _read_clean_block(block)
    for pos in range(0, len(raw), stride):
        row = raw[pos + 2 : pos + 2 + len(HISTORY_KEYS)]
        yield start + struct.unpack(">H", raw[pos : pos + 2])[0], row


def _class_counts(device):
    counts = [0, 0]
    label_at = _history_label_reader(device)
    for block in device.get("history", []):
        try:
            for timestamp, _row in _rows(block):
                counts[label_at(timestamp) in {"present", "not_present"}] += 1
        except (ValueError, KeyError, TypeError, zlib.error, struct.error):
            pass  # clean_history reports corrupt blocks without inventing a label.
    return counts


def _candidates(data):
    chunks = []
    for device_id, device in data["devices"].items():
        label_at = _history_label_reader(device)
        runs = guesses.Guesses(
            run for chunk in device.get("guess_runs", []) for run in guesses.decode(chunk)
        )
        for block in device.get("history", []):
            chunks.extend(_block_candidates(device_id, block, label_at, runs))
    return sorted(chunks, key=lambda item: item[:4])


def _block_candidates(device_id, block, label_at, runs):
    groups = defaultdict(list)
    for timestamp, row in _rows(block):
        groups[label_at(timestamp) in {"present", "not_present"}].append((timestamp, row))
    for human, rows in groups.items():
        confidence = 100 if human else _mean_confidence(rows, label_at, runs)
        for part in _encode(rows):
            yield human, confidence, part["start"], device_id, part


def _mean_confidence(rows, label_at, runs):
    percents = runs.percents(timestamp for timestamp, _row in rows)
    return sum(
        percent if label_at(timestamp) != "unknown" else 0
        for (timestamp, _row), percent in zip(rows, percents, strict=True)
    ) / len(rows)


def _limit_history(data, pending, now, policy, target, changes, report, size):
    chunks = _candidates(data)
    position = 0
    while size > target and position < len(chunks):
        # Leave modest headroom so the next save does not immediately trim again.
        needed = size - int(target * 0.98)
        removed = 0
        while position < len(chunks) and removed < needed:
            human, _confidence, _start, _device_id, block = chunks[position]
            report["removed_human" if human else "removed_automatic"] += block["count"]
            removed += len(json.dumps(block))
            position += 1
        _install_chunks(data, chunks[position:], pending, now, policy, changes)
        size = storage_bytes(data)
    return size


def _install_chunks(data, chunks, pending, now, policy, changes):
    kept = defaultdict(list)
    for _human, _confidence, _start, device_id, block in chunks:
        kept[device_id].append(block)
    for device_id, device in data["devices"].items():
        previous = sum(block["count"] for block in device.get("history", []))
        candidate = {**device, "history": kept[device_id]}
        updated, stats = clean_history(
            candidate, pending.get(device_id, []), now, policy["human_days"] * DAY, policy
        )
        timestamps = sorted(ts for block in updated["history"] for ts, _ in _rows(block))
        updated["history_labels"] = [
            label
            for label in updated["history_labels"]
            if bisect_left(timestamps, label["start"]) < bisect_left(timestamps, label["end"])
        ]
        stats["trimmed_samples"] = previous - sum(block["count"] for block in updated["history"])
        if any(device.get(key) != value for key, value in updated.items()):
            accept_cleanup(device, updated, stats, device.get("label_revision", 0))
            changes[device_id] = (updated, stats)
