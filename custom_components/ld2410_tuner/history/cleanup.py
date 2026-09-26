"""Canonical history maintenance; all functions are safe to run in an executor."""

from __future__ import annotations

import base64
import heapq
import math
import struct
import zlib
from copy import deepcopy

KEYS = [f"g{gate}_{kind}" for gate in range(9) for kind in ("move", "still")]


def _labels(labels, cutoff):
    events, valid = _label_events(labels, cutoff)
    active, heap, result = set(), [], []
    boundaries = sorted(events)
    for index, start in enumerate(boundaries[:-1]):
        winner = _latest_label(events[start], active, heap)
        if winner is None:
            continue
        label = dict(valid[winner], start=start, end=boundaries[index + 1])
        _append_label(result, label)
    return result


def _valid_label(label, cutoff):
    try:
        start, end = float(label["start"]), float(label["end"])
        if (
            label["state"] not in {"present", "not_present", "unknown"}
            or not math.isfinite(start)
            or not math.isfinite(end)
        ):
            return None
        start = max(start, cutoff)
        return dict(label, start=start, end=end) if end > start else None
    except (KeyError, TypeError, ValueError):
        return None


def _label_events(labels, cutoff):
    events, valid = {}, {}
    for index, original in enumerate(labels):
        label = _valid_label(original, cutoff)
        if label is None:
            continue
        valid[index] = label
        events.setdefault(label["start"], []).append((True, index))
        events.setdefault(label["end"], []).append((False, index))
    return events, valid


def _latest_label(events, active, heap):
    for add, index in events:
        if add:
            active.add(index)
            heapq.heappush(heap, -index)
        else:
            active.discard(index)
    while heap and -heap[0] not in active:
        heapq.heappop(heap)
    return -heap[0] if heap else None


def _append_label(result, label):
    if (
        result
        and result[-1]["end"] == label["start"]
        and result[-1]["state"] == label["state"]
        and result[-1].get("source") == label.get("source")
    ):
        result[-1]["end"] = label["end"]
    else:
        result.append(label)


def encode_payload(raw, stride=22):
    """Delta-code each byte column before zlib; values and timestamps are lossless."""
    columns = bytearray()
    for column in range(stride):
        previous = 0
        for value in raw[column::stride]:
            columns.append((value - previous) % 256)
            previous = value
    return base64.b64encode(zlib.compress(bytes(columns), 6)).decode("ascii")


def decode_payload(encoded, version, count):
    raw = zlib.decompress(base64.b64decode(encoded, validate=True))
    stride = 20 if version == 1 else 22
    if version not in (1, 2, 3) or count < 0 or len(raw) != count * stride:
        raise ValueError("Invalid history block length or version")
    if version != 3:
        return raw
    restored = bytearray(len(raw))
    for column in range(stride):
        value = 0
        for index in range(count):
            value = (value + raw[column * count + index]) % 256
            restored[index * stride + column] = value
    return bytes(restored)


def _encode(samples):
    blocks = []
    group = []

    def flush():
        if not group:
            return
        start = group[0][0]
        raw = b"".join(struct.pack(">H", round(ts - start)) + row for ts, row in group)
        blocks.append(
            {
                "version": 3,
                "start": start,
                "end": group[-1][0],
                "count": len(group),
                "data": encode_payload(raw),
            }
        )

    for ts, row in samples:
        if group:
            offset = ts - group[0][0]
            # Preserve the original timestamps exactly across block boundaries.
            if len(group) >= 60 or offset > 65535 or abs(offset - round(offset)) > 1e-6:
                flush()
                group = []
        group.append((ts, row))
    flush()
    return blocks


def clean_history(device, pending, now, retention_seconds):
    """Return normalized data without altering the caller's live objects.

    Later duplicate rows win; missing energies remain missing. Version 1 did
    not record confidence, so its new confidence fields are explicitly zero.
    Untimed legacy histograms remain separate and are never assigned timestamps.
    """
    cutoff = now - retention_seconds
    rows = {}
    stats = {
        "migrated_blocks": 0,
        "invalid_blocks": 0,
        "expired_samples": 0,
        "duplicate_samples": 0,
        "repaired_samples": 0,
        "discarded_samples": 0,
    }
    _clean_blocks(device, cutoff, now, stats, rows)
    samples = sorted(rows.items())
    labels = _labels(device.get("history_labels", []), cutoff)
    active_start = device.get("training_label_start")
    active_end = device.get("training_expires_at") or now + 1
    histogram_labels = labels
    if active_start is not None and device.get("training_state") in {"present", "not_present"}:
        histogram_labels = _labels(
            labels
            + [{"start": active_start, "end": active_end, "state": device["training_state"]}],
            cutoff,
        )
    # Keep untimed evidence if this is a legacy histogram-only device.
    legacy = device.get(
        "history_legacy_histograms",
        {} if device.get("history") or pending else device.get("histograms", {}),
    )
    histograms = _legacy_histograms(legacy)
    _accumulate_histograms(histograms, samples, pending, cutoff, histogram_labels)
    return {
        "history": _encode(samples),
        "history_labels": labels,
        "histograms": histograms,
        "history_legacy_histograms": deepcopy(legacy),
    }, stats


def _clean_blocks(device, cutoff, now, stats, rows):
    for block in device.get("history", []):
        try:
            start, version, raw, stride = _read_clean_block(block)
        except (KeyError, TypeError, ValueError, zlib.error):
            stats["invalid_blocks"] += 1
            continue
        stats["migrated_blocks"] += version < 3
        for pos in range(0, len(raw), stride):
            timestamp = start + struct.unpack(">H", raw[pos : pos + 2])[0]
            _retain_row(timestamp, raw[pos + 2 : pos + stride], version, rows, stats, cutoff, now)


def _read_clean_block(block):
    if not isinstance(block, dict):
        raise ValueError("Invalid block")
    version, start, count = block.get("version", 1), float(block["start"]), block["count"]
    if (
        version not in (1, 2, 3)
        or not math.isfinite(start)
        or not isinstance(count, int)
        or count < 0
    ):
        raise ValueError("Invalid block header")
    raw = decode_payload(block["data"], version, count)
    stride = 20 if version == 1 else 22
    if len(raw) != count * stride:
        raise ValueError("Invalid block length")
    return start, version, raw, stride


def _retain_row(timestamp, original, version, rows, stats, cutoff, now):
    if timestamp < cutoff:
        stats["expired_samples"] += 1
        return
    if timestamp > now + 60:
        stats["discarded_samples"] += 1
        return
    row = bytes(value if value <= 100 else 255 for value in original[:18])
    if all(value == 255 for value in row):
        stats["discarded_samples"] += 1
        return
    automatic = original[18:20] if version >= 2 else bytes(2)
    if automatic[0] not in (1, 2) or not 1 <= automatic[1] <= 100:
        automatic = bytes(2)
    normalized = row + automatic
    stats["repaired_samples"] += row != original[:18] or (version >= 2 and normalized != original)
    stats["duplicate_samples"] += timestamp in rows
    rows[timestamp] = normalized


def _legacy_histograms(legacy):
    histograms = {}
    for key in KEYS:
        histograms[key] = {}
        for state in ("present", "not_present"):
            source = legacy.get(key, {}).get(state, [])
            histogram = [v if isinstance(v, int) and v >= 0 else 0 for v in source[:101]]
            histogram += [0] * (101 - len(histogram))
            while sum(histogram) > 5000:
                histogram = [v // 2 for v in histogram]
            histograms[key][state] = histogram
    return histograms


def _accumulate_histograms(histograms, samples, pending, cutoff, histogram_labels):
    for row, state in _labelled_rows(samples, pending, cutoff, histogram_labels):
        for key, value in zip(KEYS, row[:18], strict=False):
            if value > 100:
                continue
            histogram = histograms[key][state]
            histogram[value] += 1
            if sum(histogram) > 5000:
                histogram[:] = [count // 2 for count in histogram]


def _labelled_rows(samples, pending, cutoff, labels):
    index = 0
    for timestamp, row in sorted(samples + list(pending)):
        if timestamp < cutoff:
            continue
        index = _advance_label(labels, index, timestamp)
        if index >= len(labels):
            break
        label = labels[index]
        if timestamp >= label["start"] and label["state"] != "unknown":
            yield row, label["state"]


def _advance_label(labels, index, timestamp):
    while index < len(labels) and labels[index]["end"] <= timestamp:
        index += 1
    return index
