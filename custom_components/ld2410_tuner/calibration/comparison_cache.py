"""Bounded, persisted score records keyed by the threshold configuration they measure."""

import hashlib
import json
from copy import deepcopy

from . import comparison

FIELD = "comparison_scores"
METADATA = ("timing", "counts", "outliers", "incomplete_samples", "evaluated_at")


def pattern_key(context, pattern):
    # Slot names and learning IDs are not signal behavior. Reuse identical settings.
    values = {
        key: float(value) if type(value) in (int, float) else value
        for key, value in pattern["thresholds"].items()
    }
    payload = json.dumps({"entities": context["entities"], "thresholds": values}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def stored(device, context, pattern):
    record = device.get(FIELD, {}).get(pattern_key(context, pattern))
    if not isinstance(record, dict) or record.get("scorer_version") != comparison.SCORER_VERSION:
        return None
    if "score" not in record:
        return None
    return record


def missing(device, context, retry_unscored=False):
    wanted, seen = {}, set()
    for slot, pattern in context["patterns"].items():
        if (
            pattern is None
            or pattern.get("joint")
            or not comparison.complete_thresholds(pattern["thresholds"], context["keys"])
        ):
            continue
        key = pattern_key(context, pattern)
        record = stored(device, context, pattern)
        needs_score = record is None or (retry_unscored and record["score"] is None)
        if needs_score and key not in seen:
            wanted[slot] = pattern
            seen.add(key)
    return wanted


def report(device, context):
    patterns = {
        slot: _item(device, context, pattern) for slot, pattern in context["patterns"].items()
    }
    dates = {
        item["evaluated_at"] for item in patterns.values() if item.get("evaluated_at") is not None
    }
    return {
        "state": "pending" if missing(device, context) else "ready",
        "model": comparison.SCORE_MODEL,
        "scorer_version": comparison.SCORER_VERSION,
        "patterns": patterns,
        "best_slots": comparison.best_slots(patterns),
        "mixed_evidence": len(dates) > 1,
    }


def _item(device, context, pattern):
    if pattern is None:
        return {"score": None, "reason": "No saved result", "applicable": False}
    if pattern.get("joint"):
        return {
            "score": None,
            "reason": "Joint zone result; compare and apply the complete group in Rooms and zones",
            "applicable": False,
        }
    if not comparison.complete_thresholds(pattern["thresholds"], context["keys"]):
        return {
            "score": None,
            "reason": "A complete threshold set for the active gates is unavailable",
            "applicable": False,
        }
    record = stored(device, context, pattern)
    if record is None:
        return {
            "score": None,
            "reason": "Score not calculated for this scorer version",
            "applicable": False,
        }
    return {
        **record,
        "result_id": pattern.get("id"),
        "applicable": pattern["applicable"] and record.get("thresholds_complete", False),
    }


def remember(device, context, calculated):
    records = device.setdefault(FIELD, {})
    metadata = {key: calculated.get(key) for key in METADATA}
    for slot, item in calculated["patterns"].items():
        pattern = context["patterns"][slot]
        if pattern is not None:
            records[pattern_key(context, pattern)] = deepcopy(
                {
                    **item,
                    **metadata,
                    "scorer_version": comparison.SCORER_VERSION,
                }
            )
    active = {
        pattern_key(context, pattern)
        for pattern in context["patterns"].values()
        if pattern is not None
    }
    device[FIELD] = {key: value for key, value in records.items() if key in active}
