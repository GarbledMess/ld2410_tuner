"""Pure, bounded-file planning: age cleanup first, automatic history before human."""

import json
import struct
import zlib
from bisect import bisect_left
from collections import defaultdict
from copy import deepcopy

from ..const import STORAGE_KEY, STORAGE_VERSION
from .cleanup import _encode, _read_clean_block, accept_cleanup, clean_history
from .labels import _history_label_reader
from .policy import DAY, MIB, settings


class BudgetExceeded(ValueError):
    """The protected metadata alone cannot fit the requested ceiling."""


# TunerStore uses this same standard JSON encoder and HA envelope.
def storage_bytes(data):
    envelope = {"version": STORAGE_VERSION, "minor_version": 1, "key": STORAGE_KEY, "data": data}
    return len(json.dumps(envelope, indent=2).encode("utf-8"))


def prepare(data, pending, now, clean=False, target=None):
    data = deepcopy(data)
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
    start, version, raw, stride = _read_clean_block(block)
    for pos in range(0, len(raw), stride):
        row = raw[pos + 2 : pos + stride]
        if version == 1:
            row += bytes(2)
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
        for block in device.get("history", []):
            chunks.extend(_block_candidates(device_id, block, label_at))
    return sorted(chunks, key=lambda item: item[:4])


def _block_candidates(device_id, block, label_at):
    groups = defaultdict(list)
    for timestamp, row in _rows(block):
        groups[label_at(timestamp) in {"present", "not_present"}].append((timestamp, row))
    for human, rows in groups.items():
        confidence = 100 if human else _mean_confidence(rows, label_at)
        for part in _encode(rows):
            yield human, confidence, part["start"], device_id, part


def _mean_confidence(rows, label_at):
    return sum(
        row[19] if row[18] in (1, 2) and label_at(timestamp) != "unknown" else 0
        for timestamp, row in rows
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
