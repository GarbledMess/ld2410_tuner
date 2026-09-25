"""Replay private recordings without emitting identifiers or modifying the input.

Usage: python tools/replay_autolabels.py STORE --baseline OLD_INFERENCE.py
       [--labels EXPORT] [--output REPORT.json]
The latest 20% of each labelled class is evaluation-only in every scenario.
Stored six-second observations cannot reconstruct intervening live readings.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import struct
import zlib
from bisect import bisect_right
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "ld2410_tuner"
KEYS = [f"g{gate}_{kind}" for gate in range(9) for kind in ("move", "still")]
LABELS = {"present", "not_present"}


def load_module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decode_rows(device):
    """Normalize label overlaps with the same rules as the integration."""
    history = load_module(PACKAGE / "history" / "cleanup.py")
    labels = history._labels(device.get("history_labels", []), float("-inf"))
    starts = [label["start"] for label in labels]
    rows = []
    for timestamp, values in _encoded_samples(device):
        index = bisect_right(starts, timestamp) - 1
        truth = labels[index]["state"] if index >= 0 and timestamp < labels[index]["end"] else None
        rows.append((timestamp, values, truth))
    return sorted(rows, key=lambda row: row[0])


def compress(histogram):
    if sum(histogram) > 5000:
        histogram[:] = [count // 2 for count in histogram]


def update_background(background, values, result):
    for key, value in values.items():
        histogram = background.setdefault(key, [0] * 101)
        if sum(histogram) < 20 or result["presence_probability"] < 0.7:
            histogram[value] += 1
            compress(histogram)


def update_references(references, values, truth):
    for key, value in values.items():
        histogram = references.setdefault(key, {}).setdefault(truth, [0] * 101)
        histogram[value] += 1
        compress(histogram)


def confirm_legacy(result, state, _now):
    """Adapter for the v1 production call-count confirmation, for comparison."""
    label = result["label"]
    state["count"] = state.get("count", 0) + 1 if state.get("pending") == label else 1
    state["pending"] = label
    if label == "unknown":
        state.update(count=0, state="unknown")
        return "unknown"
    if label == "present" and state.get("state") != "present" and result["score"] <= 0:
        state["count"] = 0
        return "unknown"
    if state["count"] < (3 if label == "present" else 5):
        return "unknown"
    state["state"] = label
    return label


class Metrics:
    def __init__(self):
        self.matrix = Counter()
        self.confidence = {}
        self.previous = None
        self.false_bursts = 0
        self.empty_seconds = 0
        self.missed_run = 0
        self.longest_missed_run = 0

    def add(self, timestamp, truth, predicted, confidence):
        self.matrix[f"{truth} -> {predicted}"] += 1
        previous = self.previous
        continuous = previous is not None and previous[1] == truth and timestamp - previous[0] <= 12
        _count_prediction(self, timestamp, truth, predicted, continuous, previous)
        if predicted != "unknown":
            bucket = str(round(confidence, 2))
            counts = self.confidence.setdefault(bucket, {"count": 0, "correct": 0})
            counts["count"] += 1
            counts["correct"] += predicted == truth
        self.previous = (timestamp, truth, predicted)

    def summary(self):
        return {
            "matrix": dict(self.matrix),
            "confidence": self.confidence,
            "false_bursts": self.false_bursts,
            "false_bursts_per_empty_hour": self.false_bursts * 3600 / self.empty_seconds
            if self.empty_seconds
            else None,
            "longest_missed_presence_run": self.longest_missed_run,
        }


def evaluate(rows, expected, model, mode):
    counts = Counter(row[2] for row in rows)
    seen = Counter()
    references, background, temporal, confirmation = {}, {}, {}, {}
    metrics = {name: Metrics() for name in ("development", "validation", "all")}
    confirm = getattr(model, "confirm_estimate", confirm_legacy)
    last_label = max((index for index, row in enumerate(rows) if row[2] in LABELS), default=-1)
    for index, (timestamp, values, truth) in enumerate(rows[: last_label + 1]):
        result = model.estimate_presence(
            values, references, background, temporal, timestamp, expected, {}
        )
        update_background(background, values, result)
        predicted = confirm(result, confirmation, timestamp)
        if truth not in LABELS:
            continue
        seen[truth] += 1
        development = seen[truth] <= int(counts[truth] * 0.8)
        split = "development" if development else "validation"
        for group in ("all", split):
            metrics[group].add(timestamp, truth, predicted, result["confidence"])
        if _should_train(mode, index, len(rows), development):
            update_references(references, values, truth)
    return {name: metric.summary() for name, metric in metrics.items()}


def run(args):
    payload = json.loads(args.store.read_text())
    devices = payload.get("data", payload)["devices"]
    labels = json.loads(args.labels.read_text())["devices"] if args.labels else {}
    models = {
        "before": load_module(args.baseline),
        "after": load_module(PACKAGE / "presence" / "inference.py"),
    }
    reports = []
    for index, (device_id, original) in enumerate(devices.items()):
        if not original.get("history"):
            continue
        device = dict(original)
        if device_id in labels:
            device["history_labels"] = labels[device_id]["history"]["labels"]
        rows = decode_rows(device)
        expected = [
            f"g{item['gate']}_{item['kind']}" for item in device.get("entities", {}).values()
        ]
        for result in _compare_device(index, rows, expected, models):
            reports.append(result)
            _write_progress(args, reports, result, models)
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=Path)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--output", type=Path)
    run(parser.parse_args())


def _should_train(mode, index, total, development):
    return development and (mode == "causal" or (mode == "first_half" and index < total // 2))


def _count_prediction(metric, timestamp, truth, predicted, continuous, previous):
    if truth == "not_present":
        _count_empty(metric, timestamp, predicted, continuous, previous)
        return
    metric.missed_run = metric.missed_run if continuous else 0
    metric.missed_run = metric.missed_run + 1 if predicted != "present" else 0
    metric.longest_missed_run = max(metric.longest_missed_run, metric.missed_run)


def _count_empty(metric, timestamp, predicted, continuous, previous):
    metric.empty_seconds += min(6, timestamp - previous[0]) if continuous else 6
    if predicted == "present" and not (continuous and previous[2] == "present"):
        metric.false_bursts += 1


def _write_progress(args, reports, result, models):
    matrices = {name: result[name]["validation"]["matrix"] for name in models}
    print(
        json.dumps({"device": result["device"], "scenario": result["scenario"], **matrices}),
        flush=True,
    )
    if args.output:
        args.output.write_text(json.dumps(reports, indent=2) + "\n")


def _encoded_samples(device):
    for block in device.get("history", []):
        stride = 20 if block.get("version", 1) == 1 else 22
        raw = zlib.decompress(base64.b64decode(block["data"], validate=True))
        if len(raw) != block["count"] * stride:
            raise ValueError("Invalid history block length")
        for offset in range(0, len(raw), stride):
            timestamp = block["start"] + struct.unpack(">H", raw[offset : offset + 2])[0]
            values = {
                key: value
                for key, value in zip(KEYS, raw[offset + 2 : offset + 20], strict=False)
                if value <= 100
            }
            yield timestamp, values


def _compare_device(index, rows, expected, models):
    for mode in ("bootstrap", "causal", "first_half"):
        result = {"device": index, "scenario": mode, "samples": len(rows)}
        for name, model in models.items():
            result[name] = evaluate(rows, expected, model, mode)
        yield result


if __name__ == "__main__":
    main()
