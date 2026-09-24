"""Canonical history maintenance; all functions are safe to run in an executor."""
from __future__ import annotations

import base64
from copy import deepcopy
import heapq
import math
import struct
import zlib

KEYS = [f"g{gate}_{kind}" for gate in range(9) for kind in ("move", "still")]


def _labels(labels, cutoff):
    events = {}
    valid = {}
    for index, label in enumerate(labels):
        try:
            start, end = float(label["start"]), float(label["end"])
            if label["state"] not in {"present", "not_present", "unknown"} or not math.isfinite(start) or not math.isfinite(end):
                continue
            start = max(start, cutoff)
            if end <= start:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        valid[index] = dict(label, start=start, end=end)
        events.setdefault(start, []).append((True, index))
        events.setdefault(end, []).append((False, index))
    active, heap, result = set(), [], []
    boundaries = sorted(events)
    for i, start in enumerate(boundaries[:-1]):
        for add, index in events[start]:
            if add:
                active.add(index)
                heapq.heappush(heap, -index)
            else:
                active.discard(index)
        while heap and -heap[0] not in active:
            heapq.heappop(heap)
        if not heap:
            continue
        label = dict(valid[-heap[0]], start=start, end=boundaries[i+1])
        if result and result[-1]["end"] == start and result[-1]["state"] == label["state"] and result[-1].get("source") == label.get("source"):
            result[-1]["end"] = label["end"]
        else:
            result.append(label)
    return result


def _encode(samples):
    blocks = []
    group = []
    def flush():
        if not group:
            return
        start = group[0][0]
        raw = b"".join(struct.pack(">H", round(ts-start))+row for ts, row in group)
        blocks.append({"version": 2, "start": start, "end": group[-1][0], "count": len(group),
                       "data": base64.b64encode(zlib.compress(raw, 6)).decode("ascii")})
    for ts, row in samples:
        if group:
            offset = ts-group[0][0]
            # Preserve the original timestamps exactly across block boundaries.
            if len(group) >= 60 or offset > 65535 or abs(offset-round(offset)) > 1e-6:
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
    cutoff = now-retention_seconds
    rows = {}
    stats = {"migrated_blocks": 0, "invalid_blocks": 0, "expired_samples": 0,
             "duplicate_samples": 0, "repaired_samples": 0, "discarded_samples": 0}
    for block in device.get("history", []):
        try:
            if not isinstance(block, dict):
                raise ValueError("Invalid block")
            version, start, count = block.get("version", 1), float(block["start"]), block["count"]
            if version not in (1, 2) or not math.isfinite(start) or not isinstance(count, int) or count < 0:
                raise ValueError("Invalid block header")
            raw = zlib.decompress(base64.b64decode(block["data"], validate=True))
            stride = 20 if version == 1 else 22
            if len(raw) != count*stride:
                raise ValueError("Invalid block length")
        except (KeyError, TypeError, ValueError, zlib.error):
            stats["invalid_blocks"] += 1
            continue
        stats["migrated_blocks"] += version == 1
        for pos in range(0, len(raw), stride):
            ts = start+struct.unpack(">H", raw[pos:pos+2])[0]
            if ts < cutoff:
                stats["expired_samples"] += 1
                continue
            if ts > now+60:
                stats["discarded_samples"] += 1
                continue
            original = raw[pos+2:pos+stride]
            row = bytes(v if v <= 100 else 255 for v in original[:18])
            if all(v == 255 for v in row):
                stats["discarded_samples"] += 1
                continue
            auto = original[18:20] if version == 2 else bytes(2)
            if auto[0] not in (1, 2) or not 1 <= auto[1] <= 100:
                auto = bytes(2)
            normalized = row+auto
            stats["repaired_samples"] += row != original[:18] or (version == 2 and normalized != original)
            stats["duplicate_samples"] += ts in rows
            rows[ts] = normalized
    samples = sorted(rows.items())
    labels = _labels(device.get("history_labels", []), cutoff)
    active_start = device.get("training_label_start")
    active_end = device.get("training_expires_at") or now+1
    histogram_labels = labels
    if active_start is not None and device.get("training_state") in {"present", "not_present"}:
        histogram_labels = _labels(labels+[{"start": active_start, "end": active_end,
                                           "state": device["training_state"]}], cutoff)
    # Keep untimed evidence if this is a legacy histogram-only device.
    legacy = device.get("history_legacy_histograms", {} if device.get("history") or pending else device.get("histograms", {}))
    histograms = {}
    for key in KEYS:
        histograms[key] = {}
        for state in ("present", "not_present"):
            source = legacy.get(key, {}).get(state, [])
            histogram = [v if isinstance(v, int) and v >= 0 else 0 for v in source[:101]]
            histogram += [0]*(101-len(histogram))
            while sum(histogram) > 5000:
                histogram = [v//2 for v in histogram]
            histograms[key][state] = histogram
    label_index = 0
    # Pending rows already contain current-format confidence; they contribute to
    # histograms but remain in the live buffer rather than being stored twice.
    for ts, row in sorted(samples+list(pending)):
        if ts < cutoff:
            continue
        while label_index < len(histogram_labels) and histogram_labels[label_index]["end"] <= ts:
            label_index += 1
        if label_index >= len(histogram_labels):
            break
        label = histogram_labels[label_index]
        if ts < label["start"] or label["state"] == "unknown":
            continue
        for key, value in zip(KEYS, row[:18]):
            if value > 100:
                continue
            histogram = histograms[key][label["state"]]
            histogram[value] += 1
            if sum(histogram) > 5000:
                histogram[:] = [v//2 for v in histogram]
    return {"history": _encode(samples), "history_labels": labels,
            "histograms": histograms, "history_legacy_histograms": deepcopy(legacy)}, stats
